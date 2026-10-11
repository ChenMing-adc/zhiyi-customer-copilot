from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import hashlib
import json

from sqlalchemy import JSON, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "service_sources"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    sheet: Mapped[str] = mapped_column(String, index=True)
    session_id: Mapped[str] = mapped_column(String, index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    provenance: Mapped[dict] = mapped_column(JSON)


class Record(Base):
    """Versioned demo entities. Updates use optimistic compare-and-swap."""
    __tablename__ = "service_records"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    session_id: Mapped[str] = mapped_column(String, index=True, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict] = mapped_column(JSON)


class Audit(Base):
    __tablename__ = "service_audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    target: Mapped[str] = mapped_column(String)
    detail: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=lambda: datetime.now().isoformat())


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{path.as_posix()}", connect_args={"check_same_thread": False, "timeout": 20})
        @event.listens_for(self.engine, "connect")
        def pragmas(connection, _):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def transaction(self):
        with self.factory.begin() as db:
            yield db


def audit(db, actor, action, target, **detail):
    db.add(Audit(actor=actor, action=action, target=target, detail=detail))
