"""Persistent storage for writer (user) records."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Boolean, Column, Float, String, Table, Text
from sqlalchemy import create_engine, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from config.settings import load_config
from src.job_store import _metadata


_writers_table = Table(
    "writers",
    _metadata,
    Column("id", String(32), primary_key=True),
    Column("email", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("is_active", Boolean, nullable=False, server_default="1"),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)


@dataclass
class Writer:
    id: str
    email: str
    name: str
    is_active: bool = True
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "is_active": self.is_active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _normalize_email(email: str) -> str:
    return email.strip().lower()


class WriterStore:
    """SQL-backed store for writer records."""

    def __init__(self) -> None:
        self._engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None
        self._engine_url: str | None = None

    def _ensure_session_factory(self) -> sessionmaker[Session]:
        config = load_config()
        url = config.database.url
        if self._session_factory is not None and self._engine_url == url:
            return self._session_factory

        if self._engine is not None:
            self._engine.dispose()

        connect_args: dict[str, Any] = {}
        engine_kwargs: dict[str, Any] = {
            "future": True,
            "echo": config.database.echo,
            "connect_args": connect_args,
        }
        if url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        else:
            connect_args["connect_timeout"] = 10
            engine_kwargs["pool_pre_ping"] = True
            engine_kwargs["pool_recycle"] = 300

        self._engine = create_engine(url, **engine_kwargs)
        self._engine_url = url
        self._session_factory = sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            future=True,
        )
        return self._session_factory

    def _session(self) -> Session:
        return self._ensure_session_factory()()

    def find_or_create(self, email: str, name: str) -> Writer:
        """Find existing writer by email or create a new one."""
        normalized = _normalize_email(email)
        if not normalized:
            raise ValueError("Email cannot be empty")

        with self._session() as session:
            row = (
                session.execute(
                    select(_writers_table).where(_writers_table.c.email == normalized)
                )
                .mappings()
                .one_or_none()
            )
            if row is not None:
                # Update name if changed
                if row["name"] != name.strip() and name.strip():
                    now = time.time()
                    session.execute(
                        update(_writers_table)
                        .where(_writers_table.c.id == row["id"])
                        .values(name=name.strip(), updated_at=now)
                    )
                    session.commit()
                    return Writer(
                        id=row["id"],
                        email=row["email"],
                        name=name.strip(),
                        is_active=bool(row["is_active"]),
                        created_at=float(row["created_at"]),
                        updated_at=now,
                    )
                return self._hydrate(row)

            now = time.time()
            writer_id = uuid.uuid4().hex[:32]
            record = {
                "id": writer_id,
                "email": normalized,
                "name": name.strip() or normalized,
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
            session.execute(insert(_writers_table).values(**record))
            session.commit()
            return Writer(**record)

    def get_by_id(self, writer_id: str) -> Writer | None:
        with self._session() as session:
            row = (
                session.execute(
                    select(_writers_table).where(_writers_table.c.id == writer_id)
                )
                .mappings()
                .one_or_none()
            )
        return self._hydrate(row) if row is not None else None

    def get_by_email(self, email: str) -> Writer | None:
        normalized = _normalize_email(email)
        with self._session() as session:
            row = (
                session.execute(
                    select(_writers_table).where(_writers_table.c.email == normalized)
                )
                .mappings()
                .one_or_none()
            )
        return self._hydrate(row) if row is not None else None

    def list_all(self, *, active_only: bool = True) -> list[Writer]:
        query = select(_writers_table).order_by(
            _writers_table.c.name,
            _writers_table.c.email,
        )
        if active_only:
            query = query.where(_writers_table.c.is_active == True)  # noqa: E712
        with self._session() as session:
            rows = session.execute(query).mappings().all()
        return [self._hydrate(row) for row in rows]

    @staticmethod
    def _hydrate(row) -> Writer:
        return Writer(
            id=row["id"],
            email=row["email"],
            name=row["name"],
            is_active=bool(row["is_active"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )


writer_store = WriterStore()
