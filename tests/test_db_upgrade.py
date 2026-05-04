"""Tests for database upgrade helpers used by deployment startup paths."""

from __future__ import annotations


def test_upgrade_database_uses_plain_alembic_for_non_postgres(monkeypatch):
    from src import db_upgrade

    calls: list[str] = []

    monkeypatch.setattr(
        db_upgrade, "_resolve_database_url", lambda _: "sqlite:///tmp.db"
    )
    monkeypatch.setattr(
        db_upgrade,
        "_run_alembic_upgrade",
        lambda url: calls.append(url) or 0,
    )

    exit_code = db_upgrade.upgrade_database()

    assert exit_code == 0
    assert calls == ["sqlite:///tmp.db"]


def test_upgrade_database_uses_plain_alembic_for_managed_postgres(monkeypatch):
    from src import db_upgrade

    messages: list[str] = []
    calls: list[str] = []

    monkeypatch.setattr(
        db_upgrade, "_resolve_database_url", lambda _: "postgresql://example"
    )
    monkeypatch.setattr(db_upgrade, "_inspect_state", lambda url: (True, True))
    monkeypatch.setattr(
        db_upgrade,
        "_run_alembic_upgrade",
        lambda url: calls.append(url) or 0,
    )

    exit_code = db_upgrade.upgrade_database(status=messages.append)

    assert exit_code == 0
    assert calls == ["postgresql://example"]
    assert messages == [
        "Database is already Alembic-managed; applying any pending migrations.",
    ]


def test_upgrade_database_recovers_legacy_postgres(monkeypatch):
    from src import db_upgrade

    messages: list[str] = []
    dropped: list[str] = []
    restored: list[tuple[str, list[dict[str, object]]]] = []
    rows = [{"job_id": "abc", "created_at": 1.0}]

    monkeypatch.setattr(
        db_upgrade, "_resolve_database_url", lambda _: "postgresql://example"
    )
    monkeypatch.setattr(db_upgrade, "_inspect_state", lambda url: (True, False))
    monkeypatch.setattr(db_upgrade, "_load_rows", lambda url: rows)
    monkeypatch.setattr(
        db_upgrade,
        "_drop_legacy_tables",
        lambda url: dropped.append(url),
    )
    monkeypatch.setattr(db_upgrade, "_run_alembic_upgrade", lambda url: 0)
    monkeypatch.setattr(
        db_upgrade,
        "_restore_rows",
        lambda url, saved_rows: restored.append((url, saved_rows)),
    )

    exit_code = db_upgrade.upgrade_database(status=messages.append)

    assert exit_code == 0
    assert dropped == ["postgresql://example"]
    assert restored == [("postgresql://example", rows)]
    assert messages == [
        "Backing up 1 existing web_jobs row(s).",
        "Dropped legacy web_jobs table; recreating schema through Alembic.",
        "Restored 1 web_jobs row(s).",
    ]
