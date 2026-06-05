import os
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.core.env import load_environment

load_environment()

# Get database URL from environment
database_url = os.getenv("DATABASE_URL", "postgresql+asyncpg://appuser:secret123@localhost:5432/defaultdb")

# Configure SQL logging based on environment
sql_log_level = os.getenv("SQL_LOG_LEVEL", "INFO").upper()
enable_sql_echo = sql_log_level in ["DEBUG", "INFO"]

engine = create_async_engine(
    database_url,
    echo=enable_sql_echo,
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
