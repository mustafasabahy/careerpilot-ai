"""Versioned transcript store; separate from checkpoints, confirmed memory and domain SQL."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID

from careerpilot.application.conversations import (
    Card,
    Conversation,
    ConversationRepository,
    Message,
    Operation,
)
from careerpilot.application.execution import storage_timeout
from careerpilot.application.security import SecurityDenied
from careerpilot.domain.common import utc_now

_SCHEMA = """
CREATE TABLE conversations(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX conversations_owner ON conversations(owner_id);
CREATE TABLE messages(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL
 REFERENCES conversations(id) ON DELETE CASCADE, sequence INTEGER NOT NULL CHECK(sequence>0),
 role TEXT NOT NULL CHECK(role IN ('user','assistant')), payload TEXT NOT NULL,
 UNIQUE(conversation_id,sequence));
CREATE TABLE operations(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL
 REFERENCES conversations(id) ON DELETE CASCADE, request_key TEXT NOT NULL, payload TEXT NOT NULL,
 UNIQUE(conversation_id,request_key));
"""


class ConversationDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=storage_timeout())
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN EXCLUSIVE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise ValueError("Unsupported conversation schema")
            if version == 0:
                if connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall():
                    raise ValueError("Refusing an unversioned existing conversation database")
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute("PRAGMA user_version=1")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[ConversationRepository]:
        connection = self._connect()
        try:
            if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("Run explicit storage migration to initialize chat")
            connection.execute("BEGIN IMMEDIATE")
            yield SQLiteConversationRepository(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


class SQLiteConversationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create(self, value: Conversation) -> None:
        self.connection.execute(
            "INSERT INTO conversations VALUES (?,?,?)",
            (str(value.id), str(value.owner_id), value.model_dump_json()),
        )

    def get(self, owner: UUID, identifier: UUID) -> Conversation:
        row = self.connection.execute(
            "SELECT payload FROM conversations WHERE id=? AND owner_id=?",
            (str(identifier), str(owner)),
        ).fetchone()
        if row is None:
            raise SecurityDenied("Conversation unavailable")
        return Conversation.model_validate_json(row[0])

    def save(self, value: Conversation) -> None:
        self.get(value.owner_id, value.id)
        self.connection.execute(
            "UPDATE conversations SET payload=? WHERE id=? AND owner_id=?",
            (value.model_dump_json(), str(value.id), str(value.owner_id)),
        )

    def list(self, owner: UUID, offset: int, limit: int) -> tuple[Conversation, ...]:
        rows = self.connection.execute(
            "SELECT payload FROM conversations WHERE owner_id=? "
            "ORDER BY rowid DESC LIMIT ? OFFSET ?",
            (str(owner), limit, offset),
        )
        return tuple(Conversation.model_validate_json(row[0]) for row in rows)

    def messages(self, identifier: UUID, offset: int, limit: int) -> tuple[Message, ...]:
        rows = self.connection.execute(
            "SELECT payload FROM messages WHERE conversation_id=? "
            "ORDER BY sequence LIMIT ? OFFSET ?",
            (str(identifier), limit, offset),
        )
        return tuple(Message.model_validate_json(row[0]) for row in rows)

    def append(
        self,
        identifier: UUID,
        role: Literal["user", "assistant"],
        content: str,
        card: Card | None = None,
        *,
        cards: tuple[Card, ...] = (),
        mode: Literal["demo", "llm"] = "demo",
    ) -> Message:
        number = self.connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM messages WHERE conversation_id=?",
            (str(identifier),),
        ).fetchone()[0]
        value = Message(
            conversation_id=identifier,
            sequence=number,
            role=role,
            content=content,
            card=card,
            cards=cards,
            mode=mode,
        )
        self.connection.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?)",
            (str(value.id), str(identifier), number, role, value.model_dump_json()),
        )
        return value

    def operation(self, identifier: UUID, key: UUID) -> Operation | None:
        row = self.connection.execute(
            "SELECT payload FROM operations WHERE conversation_id=? AND request_key=?",
            (str(identifier), str(key)),
        ).fetchone()
        return Operation.model_validate_json(row[0]) if row else None

    def operations(self, identifier: UUID) -> tuple[Operation, ...]:
        return tuple(
            Operation.model_validate_json(row[0])
            for row in self.connection.execute(
                "SELECT payload FROM operations WHERE conversation_id=? ORDER BY rowid",
                (str(identifier),),
            )
        )

    def put_operation(self, value: Operation) -> None:
        previous = self.operation(value.conversation_id, value.request.idempotency_key)
        if previous and (
            previous.id != value.id
            or previous.context != value.context
            or previous.request != value.request
            or previous.command != value.command
            or previous.mode != value.mode
            or (
                previous.claim_expires_at is not None
                and previous.claim_expires_at != value.claim_expires_at
                and previous.claim_expires_at > utc_now()
                and previous.status != "recoverable"
            )
            or previous.created_at != value.created_at
            or (previous.status not in {"running", "recoverable"} and previous != value)
            or value.status not in {"running", "completed", "failed", "unknown", "recoverable"}
        ):
            raise ValueError("Invalid operation transition")
        self.connection.execute(
            "INSERT INTO operations VALUES (?,?,?,?) ON CONFLICT(id) "
            "DO UPDATE SET payload=excluded.payload",
            (
                str(value.id),
                str(value.conversation_id),
                str(value.request.idempotency_key),
                json.dumps(
                    {**value.model_dump(mode="json"), "agent_progress": value.agent_progress}
                ),
            ),
        )

    def delete(self, owner: UUID, identifier: UUID) -> None:
        self.get(owner, identifier)
        self.connection.execute(
            "DELETE FROM conversations WHERE id=? AND owner_id=?", (str(identifier), str(owner))
        )
