"""Dual-database engine management: Strict Read-Only HIS DB & Read-Write Immutable Audit DB."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from backend.config import get_settings
from database.audit_schema import AuditBase, create_audit_immutability_triggers

_his_engine: Engine | None = None
_his_engine_path: Path | None = None
_HISSessionLocal: sessionmaker[Session] | None = None

_audit_engine: Engine | None = None
_audit_engine_path: Path | None = None
_AuditSessionLocal: sessionmaker[Session] | None = None


def _build_ro_his_engine(his_path: Path) -> Engine:
    """Construct a SQLAlchemy engine strictly enforcing SQLite mode=ro + PRAGMA query_only=ON."""
    if not his_path.exists():
        raise FileNotFoundError(
            f"Mock HIS database not found at '{his_path}'. Run `python database/seed_his_db.py` first."
        )
    # Using Path.as_uri() properly percent-encodes spaces in directory names like 'ClearBill AI'
    ro_uri = f"{his_path.resolve().as_uri()}?mode=ro"

    def _ro_creator() -> sqlite3.Connection:
        conn = sqlite3.connect(ro_uri, uri=True, check_same_thread=False)
        conn.execute("PRAGMA query_only = ON;")
        return conn

    engine = create_engine(
        "sqlite://",
        creator=_ro_creator,
        poolclass=NullPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _enforce_query_only(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA query_only = ON;")
        cursor.close()

    return engine


def _build_audit_engine(audit_path: Path) -> Engine:
    """Construct the Read-Write Audit & Staging SQLite engine."""
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{audit_path.resolve()}",
        connect_args={"check_same_thread": False, "timeout": 15.0},
        poolclass=NullPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _configure_wal_and_timeout(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA busy_timeout=15000;")
        cursor.close()

    return engine


def get_his_engine() -> Engine:
    """Return the Read-Only HIS SQLAlchemy engine (rebuilds automatically if config path changes)."""
    global _his_engine, _his_engine_path, _HISSessionLocal
    his_path = get_settings().his_db_path.resolve()
    if _his_engine is None or _his_engine_path != his_path:
        if _his_engine is not None:
            _his_engine.dispose()
        _his_engine = _build_ro_his_engine(his_path)
        _his_engine_path = his_path
        _HISSessionLocal = sessionmaker(
            bind=_his_engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )
    return _his_engine


def get_audit_engine() -> Engine:
    """Return the Read-Write Audit SQLAlchemy engine (rebuilds automatically if config path changes)."""
    global _audit_engine, _audit_engine_path, _AuditSessionLocal
    audit_path = get_settings().audit_db_path.resolve()
    if _audit_engine is None or _audit_engine_path != audit_path:
        if _audit_engine is not None:
            _audit_engine.dispose()
        _audit_engine = _build_audit_engine(audit_path)
        _audit_engine_path = audit_path
        _AuditSessionLocal = sessionmaker(
            bind=_audit_engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )
    return _audit_engine


def init_audit_db() -> None:
    """Ensure Audit/Staging tables and SQLite immutability triggers exist."""
    engine = get_audit_engine()
    AuditBase.metadata.create_all(bind=engine)
    create_audit_immutability_triggers(engine)


def reset_db_caches() -> None:
    """Dispose cached engines (primarily used by tests when switching temporary DB paths)."""
    global _his_engine, _his_engine_path, _HISSessionLocal
    global _audit_engine, _audit_engine_path, _AuditSessionLocal
    if _his_engine is not None:
        _his_engine.dispose()
    if _audit_engine is not None:
        _audit_engine.dispose()
    _his_engine = None
    _his_engine_path = None
    _HISSessionLocal = None
    _audit_engine = None
    _audit_engine_path = None
    _AuditSessionLocal = None


def get_his_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a Read-Only HIS database session."""
    get_his_engine()
    assert _HISSessionLocal is not None
    session = _HISSessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_audit_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a ClearBill Audit/Staging database session."""
    get_audit_engine()
    assert _AuditSessionLocal is not None
    session = _AuditSessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def his_session_scope() -> Generator[Session, None, None]:
    """Context manager for direct Read-Only HIS access outside FastAPI routes."""
    yield from get_his_db()


@contextmanager
def audit_session_scope() -> Generator[Session, None, None]:
    """Context manager for ClearBill Audit DB operations outside FastAPI routes."""
    yield from get_audit_db()
