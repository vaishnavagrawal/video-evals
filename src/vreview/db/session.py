"""Database session management (SQLAlchemy 2.x sync engine with psycopg 3)."""
from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from vreview.config import settings


@lru_cache
def get_engine(database_url: str | None = None) -> Engine:
    url = database_url or settings().database_url
    # connect_args for psycopg if needed, pool_pre_ping for resilient connections
    connect_args = {}
    if "sqlite" in url:
        connect_args["check_same_thread"] = False
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


@lru_cache
def get_sessionmaker(database_url: str | None = None) -> sessionmaker[Session]:
    engine = get_engine(database_url)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a database session."""
    session_factory = get_sessionmaker()
    with session_factory() as session:
        yield session
