import os
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.core.env import load_environment

load_environment()

# Get database URL from environment. No fallback: refuse to start pointing at a
# hardcoded default DB, which would mask a misconfigured deployment and embeds a
# credential in source.
database_url = os.getenv("DATABASE_URL")
if not database_url:
    raise RuntimeError(
        "DATABASE_URL must be set. Refusing to start without an explicit database URL."
    )

# SQL echo is off unless SQL_LOG_LEVEL is explicitly DEBUG. Echoing statements
# logs bound parameters (member PII, password hashes) and adds per-request I/O.
sql_log_level = os.getenv("SQL_LOG_LEVEL", "WARNING").upper()
enable_sql_echo = sql_log_level == "DEBUG"

engine = create_async_engine(
    database_url,
    echo=enable_sql_echo,
    hide_parameters=not enable_sql_echo,  # never log bound params outside debug
    pool_pre_ping=True,  # Verify connections before using them
    pool_recycle=3600,    # Recycle connections every hour
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# Alias for compatibility with new job system
async_session_factory = SessionLocal


class Base(DeclarativeBase):
    metadata = MetaData(schema="app")
    pass


async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session
