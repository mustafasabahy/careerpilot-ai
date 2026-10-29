"""Explicit local transactions, immutable inserts, and guarded lifecycle updates."""

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from pydantic import TypeAdapter

from careerpilot.application.execution import storage_timeout
from careerpilot.application.storage import (
    CareerRepository,
    DuplicateJob,
    IntegrityViolation,
    NotFound,
    Stored,
    StoredT,
)
from careerpilot.domain.applications import (
    ApplicationDocument,
    ApplicationStatus,
    DocumentKind,
    DocumentVersion,
    JobApplication,
)
from careerpilot.domain.common import Entity
from careerpilot.domain.governance import ApprovalRequest, ApprovalStatus, AuditEvent
from careerpilot.domain.jobs import JobFitAssessment, JobPosting, JobSource
from careerpilot.domain.preparation import PreparationRecord
from careerpilot.domain.profile import CandidateProfile
from careerpilot.persistence.schema import (
    ACTION_IMMUTABILITY_TRIGGERS,
    MIGRATION_1_TO_2,
    MIGRATION_2_TO_3,
    SCHEMA,
    SCHEMA_VERSION,
)

_TABLES: dict[type[Entity], str] = {
    CandidateProfile: "candidates",
    JobPosting: "jobs",
    JobFitAssessment: "assessments",
    ApplicationDocument: "documents",
    DocumentVersion: "versions",
    JobApplication: "applications",
    ApprovalRequest: "approvals",
    AuditEvent: "audit_events",
    PreparationRecord: "preparations",
}


