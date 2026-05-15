"""
Image service for handling profile picture uploads
"""
import logging
import os
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple
from PIL import Image
import io

logger = logging.getLogger(__name__)

class ImageService:
    """Service for handling profile picture uploads and management"""

    # Configuration
    UPLOAD_DIR = Path("uploads/profile_pictures")
    ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png'}
    MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
    TARGET_SIZE = (500, 500)  # Maximum dimensions
    THUMBNAIL_SIZE = (150, 150)  # For future use

    def __init__(self):
        """Initialize the image service and ensure upload directory exists"""
        self.upload_path = Path(__file__).parent.parent.parent / self.UPLOAD_DIR
        self.upload_path.mkdir(parents=True, exist_ok=True)

    def validate_image(self, file_data: bytes, filename: str) -> Tuple[bool, str]:
        """
        Validate image file

        Args:
            file_data: Image file bytes
            filename: Original filename

        Returns:
            Tuple of (is_valid, error_message)
        """
        # Check file size
        if len(file_data) > self.MAX_FILE_SIZE:
            return False, f"File size exceeds {self.MAX_FILE_SIZE // (1024*1024)}MB limit"

        # Check file extension
        ext = Path(filename).suffix.lower()
        if ext not in self.ALLOWED_EXTENSIONS:
            return False, f"Invalid file type. Allowed types: {', '.join(self.ALLOWED_EXTENSIONS)}"

        # Validate it's a real image
        try:
            img = Image.open(io.BytesIO(file_data))
            img.verify()
            return True, ""
        except Exception as e:
            return False, f"Invalid image file: {str(e)}"

    def process_and_save_image(
        self,
        file_data: bytes,
        user_id: int,
        original_filename: str
    ) -> Optional[str]:
        """
        Process and save profile picture

        Args:
            file_data: Image file bytes
            user_id: User ID for filename generation
            original_filename: Original filename for extension

        Returns:
            Relative path to saved image or None if failed
        """
        try:
            # Open and process image
            img = Image.open(io.BytesIO(file_data))

            # Convert RGBA to RGB if necessary
            if img.mode in ('RGBA', 'LA', 'P'):
                # Create a white background
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')

            # Make square by cropping to center
            width, height = img.size
            if width != height:
                # Determine the size of the square
                size = min(width, height)
                # Calculate cropping box
                left = (width - size) // 2
                top = (height - size) // 2
                right = left + size
                bottom = top + size
                img = img.crop((left, top, right, bottom))

            # Resize if larger than target
            if img.size[0] > self.TARGET_SIZE[0]:
                img.thumbnail(self.TARGET_SIZE, Image.Resampling.LANCZOS)

            # Generate filename
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            ext = Path(original_filename).suffix.lower()
            filename = f"user_{user_id}_{timestamp}{ext}"
            filepath = self.upload_path / filename

            # Save optimized image
            if ext in ['.jpg', '.jpeg']:
                img.save(filepath, 'JPEG', quality=85, optimize=True)
            else:
                img.save(filepath, 'PNG', optimize=True)

            # Return relative path for database storage
            return f"profile_pictures/{filename}"

        except Exception as e:
            logger.exception("Error processing image: %s", e)
            return None

    def delete_old_picture(self, picture_path: Optional[str]) -> bool:
        """
        Delete old profile picture file

        Args:
            picture_path: Relative path to picture file

        Returns:
            True if deleted successfully or no file to delete
        """
        if not picture_path:
            return True

        try:
            # Construct full path
            full_path = Path(__file__).parent.parent.parent / "uploads" / picture_path
            if full_path.exists():
                full_path.unlink()
            return True
        except Exception as e:
            logger.exception("Error deleting old picture: %s", e)
            return False

    def get_full_url(self, picture_path: Optional[str], base_url: str) -> Optional[str]:
        """
        Get full URL for profile picture

        Args:
            picture_path: Relative path from database
            base_url: Base URL of the application

        Returns:
            Full URL to access the image
        """
        if not picture_path:
            return None

        # Ensure base_url doesn't end with /
        base = base_url.rstrip('/')
        return f"{base}/uploads/{picture_path}"

    def cleanup_orphaned_files(self, active_paths: list) -> int:
        """
        Clean up orphaned image files not in the database

        Args:
            active_paths: List of active picture paths from database

        Returns:
            Number of files deleted
        """
        deleted_count = 0
        active_set = set(active_paths)

        try:
            # List all files in upload directory
            for file_path in self.upload_path.glob("user_*"):
                relative_path = f"profile_pictures/{file_path.name}"
                if relative_path not in active_set:
                    file_path.unlink()
                    deleted_count += 1
        except Exception as e:
            logger.exception("Error during cleanup: %s", e)

        return deleted_count
