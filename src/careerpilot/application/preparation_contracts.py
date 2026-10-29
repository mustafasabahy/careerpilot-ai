"Preparation authorization and provider-neutral planning/output boundaries."

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from careerpilot.application.discovery_contracts import AccessGrant
from careerpilot.domain.common import Entity, Model, Text, UtcTimestamp
from careerpilot.domain.preparation import (
    CareerIntelligence,
    FactKind,
    PreparationRecord,
    ProfileFact,
)


class PreparationError(RuntimeError):
    pass


class PreparationValidationError(PreparationError):
    pass


class InvalidPreparationRequest(PreparationValidationError):
    pass


class PreparationGenerationError(PreparationError):
    pass


class PreparationConflict(PreparationError):
    pass


class PreparationAccessDenied(PreparationError):
    pass


@dataclass(frozen=True)
class PreparationAuthorization:
    access: AccessGrant
    job_ids: frozenset[UUID]
    allow_drafts: bool = False
    allow_profile_model_export: bool = False


class PreparationRequest(Entity):
    id: UUID = Field(...)
    created_at: UtcTimestamp = Field(...)
    candidate_id: UUID
    job_id: UUID
    assessment_id: UUID
    resume_document_id: UUID | None = None
    cover_document_id: UUID | None = None
    original_resume_version_id: UUID | None = None


class PlanningInput(Model):
    facts: tuple[ProfileFact, ...] = Field(max_length=100)
    preferred_fact_ids: tuple[UUID, ...]
    # No name, raw CV, work-authorization fact or unrestricted prose instructions.
    purpose: Literal[("Select existing fact IDs for a reviewable career draft")] = (
        "Select existing fact IDs for a reviewable career draft"
    )

    @model_validator(mode="after")
    def minimal_safe_catalog(self) -> "PlanningInput":
        if sum(len(fact.text.encode()) for fact in self.facts) > 32 * 1024:
            raise ValueError("Planner professional fact text exceeds 32 KiB")
        ids = {fact.id for fact in self.facts}
        if len(ids) != len(self.facts) or not set(self.preferred_fact_ids).issubset(ids):
            raise ValueError("Planning fact references are inconsistent")
        if any(
            fact.verification != "user_confirmed"
            or fact.kind in {FactKind.NAME, FactKind.AUTHORIZATION}
            for fact in self.facts
        ):
            raise ValueError("Only user-confirmed professional facts may reach a planner")
        return self


class DraftPlanner(Protocol):
    @property
    def provider(self) -> str: ...
    @property
    def model(self) -> str: ...
    def plan(self, request: PlanningInput) -> object: ...


class ReviewableDraft(Model):
    version_id: UUID
    kind: Literal["resume", "cover_letter"]
    content: str
    changes: str


class PreparationSuccess(Model):
    status: Literal["drafts_created"] = "drafts_created"
    record: PreparationRecord
    drafts: tuple[ReviewableDraft, ...]


class PreparationNeedsInformation(Model):
    status: Literal["needs_information"] = "needs_information"
    intelligence: CareerIntelligence


class PreparationFailure(Model):
    status: Literal["error"] = "error"
    code: Literal[
        "invalid_request",
        "access_denied",
        "validation_failed",
        "generation_failed",
        "identity_conflict",
        "not_found",
    ]
    message: Text
