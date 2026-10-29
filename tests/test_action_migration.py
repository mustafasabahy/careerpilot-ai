"""Business schema v2 upgrade is additive, idempotent and atomically rolled back."""

import sqlite3
from pathlib import Path

import pytest
from test_preparation_migration import create_v1

from careerpilot.domain.applications import DocumentVersion
from careerpilot.persistence.schema import MIGRATION_1_TO_2, MIGRATION_2_TO_3, SCHEMA_VERSION
from careerpilot.persistence.sqlite import SQLiteDatabase


def create_v2(path: Path) -> DocumentVersion:
    _, _, version = create_v1(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(MIGRATION_1_TO_2)
        connection.execute("PRAGMA user_version=2")
    return version


def test_upgrade_v2_to_v3_preserves_original_documents_and_payloads(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.sqlite3"
    original = create_v2(path)
    database = SQLiteDatabase(path)
    database.initialize()
    database.initialize()
    with database.transaction() as repository:
        assert repository.get(DocumentVersion, original.id) == original
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert connection.execute("SELECT COUNT(*) FROM action_proposals").fetchone()[0] == 0
        assert (
            connection.execute("SELECT payload FROM versions").fetchone()[0]
            == original.model_dump_json()
        )


def test_failed_v2_upgrade_rolls_back_all_new_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rollback.sqlite3"
    original = create_v2(path)
    monkeypatch.setattr(
        "careerpilot.persistence.sqlite.MIGRATION_2_TO_3", MIGRATION_2_TO_3 + "BROKEN SQL;"
    )
    with pytest.raises(sqlite3.OperationalError):
        SQLiteDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            connection.execute("SELECT payload FROM versions").fetchone()[0]
            == original.model_dump_json()
        )
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'action_%'"
        ).fetchall()
