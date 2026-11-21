"""Database engine and session factory (PostgreSQL via psycopg 3)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def normalize_db_url(url: str) -> str:
    # Render and most hosts hand out postgres:// or postgresql:// URLs; use the psycopg 3 driver.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(normalize_db_url(get_settings().database_url), pool_pre_ping=True, future=True)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, autoflush=True)
    return _engine


def reset_engine() -> None:
    """Drop the cached engine (tests switch databases)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine, _SessionLocal = None, None


def SessionLocal() -> Session:  # noqa: N802 - factory used like a class
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


@contextmanager
def session_scope() -> Iterator[Session]:
    """A session that commits on success and rolls back on error (used by background jobs)."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def advisory_xact_lock(session: Session, namespace: int, key: int) -> None:
    """Block until we hold a transaction-scoped Postgres advisory lock."""
    session.execute(text("SELECT pg_advisory_xact_lock(:a, :b)"), {"a": namespace, "b": key})


def try_advisory_lock(session: Session, namespace: int, key: int) -> bool:
    """Try to take a session-scoped advisory lock without waiting."""
    return bool(session.execute(text("SELECT pg_try_advisory_lock(:a, :b)"), {"a": namespace, "b": key}).scalar())


def advisory_unlock(session: Session, namespace: int, key: int) -> None:
    session.execute(text("SELECT pg_advisory_unlock(:a, :b)"), {"a": namespace, "b": key})


# Advisory lock namespaces
LOCK_ITEM_SYNC = 1001
LOCK_WALLET_DEDUPE = 1002
LOCK_MATCHING = 1003
