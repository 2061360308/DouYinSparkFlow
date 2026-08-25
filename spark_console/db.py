from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session

from spark_console.config import Settings
from spark_console.models import Base, WorkerLock


def create_engine_for(settings: Settings) -> Engine:
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
    )
    if settings.database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def set_sqlite_pragmas(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()
    return engine


def create_schema(engine: Engine) -> None:
    """Create all declared tables additively for fresh and existing databases."""
    Base.metadata.create_all(engine)
    with session_scope(engine) as session:
        if session.get(WorkerLock, 1) is None:
            session.add(WorkerLock(id=1))


@contextmanager
def session_scope(engine: Engine):
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
