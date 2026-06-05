"""Environment loading helpers."""
from pathlib import Path


_LOADED = False


def load_environment() -> None:
    """Load backend/.env once, without overriding real environment variables."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True

    try:
        from dotenv import load_dotenv

        env_path = Path(__file__).resolve().parents[2] / ".env"
        load_dotenv(env_path, override=False, encoding="utf-8")
    except Exception:
        # Environment variables may also be supplied by systemd/container runtime.
        pass
