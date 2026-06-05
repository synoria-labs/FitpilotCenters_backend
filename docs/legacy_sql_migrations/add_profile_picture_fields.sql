-- Migration: Add profile picture fields to people table
-- Date: 2025-11-04
-- Description: Adds profile_picture_path and profile_picture_uploaded_at columns to support user avatars

-- Add columns to people table
ALTER TABLE app.people
ADD COLUMN IF NOT EXISTS profile_picture_path VARCHAR(255),
ADD COLUMN IF NOT EXISTS profile_picture_uploaded_at TIMESTAMP WITH TIME ZONE;

-- Add index for faster queries on users with profile pictures
CREATE INDEX IF NOT EXISTS idx_people_has_picture
ON app.people(id)
WHERE profile_picture_path IS NOT NULL;

-- Comment on columns for documentation
COMMENT ON COLUMN app.people.profile_picture_path IS 'Relative path to user profile picture file';
COMMENT ON COLUMN app.people.profile_picture_uploaded_at IS 'Timestamp when the profile picture was uploaded';