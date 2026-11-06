"""Database engine and session management (SQLite by default, Postgres via DATABASE_URL)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

_engine = None
_SessionLocal = None


def get_engine(url: str | None = None):
    global _engine, _SessionLocal
    if _engine is None or url is not None:
        url = url or settings.database_url
        connect_args = {}
        if url.startswith("sqlite"):
            db_path = url.replace("sqlite:///", "")
            if db_path and db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            connect_args = {"check_same_thread": False}
        engine = create_engine(url, connect_args=connect_args, future=True)
        if url.startswith("sqlite"):

            @event.listens_for(engine, "connect")
            def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial
                dbapi_conn.execute("PRAGMA foreign_keys=ON")
                dbapi_conn.execute("PRAGMA journal_mode=WAL")

        _engine = engine
        _SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return _engine


def init_db(url: str | None = None) -> None:
    from . import models  # noqa: F401  (registers tables)

    engine = get_engine(url)
    models.Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    get_engine()
    session = _SessionLocal()
    try:
        yield session
    finally:
        session.close()
