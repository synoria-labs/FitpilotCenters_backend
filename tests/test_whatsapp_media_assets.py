import pytest

from app.services.whatsapp_media_assets_service import (
    MediaAssetError,
    assert_asset_matches_header,
    validate_media,
)


def test_validate_media_accepts_supported_image():
    validate_media("image", b"fake-image-bytes", "image/png")


def test_validate_media_rejects_audio_as_image():
    with pytest.raises(MediaAssetError, match="MIME no soportado"):
        validate_media("image", b"fake-audio-bytes", "audio/mpeg")


def test_assert_asset_matches_header_rejects_audio_template_header():
    class Asset:
        media_kind = "audio"
        status = "active"

    with pytest.raises(MediaAssetError, match="requiere image"):
        assert_asset_matches_header(Asset(), "IMAGE")
