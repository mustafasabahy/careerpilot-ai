"""Persistence contract used by local transactions; independent of SQLite."""

from typing import Protocol, TypeVar
from uuid import UUID

from careerpilot.domain.applications import (
    ApplicationDocument,
    ApplicationStatus,
    DocumentVersion,
    JobApplication,
)
from careerpilot.domain.governance import ApprovalRequest, AuditEvent
from careerpilot.domain.jobs import JobFitAssessment, JobPosting, JobSource
from careerpilot.domain.preparation import PreparationRecord
from careerpilot.domain.profile import CandidateProfile

Stored = (
    CandidateProfile
    | JobPosting
    | JobFitAssessment
    | ApplicationDocument
    | DocumentVersion
    | JobApplication
    | ApprovalRequest
    | AuditEvent
    | PreparationRecord
)
StoredT = TypeVar("StoredT", bound=Stored)


class IntegrityViolation(ValueError):
    """A uniqueness, reference or ownership invariant was violated."""


class DuplicateJob(IntegrityViolation):
    """Source identity or canonical URL already exists."""


class NotFound(LookupError):
    """A requested entity does not exist."""


class CareerRepository(Protocol):
    """A transaction-scoped aggregate store; no implicit commits or raw SQL."""

    def add(self, entity: Stored) -> None: ...
    def get(self, model: type[StoredT], identifier: UUID) -> StoredT: ...
    def find_duplicate_jobs(self, source: JobSource) -> tuple[JobPosting, ...]: ...
    def transition_application(
        self, identifier: UUID, target: ApplicationStatus
    ) -> JobApplication: ...
    def attach_document(self, identifier: UUID, version_id: UUID) -> JobApplication: ...
    def latest_document_version(self, document_id: UUID) -> DocumentVersion | None: ...
    def audit_history(self, application_id: UUID) -> tuple[AuditEvent, ...]: ...
    def decide_approval(self, identifier: UUID, *, approved: bool) -> ApprovalRequest: ...