class SQLiteDatabase:
    """File-backed development database. Call initialize before transaction()."""

    def __init__(self, path: Path) -> None:
        if str(path) == ":memory:":
            raise ValueError("Use a file path: each transaction owns its connection")
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=storage_timeout())
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        """Create v3 or migrate v1/v2 atomically; reject unknown versions without changes."""
        connection = self._connect()
        try:
            connection.execute("BEGIN EXCLUSIVE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                connection.commit()
                return
            if version not in {0, 1, 2}:
                raise IntegrityViolation("Unsupported database schema version")
            if version == 0:
                tables = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if tables:
                    raise IntegrityViolation(
                        "Refusing to initialize an unversioned existing database"
                    )
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        connection.execute(statement)
            if version < 2:
                for statement in MIGRATION_1_TO_2.split(";"):
                    if statement.strip():
                        connection.execute(statement)
            for statement in MIGRATION_2_TO_3.split(";"):
                if statement.strip():
                    connection.execute(statement)
            for statement in ACTION_IMMUTABILITY_TRIGGERS:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[CareerRepository]:
        """Commit on normal exit; roll back every failure. Sessions cannot outlive the block."""
        connection = self._connect()
        try:
            if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise IntegrityViolation("Initialize a supported database before use")
            connection.execute("BEGIN IMMEDIATE")
            yield SQLiteRepository(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


class SQLiteRepository:
    """Transaction-scoped implementation; callers obtain it from SQLiteDatabase."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self, model: type[StoredT], identifier: UUID) -> StoredT:
        table = _TABLES[model]
        row = self._connection.execute(
            f"SELECT payload FROM {table} WHERE id = ?", (str(identifier),)
        ).fetchone()
        if row is None:
            raise NotFound(f"{table}: {identifier}")
        return TypeAdapter(model).validate_json(row[0])

    def find_duplicate_jobs(self, source: JobSource) -> tuple[JobPosting, ...]:
        rows = self._connection.execute(
            "SELECT payload FROM jobs WHERE (source_name = ? AND external_id = ?) "
            "OR canonical_url = ? ORDER BY id",
            (source.name, source.external_id, source.canonical_url),
        ).fetchall()
        return tuple(JobPosting.model_validate_json(row[0]) for row in rows)

    @contextmanager
    def _write(self) -> Iterator[None]:
        # A caught error must not leave half an operation (e.g. update without audit).
        if not self._connection.in_transaction:
            raise RuntimeError("Repository writes require an explicit transaction")
        self._connection.execute("SAVEPOINT repository_operation")
        try:
            yield
            self._connection.execute("RELEASE repository_operation")
        except BaseException:
            self._connection.execute("ROLLBACK TO repository_operation")
            self._connection.execute("RELEASE repository_operation")
            raise

    def add(self, entity: Stored) -> None:
        with self._write():
            self._add(entity)

    def _add(self, entity: Stored) -> None:
        # Revalidate even instances made using Pydantic's deliberately unsafe copy/construct APIs.
        entity = type(entity).model_validate(entity.model_dump(warnings=False))
        table = _TABLES[type(entity)]
        columns: dict[str, str | int | None] = {
            "id": str(entity.id),
            "payload": entity.model_dump_json(),
        }
        if isinstance(entity, JobPosting):
            if self.find_duplicate_jobs(entity.source):
                raise DuplicateJob("Job source identifier or canonical URL already exists")
            columns.update(
                source_name=entity.source.name,
                external_id=entity.source.external_id,
                canonical_url=entity.source.canonical_url,
            )
        elif isinstance(entity, (ApplicationDocument, JobFitAssessment, JobApplication)):
            columns["candidate_id"] = str(entity.candidate_id)
            if isinstance(entity, (JobFitAssessment, JobApplication)):
                columns["job_id"] = str(entity.job_id)
            if isinstance(entity, JobFitAssessment):
                entity.decision(self.get(JobPosting, entity.job_id))
            if isinstance(entity, JobApplication):
                if entity.status != ApplicationStatus.DRAFT:
                    raise IntegrityViolation("New applications must start in draft")
                columns.update(
                    document_version_id=_optional_id(entity.document_version_id),
                    status=entity.status.value,
                )
        elif isinstance(entity, DocumentVersion):
            document = self.get(ApplicationDocument, entity.document_id)
            if entity.previous_version_id is not None:
                previous = self.get(DocumentVersion, entity.previous_version_id)
                try:
                    entity.validate_predecessor(previous)
                except ValueError:
                    raise IntegrityViolation("Invalid document version lineage") from None
            columns.update(
                document_id=str(entity.document_id),
                candidate_id=str(document.candidate_id),
                number=entity.number,
                previous_version_id=_optional_id(entity.previous_version_id),
            )
        elif isinstance(entity, ApprovalRequest):
            if entity.status != ApprovalStatus.PENDING:
                raise IntegrityViolation("New approvals must start pending")
            application = self.get(JobApplication, entity.application_id)
            if application.document_version_id != entity.document_version_id:
                raise IntegrityViolation(
                    "Approval must refer to the application's selected version"
                )
            columns.update(
                application_id=str(entity.application_id),
                document_version_id=str(entity.document_version_id),
                candidate_id=str(application.candidate_id),
            )
        elif isinstance(entity, AuditEvent):
            columns["application_id"] = str(entity.application_id)
        elif isinstance(entity, PreparationRecord):
            versions = {}
            for metadata in entity.documents:
                version = self.get(DocumentVersion, metadata.version_id)
                document = self.get(ApplicationDocument, version.document_id)
                if (
                    document.candidate_id != entity.candidate_id
                    or document.kind != metadata.kind
                    or document.id != metadata.document_id
                    or version.previous_version_id != metadata.previous_version_id
                    or hashlib.sha256(version.content.encode()).hexdigest()
                    != metadata.content_sha256
                ):
                    raise IntegrityViolation("Preparation document metadata/ownership mismatch")
                versions[metadata.kind] = version.id
            if entity.original_resume_version_id is not None:
                original = self.get(DocumentVersion, entity.original_resume_version_id)
                parent = self.get(ApplicationDocument, original.document_id)
                if parent.candidate_id != entity.candidate_id or parent.kind != DocumentKind.RESUME:
                    raise IntegrityViolation("Original CV must belong to this candidate")
            columns.update(
                candidate_id=str(entity.candidate_id),
                job_id=str(entity.job_id),
                assessment_id=str(entity.assessment_id),
                resume_version_id=str(versions[DocumentKind.RESUME]),
                cover_version_id=str(versions[DocumentKind.COVER_LETTER]),
                original_resume_version_id=_optional_id(entity.original_resume_version_id),
            )
        # Table/column identifiers are internal constants; all data is parameterized.
        placeholders = ",".join("?" for _ in columns)
        try:
            self._connection.execute(
                f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
                tuple(columns.values()),
            )
        except sqlite3.IntegrityError:
            raise IntegrityViolation(f"Constraint violation in {table}") from None
        if isinstance(entity, JobApplication):
            self.add(
                AuditEvent(application_id=entity.id, actor="system", action="application_created")
            )
        elif isinstance(entity, ApprovalRequest):
            self.add(
                AuditEvent(
                    application_id=entity.application_id,
                    actor="system",
                    action="approval_requested",
                )
            )

    def latest_document_version(self, document_id: UUID) -> DocumentVersion | None:
        self.get(ApplicationDocument, document_id)
        row = self._connection.execute(
            "SELECT payload FROM versions WHERE document_id = ? ORDER BY number DESC LIMIT 1",
            (str(document_id),),
        ).fetchone()
        return DocumentVersion.model_validate_json(row[0]) if row is not None else None

    def attach_document(self, identifier: UUID, version_id: UUID) -> JobApplication:
        with self._write():
            previous = self.get(JobApplication, identifier)
            updated = previous.attach_document(version_id)
            try:
                self._connection.execute(
                    "UPDATE applications SET document_version_id = ?, payload = ? WHERE id = ?",
                    (str(version_id), updated.model_dump_json(), str(identifier)),
                )
            except sqlite3.IntegrityError:
                raise IntegrityViolation("Document version must belong to this candidate") from None
            self.add(
                AuditEvent(application_id=identifier, actor="user", action="document_attached")
            )
            return updated

    def audit_history(self, application_id: UUID) -> tuple[AuditEvent, ...]:
        self.get(JobApplication, application_id)
        rows = self._connection.execute(
            "SELECT payload FROM audit_events WHERE application_id = ? ORDER BY rowid",
            (str(application_id),),
        ).fetchall()
        return tuple(AuditEvent.model_validate_json(row[0]) for row in rows)

    def transition_application(self, identifier: UUID, target: ApplicationStatus) -> JobApplication:
        with self._write():
            return self._transition_application(identifier, target)

    def _transition_application(
        self, identifier: UUID, target: ApplicationStatus
    ) -> JobApplication:
        previous = self.get(JobApplication, identifier)
        updated = previous.transition(target)
        self._connection.execute(
            "UPDATE applications SET status = ?, payload = ? WHERE id = ?",
            (updated.status.value, updated.model_dump_json(), str(identifier)),
        )
        self.add(
            AuditEvent(
                application_id=identifier,
                actor="user",
                action="status_changed",
                from_status=previous.status.value,
                to_status=updated.status.value,
            )
        )
        return updated

    def decide_approval(self, identifier: UUID, *, approved: bool) -> ApprovalRequest:
        with self._write():
            return self._decide_approval(identifier, approved=approved)

    def _decide_approval(self, identifier: UUID, *, approved: bool) -> ApprovalRequest:
        previous = self.get(ApprovalRequest, identifier)
        application = self.get(JobApplication, previous.application_id)
        if application.document_version_id != previous.document_version_id:
            raise IntegrityViolation("Cannot decide approval for a superseded version")
        updated = previous.decide(approved)
        self._connection.execute(
            "UPDATE approvals SET payload = ? WHERE id = ?",
            (updated.model_dump_json(), str(identifier)),
        )
        self.add(
            AuditEvent(
                application_id=previous.application_id,
                actor="user",
                action="approval_decided",
                from_status=previous.status.value,
                to_status=updated.status.value,
            )
        )
        return updated


def _optional_id(value: UUID | None) -> str | None:
    return str(value) if value is not None else None
