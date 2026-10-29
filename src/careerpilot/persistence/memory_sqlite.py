"""Separate local confirmed-memory database and thread ownership registry."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from careerpilot.application.execution import storage_timeout
from careerpilot.application.memory import MemoryConflict, MemoryDenied, MemoryRepository
from careerpilot.domain.memory import ExplicitCorrection, MemoryItem

_APPLICATION_ID = 0x43504D31

_SCHEMA = """
CREATE TABLE owners(owner_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE memories(
 owner_id TEXT NOT NULL REFERENCES owners(owner_id), id TEXT NOT NULL,
 version INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(owner_id,id));
CREATE TABLE workflow_threads(
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(owner_id),
 candidate_id TEXT, deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)));
"""


class SQLiteMemoryDatabase:
    def __init__(self, path: Path) -> None:
        if str(path) == ":memory:":
            raise ValueError("Use a file-backed local memory database")
        self.path = path.resolve()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=storage_timeout())
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN EXCLUSIVE")
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    tables = connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                    if tables:
                        raise ValueError("Refusing an unversioned existing memory database")
                    for statement in _SCHEMA.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                    connection.execute("PRAGMA user_version=1")
                elif (
                    version != 1
                    or connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID
                ):
                    raise ValueError("Unsupported memory database version")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @contextmanager
    def transaction(self) -> Iterator[MemoryRepository]:
        # Local short workflows serialize here across instances/processes. Never do
        # model/network calls under this reservation. Crash releases SQLite's lock.
        with self._connection() as connection:
            if (
                connection.execute("PRAGMA user_version").fetchone()[0] != 1
                or connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID
            ):
                raise ValueError("Memory database requires explicit initialization")
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield SQLiteMemoryRepository(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise


class SQLiteMemoryRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def _owner(self, owner_id: UUID) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO owners(owner_id) VALUES (?)", (str(owner_id),)
        )

    def _bump(self, owner_id: UUID) -> None:
        self.connection.execute(
            "UPDATE owners SET revision=revision+1 WHERE owner_id=?", (str(owner_id),)
        )

    def items(self, owner_id: UUID) -> tuple[MemoryItem, ...]:
        rows = self.connection.execute(
            "SELECT payload FROM memories WHERE owner_id=? ORDER BY id", (str(owner_id),)
        ).fetchall()
        items = tuple(MemoryItem.model_validate_json(row[0]) for row in rows)
        if any(item.owner_id != owner_id for item in items):
            raise MemoryDenied("Invalid memory ownership")
        return items

    def revision(self, owner_id: UUID) -> int:
        row = self.connection.execute(
            "SELECT revision FROM owners WHERE owner_id=?", (str(owner_id),)
        ).fetchone()
        return int(row[0]) if row else 0

    def put(self, owner_id: UUID, item: MemoryItem, expected_version: int) -> None:
        item = MemoryItem.model_validate(item.model_dump())
        if item.owner_id != owner_id or item.version != expected_version + 1:
            raise MemoryDenied("Invalid memory ownership/version")
        self._owner(owner_id)
        existing = {entry.id: entry for entry in self.items(owner_id)}
        current = existing.get(item.id)
        if (current.version if current else 0) != expected_version:
            raise MemoryConflict("Memory was changed; reload before editing")
        if current is not None and current.value.kind in {"career_fact", "correction"}:
            raise MemoryConflict("Use an explicit correction for an existing career fact")
        if (
            current is None
            and len(existing) >= 100
            and not isinstance(item.value, ExplicitCorrection)
        ):
            raise MemoryConflict("Local memory item limit reached")
        if isinstance(item.value, ExplicitCorrection):
            target = existing.get(item.value.replaces_id)
            if current is not None:
                raise MemoryConflict("Create a new correction instead of rewriting a correction")
            if (
                item.id == item.value.replaces_id
                or target is None
                or target.version != item.value.replaces_version
            ):
                raise MemoryConflict("Correction requires another existing fact")
            if target.value.kind not in {"career_fact", "correction"}:
                raise MemoryConflict("Correction target is not a career fact")
            self.connection.execute(
                "DELETE FROM memories WHERE owner_id=? AND id=?",
                (str(owner_id), str(target.id)),
            )
        self.connection.execute(
            "INSERT INTO memories(owner_id,id,version,payload) VALUES (?,?,?,?) "
            "ON CONFLICT(owner_id,id) DO UPDATE SET "
            "version=excluded.version,payload=excluded.payload",
            (str(owner_id), str(item.id), item.version, item.model_dump_json()),
        )
        self._bump(owner_id)

    def delete(self, owner_id: UUID, identifier: UUID, expected_version: int) -> None:
        cursor = self.connection.execute(
            "DELETE FROM memories WHERE owner_id=? AND id=? AND version=?",
            (str(owner_id), str(identifier), expected_version),
        )
        if cursor.rowcount != 1:
            raise MemoryConflict("Memory is absent or changed within this user's scope")
        self._bump(owner_id)

    def clear(self, owner_id: UUID) -> None:
        self._owner(owner_id)
        self.connection.execute("DELETE FROM memories WHERE owner_id=?", (str(owner_id),))
        self._bump(owner_id)

    def register_thread(self, owner_id: UUID, thread_id: UUID, candidate_id: UUID | None) -> None:
        self._owner(owner_id)
        try:
            self.connection.execute(
                "INSERT INTO workflow_threads(id,owner_id,candidate_id) VALUES (?,?,?)",
                (str(thread_id), str(owner_id), str(candidate_id) if candidate_id else None),
            )
        except sqlite3.IntegrityError:
            raise MemoryConflict("Thread ID already registered; use a new ID") from None

    def require_thread(self, owner_id: UUID, thread_id: UUID) -> UUID | None:
        row = self.connection.execute(
            "SELECT candidate_id FROM workflow_threads WHERE id=? AND owner_id=? AND deleted=0",
            (str(thread_id), str(owner_id)),
        ).fetchone()
        if row is None:
            raise MemoryDenied("Thread is unavailable within this user's scope")
        return UUID(row[0]) if row[0] else None

    def threads(self, owner_id: UUID) -> tuple[UUID, ...]:
        # Includes tombstones so a failed physical deletion can be retried safely.
        return tuple(
            UUID(row[0])
            for row in self.connection.execute(
                "SELECT id FROM workflow_threads WHERE owner_id=? ORDER BY id", (str(owner_id),)
            )
        )

    def mark_thread_deleted(self, owner_id: UUID, thread_id: UUID) -> None:
        cursor = self.connection.execute(
            "UPDATE workflow_threads SET deleted=1 WHERE id=? AND owner_id=?",
            (str(thread_id), str(owner_id)),
        )
        if cursor.rowcount != 1:
            raise MemoryDenied("Thread is unavailable within this user's scope")
