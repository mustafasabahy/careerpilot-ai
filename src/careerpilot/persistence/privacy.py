"""Explicit local retention/deletion. Revoke access before destructive writes."""

from uuid import UUID

from careerpilot.application.security import RetentionConflict
from careerpilot.domain.applications import ApplicationDocument
from careerpilot.persistence.security import SecurityStore
from careerpilot.persistence.sqlite import SQLiteDatabase, SQLiteRepository


def purge_candidate(
    database: SQLiteDatabase, security: SecurityStore, owner: UUID, candidate: UUID
) -> None:
    security.require(owner, "candidate", candidate, include_deleted=True)
    with database.transaction() as repository:
        if not isinstance(repository, SQLiteRepository):
            raise TypeError("Local privacy adapter requires SQLite")
        connection = repository._connection
        if connection.execute(
            "SELECT 1 FROM action_proposals WHERE candidate_id=?", (str(candidate),)
        ).fetchone():
            raise RetentionConflict("Sensitive action audit retention blocks destructive deletion")
        security.revoke_group(owner, candidate)
        connection.execute(
            "DELETE FROM audit_events WHERE application_id IN "
            "(SELECT id FROM applications WHERE candidate_id=?)",
            (str(candidate),),
        )
        for table in (
            "approvals",
            "applications",
            "preparations",
            "assessments",
            "versions",
            "documents",
        ):
            connection.execute(f"DELETE FROM {table} WHERE candidate_id=?", (str(candidate),))
        connection.execute("DELETE FROM candidates WHERE id=?", (str(candidate),))


def purge_document(
    database: SQLiteDatabase, security: SecurityStore, owner: UUID, identifier: UUID
) -> None:
    security.require(owner, "document", identifier)
    with database.transaction() as repository:
        document = repository.get(ApplicationDocument, identifier)
        security.require(owner, "candidate", document.candidate_id)
        if not isinstance(repository, SQLiteRepository):
            raise TypeError("Local privacy adapter requires SQLite")
        connection = repository._connection
        references = connection.execute(
            "SELECT 1 FROM versions v WHERE v.document_id=? AND (EXISTS "
            "(SELECT 1 FROM applications a WHERE a.document_version_id=v.id) OR EXISTS "
            "(SELECT 1 FROM preparations p WHERE p.resume_version_id=v.id OR "
            "p.cover_version_id=v.id OR p.original_resume_version_id=v.id))",
            (str(identifier),),
        ).fetchone()
        if references:
            raise RetentionConflict("Referenced document versions cannot be destructively deleted")
        security.revoke_resource(owner, "document", identifier)
        for row in connection.execute(
            "SELECT id FROM versions WHERE document_id=?", (str(identifier),)
        ):
            security.revoke_resource(owner, "version", UUID(row[0]))
        connection.execute("DELETE FROM versions WHERE document_id=?", (str(identifier),))
        connection.execute("DELETE FROM documents WHERE id=?", (str(identifier),))
