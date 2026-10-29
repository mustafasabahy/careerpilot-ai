"""Managed local LangGraph checkpoint resources, separate from business/memory DBs."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from careerpilot.application.execution import storage_timeout


def isolated_checkpointer() -> InMemorySaver:
    return InMemorySaver(
        serde=JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=[])
    )


@contextmanager
def local_checkpointer(path: Path, *, separate_from: tuple[Path, ...]) -> Iterator[SqliteSaver]:
    resolved = path.resolve()
    if str(path) == ":memory:" or any(
        resolved == other.resolve()
        or (resolved.exists() and other.exists() and resolved.samefile(other))
        for other in separate_from
    ):
        raise ValueError("Checkpoints require a separate file from business and memory data")
    connection = sqlite3.connect(resolved, check_same_thread=False, timeout=storage_timeout())
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if (
            connection.execute("PRAGMA user_version").fetchone()[0] != 0
            or connection.execute("PRAGMA application_id").fetchone()[0] != 0
            or tables - {"checkpoints", "writes"}
        ):
            raise ValueError("Refusing a database that is not a checkpoint store")
        yield SqliteSaver(
            connection,
            serde=JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=[]),
        )
    finally:
        connection.close()
