"""Hashed local credentials and per-resource ownership; never model supplied grants."""

import hashlib
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from careerpilot.application.security import ResourceKind, SecurityDenied, UserIdentity
from careerpilot.domain.common import utc_now


class SecurityStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        with self.connection() as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables - {"access_tokens", "resource_owners"}:
                raise ValueError("Security store requires a separate file")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS access_tokens("
                "hash TEXT PRIMARY KEY,owner_id TEXT NOT NULL,expires_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS resource_owners("
                "kind TEXT NOT NULL,id TEXT NOT NULL,owner_id TEXT NOT NULL,"
                "root_id TEXT,deleted INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(kind,id))"
            )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def issue(self, owner_id: UUID, *, hours: int = 8) -> str:
        if not 1 <= hours <= 24:
            raise ValueError("Token lifetime must be 1–24 hours")
        token = secrets.token_urlsafe(32)
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO access_tokens VALUES (?,?,?)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    str(owner_id),
                    (utc_now() + timedelta(hours=hours)).isoformat(),
                ),
            )
        return token

    def authenticate(self, token: str) -> UserIdentity:
        if not 32 <= len(token) <= 256:
            raise SecurityDenied("Authentication required")
        with self.connection() as connection:
            row = connection.execute(
                "SELECT owner_id,expires_at FROM access_tokens WHERE hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
        if row is None or row[1] <= utc_now().isoformat():
            raise SecurityDenied("Authentication required")
        return UserIdentity(id=UUID(row[0]))

    def revoke_tokens(self, owner_id: UUID) -> None:
        with self.connection() as connection:
            connection.execute("DELETE FROM access_tokens WHERE owner_id=?", (str(owner_id),))

    def bind(
        self, owner: UUID, kind: ResourceKind, identifier: UUID, root: UUID | None = None
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO resource_owners(kind,id,owner_id,root_id) VALUES (?,?,?,?)",
                (kind, str(identifier), str(owner), str(root) if root else None),
            )
        self.require(owner, kind, identifier)

    def require(
        self, owner: UUID, kind: ResourceKind, identifier: UUID, *, include_deleted: bool = False
    ) -> None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM resource_owners WHERE kind=? AND id=? "
                "AND owner_id=? AND (deleted=0 OR ?)",
                (kind, str(identifier), str(owner), include_deleted),
            ).fetchone()
        if row is None:
            raise SecurityDenied("Resource is unavailable within this account")

    def list(
        self, owner: UUID, kind: ResourceKind, *, offset: int = 0, limit: int = 20
    ) -> tuple[UUID, ...]:
        if not 0 <= offset or not 1 <= limit <= 50:
            raise ValueError("Invalid pagination")
        with self.connection() as connection:
            return tuple(
                UUID(row[0])
                for row in connection.execute(
                    "SELECT id FROM resource_owners WHERE owner_id=? AND kind=? "
                    "AND deleted=0 ORDER BY rowid LIMIT ? OFFSET ?",
                    (str(owner), kind, limit, offset),
                )
            )

    def revoke_resource(self, owner: UUID, kind: ResourceKind, identifier: UUID) -> None:
        self.require(owner, kind, identifier)
        with self.connection() as connection:
            connection.execute(
                "UPDATE resource_owners SET deleted=1 WHERE owner_id=? AND kind=? AND id=?",
                (str(owner), kind, str(identifier)),
            )

    def revoke_group(self, owner: UUID, root: UUID) -> None:
        self.require(owner, "candidate", root, include_deleted=True)
        with self.connection() as connection:
            connection.execute(
                "UPDATE resource_owners SET deleted=1 WHERE owner_id=? "
                "AND (root_id=? OR (kind='candidate' AND id=?))",
                (str(owner), str(root), str(root)),
            )
