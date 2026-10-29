"""Documents and the application lifecycle; no storage or orchestration."""

from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import Field, StrictBool, StringConstraints, model_validator

from careerpilot.domain.common import Entity, UtcTimestamp, utc_now


class DocumentKind(StrEnum):
    RESUME = "resume"
    COVER_LETTER = "cover_letter"


class ApplicationDocument(Entity):
    candidate_id: UUID
    kind: DocumentKind


class DocumentVersion(Entity):
    document_id: UUID
    number: int = Field(ge=1, strict=True)
    content: Annotated[
        str, StringConstraints(min_length=1, max_length=200_000)
    ]  # Development storage only; never logged.
    generated_by_ai: StrictBool | None = None
    previous_version_id: UUID | None = None

    @model_validator(mode="after")
    def valid_lineage(self) -> Self:
        if not self.content.strip():
            raise ValueError("Document content cannot be blank")
        if (self.number == 1) != (self.previous_version_id is None):
            raise ValueError("Only the first version may omit its predecessor")
        if self.previous_version_id == self.id:
            raise ValueError("A version cannot reference itself")
        return self

    def validate_predecessor(self, previous: "DocumentVersion") -> None:
        if (
            previous.id != self.previous_version_id
            or previous.document_id != self.document_id
            or previous.number + 1 != self.number
            or previous.created_at > self.created_at
        ):
            raise ValueError("Invalid document version lineage")


class ApplicationStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    SUBMITTED = "submitted"
    INTERVIEWING = "interviewing"
    OFFERED = "offered"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


TRANSITIONS: dict[ApplicationStatus, frozenset[ApplicationStatus]] = {
    ApplicationStatus.DRAFT: frozenset({ApplicationStatus.READY, ApplicationStatus.WITHDRAWN}),
    ApplicationStatus.READY: frozenset(
        {ApplicationStatus.DRAFT, ApplicationStatus.SUBMITTED, ApplicationStatus.WITHDRAWN}
    ),
    ApplicationStatus.SUBMITTED: frozenset(
        {ApplicationStatus.INTERVIEWING, ApplicationStatus.REJECTED, ApplicationStatus.WITHDRAWN}
    ),
    ApplicationStatus.INTERVIEWING: frozenset(
        {ApplicationStatus.OFFERED, ApplicationStatus.REJECTED, ApplicationStatus.WITHDRAWN}
    ),
    ApplicationStatus.OFFERED: frozenset(
        {ApplicationStatus.ACCEPTED, ApplicationStatus.REJECTED, ApplicationStatus.WITHDRAWN}
    ),
    ApplicationStatus.ACCEPTED: frozenset(),
    ApplicationStatus.REJECTED: frozenset(),
    ApplicationStatus.WITHDRAWN: frozenset(),
}


class JobApplication(Entity):
    candidate_id: UUID
    job_id: UUID
    document_version_id: UUID | None = None
    status: ApplicationStatus = ApplicationStatus.DRAFT
    updated_at: UtcTimestamp = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def valid_state(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("Application update precedes creation")
        if self.status not in {ApplicationStatus.DRAFT, ApplicationStatus.WITHDRAWN}:
            if self.document_version_id is None:
                raise ValueError("A prepared/submitted application requires a document version")
        return self

    def transition(self, target: ApplicationStatus, *, at: UtcTimestamp | None = None) -> Self:
        if target not in TRANSITIONS[self.status]:
            raise ValueError(f"Invalid application transition: {self.status} -> {target}")
        updated = type(self).model_validate(
            {
                **self.model_dump(),
                "status": target,
                "updated_at": at if at is not None else utc_now(),
            }
        )
        if updated.updated_at < self.updated_at:
            raise ValueError("Application transition cannot move time backwards")
        return updated

    def attach_document(self, version_id: UUID) -> Self:
        if self.status != ApplicationStatus.DRAFT:
            raise ValueError("Documents may only change while an application is in draft")
        updated = type(self).model_validate(
            {**self.model_dump(), "document_version_id": version_id, "updated_at": utc_now()}
        )
        if updated.updated_at < self.updated_at:
            raise ValueError("Document attachment cannot move time backwards")
        return updated
