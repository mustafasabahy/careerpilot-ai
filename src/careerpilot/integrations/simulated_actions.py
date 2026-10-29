"""Explicit simulated effects with their own durable idempotency ledger; no network."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid5

from careerpilot.application.action_contracts import (
    AdapterReceipt,
    AmbiguousExecution,
    DefiniteAdapterFailure,
)
from careerpilot.application.execution import storage_timeout
from careerpilot.domain.actions import ActionProposal, ChangeApplication


class SimulatedActionAdapter:
    identifier = "simulated-actions-v1"
    enabled = True

    def __init__(
        self,
        ledger_path: Path,
        *,
        behavior: Literal["success", "ack_lost", "failure"] = "success",
    ) -> None:
        self.path = ledger_path
        self.behavior = behavior
        with self._connection() as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables - {"simulated_effects"}:
                raise ValueError("Simulator requires a dedicated local ledger")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS simulated_effects("
                "idempotency_key TEXT PRIMARY KEY, proposal_id TEXT NOT NULL,"
                "content_hash TEXT NOT NULL, receipt_id TEXT NOT NULL)"
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=storage_timeout())
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def execute(
        self, proposal: ActionProposal, idempotency_key: str, *, timeout_seconds: float = 30
    ) -> AdapterReceipt:
        if timeout_seconds <= 0:
            raise DefiniteAdapterFailure("No execution time remains")
        if isinstance(proposal.content, ChangeApplication):
            raise DefiniteAdapterFailure("Local record changes are not adapter effects")
        if self.behavior == "failure":
            raise DefiniteAdapterFailure("Synthetic failure before any effect")
        receipt_id = str(uuid5(proposal.id, "simulated-effect"))
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT proposal_id,content_hash,receipt_id FROM simulated_effects "
                "WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                if row[:2] != (str(proposal.id), proposal.content_sha256):
                    raise DefiniteAdapterFailure("Idempotency key conflicts")
                receipt_id = row[2]
            else:
                connection.execute(
                    "INSERT INTO simulated_effects VALUES (?,?,?,?)",
                    (idempotency_key, str(proposal.id), proposal.content_sha256, receipt_id),
                )
        if self.behavior == "ack_lost":
            raise AmbiguousExecution("Synthetic effect persisted but acknowledgement lost")
        return AdapterReceipt(receipt_id=receipt_id)

    def effect_count(self) -> int:
        with self._connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM simulated_effects").fetchone()[0])

    def receipt(self, idempotency_key: str) -> UUID | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT receipt_id FROM simulated_effects WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        return UUID(row[0]) if row else None
