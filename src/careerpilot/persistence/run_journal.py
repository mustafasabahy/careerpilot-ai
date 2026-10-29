"""Optional owner-scoped execution metadata journal, separate from business records."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from careerpilot.application.action_contracts import ActionConflict, ActionDenied
from careerpilot.domain.reliability import RunRecord


class SQLiteRunJournal:
    def __init__(self, path: Path, owner_id: UUID) -> None:
        self.path, self.owner_id = path, owner_id
        with self._connection() as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables - {"reliability_runs"}:
                raise ValueError("Execution journal requires its own local file")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS reliability_runs("
                "id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,payload TEXT NOT NULL,"
                "cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)))"
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def begin(self, record: RunRecord) -> None:
        record = RunRecord.model_validate(record.model_dump())
        if record.owner_id != self.owner_id or record.status != "running":
            raise ActionDenied("Journal ownership/start state does not match")
        with self._connection() as connection:
            try:
                connection.execute(
                    "INSERT INTO reliability_runs(id,owner_id,payload) VALUES (?,?,?)",
                    (str(record.operation_id), str(self.owner_id), record.model_dump_json()),
                )
            except sqlite3.IntegrityError:
                raise ActionConflict(
                    "Operation ID already recorded; recover with a new invocation ID"
                ) from None

    def save(self, record: RunRecord) -> None:
        record = RunRecord.model_validate(record.model_dump())
        if record.owner_id != self.owner_id:
            raise ActionDenied("Journal ownership does not match")
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE reliability_runs SET payload=? WHERE id=? AND owner_id=?",
                (record.model_dump_json(), str(record.operation_id), str(self.owner_id)),
            )
            if cursor.rowcount != 1:
                raise ActionDenied("Journal operation is unavailable within this user's scope")

    def read(self, operation_id: UUID) -> RunRecord:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM reliability_runs WHERE id=? AND owner_id=?",
                (str(operation_id), str(self.owner_id)),
            ).fetchone()
        if row is None:
            raise ActionDenied("Journal operation is unavailable within this user's scope")
        record = RunRecord.model_validate_json(row[0])
        if record.owner_id != self.owner_id:
            raise ActionDenied("Invalid journal ownership")
        return record

    def cancelled(self, operation_id: UUID) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM reliability_runs WHERE id=? AND owner_id=?",
                (str(operation_id), str(self.owner_id)),
            ).fetchone()
        if row is None:
            raise ActionDenied("Journal operation is unavailable within this user's scope")
        return bool(row[0])

    def request_cancel(self, operation_id: UUID) -> bool:
        self.read(operation_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM reliability_runs WHERE id=? AND owner_id=?",
                (str(operation_id), str(self.owner_id)),
            ).fetchone()
            if RunRecord.model_validate_json(row[0]).status != "running":
                return False
            connection.execute(
                "UPDATE reliability_runs SET cancel_requested=1 WHERE id=? AND owner_id=?",
                (str(operation_id), str(self.owner_id)),
            )
        return True
