"""Immutable sensitive-action proposals and exact-version review records."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator, model_validator

from careerpilot.domain.applications import ApplicationStatus
from careerpilot.domain.common import Entity, Model, UtcTimestamp

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
ActionKind = Literal["recruiter_message", "submit_application", "change_application"]


class RecruiterMessage(Model):
    kind: Literal["recruiter_message"] = "recruiter_message"
    target_id: UUID
    subject: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]

    @field_validator("subject")
    @classmethod
    def safe_subject(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("Message subjects must not contain control characters")
        return value

    @field_validator("body")
    @classmethod
    def safe_body(cls, value: str) -> str:
        if any(ord(character) < 32 and character not in "\n\t" for character in value):
            raise ValueError("Message content contains unsupported control characters")
        return value


class SubmitApplication(Model):
    kind: Literal["submit_application"] = "submit_application"
    target_id: UUID
    document_version_id: UUID
    document_sha256: Digest


class ChangeApplication(Model):
    kind: Literal["change_application"] = "change_application"
    target_status: ApplicationStatus
    reason: ShortText

    @model_validator(mode="after")
    def submission_has_own_action(self) -> Self:
        if self.target_status == ApplicationStatus.SUBMITTED:
            raise ValueError("Submission requires its dedicated reviewed external action")
        return self


ActionContent = Annotated[
    RecruiterMessage | SubmitApplication | ChangeApplication, Field(discriminator="kind")
]


class ConfiguredTarget(Model):
    id: UUID
    kind: Literal["recruiter_message", "submit_application"]
    label: ShortText
    route_key: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9@._+-]{1,200}$")]
    revision: int = Field(ge=1, strict=True)


class ProposalRequest(Model):
    id: UUID
    thread_id: UUID
    application_id: UUID
    content: ActionContent
    expires_at: UtcTimestamp
    previous_proposal_id: UUID | None = None


class ActionProposal(Entity):
    thread_id: UUID
    owner_id: UUID
    application_id: UUID
    candidate_id: UUID
    job_id: UUID
    version: int = Field(ge=1, strict=True)
    previous_proposal_id: UUID | None
    expires_at: UtcTimestamp
    application_sha256: Digest
    target: ConfiguredTarget | None
    adapter_id: ShortText
    content: ActionContent
    content_sha256: Digest

    @model_validator(mode="after")
    def consistent_proposal(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("Proposal must expire after creation")
        if (self.version == 1) != (self.previous_proposal_id is None):
            raise ValueError("Revisions require a predecessor and a new version")
        if isinstance(self.content, ChangeApplication):
            if self.target is not None or self.adapter_id != "local-record-v1":
                raise ValueError("Local changes cannot acquire external routing")
        elif (
            self.target is None
            or self.target.id != self.content.target_id
            or self.target.kind != self.content.kind
        ):
            raise ValueError("Proposal target does not match the action")
        return self


class ReviewDecision(Model):
    id: UUID
    proposal_id: UUID
    thread_id: UUID
    proposal_version: int = Field(ge=1, strict=True)
    content_sha256: Digest
    decision: Literal["approve", "reject", "revise"]
    reason: ShortText | None = None

    @model_validator(mode="after")
    def revision_needs_feedback(self) -> Self:
        if self.decision == "revise" and self.reason is None:
            raise ValueError("A revision request needs review feedback")
        return self


class ActionDecision(Entity):
    owner_id: UUID
    actor_id: UUID
    proposal_id: UUID
    thread_id: UUID
    proposal_version: int = Field(ge=1, strict=True)
    content_sha256: Digest
    decision: Literal["approve", "reject", "revise"]
    reason: ShortText | None


ActionStatus = Literal[
    "pending",
    "approved",
    "rejected",
    "revision_requested",
    "executing",
    "simulated",
    "applied",
    "failed",
    "unknown",
    "invalidated",
]


class ActionLifecycle(Model):
    status: ActionStatus


class ActionOutcome(Entity):
    proposal_id: UUID
    status: Literal["simulated", "applied", "failed", "unknown", "invalidated"]
    code: Literal[
        "simulated_effect",
        "record_updated",
        "adapter_failure",
        "ambiguous_execution",
        "stale_or_expired",
        "cancelled",
    ]
    receipt_id: ShortText | None = None

    @model_validator(mode="after")
    def consistent_outcome(self) -> Self:
        codes = {
            "simulated": "simulated_effect",
            "applied": "record_updated",
            "failed": "adapter_failure",
            "unknown": "ambiguous_execution",
            "invalidated": "stale_or_expired",
        }
        if self.code != codes[self.status] and not (
            self.status == "invalidated" and self.code == "cancelled"
        ):
            raise ValueError("Outcome status and reason code must agree")
        if (self.status == "simulated") != (self.receipt_id is not None):
            raise ValueError("Only simulated success carries a simulation receipt")
        return self


class ActionAudit(Entity):
    proposal_id: UUID
    actor_id: UUID
    action: Literal[
        "proposed",
        "approved",
        "rejected",
        "revision_requested",
        "execution_claimed",
        "simulated",
        "applied",
        "failed",
        "unknown",
        "invalidated",
    ]


class ActionReview(Model):
    proposal: ActionProposal
    status: ActionStatus
    decision: ActionDecision | None
    outcome: ActionOutcome | None
    # A selected submission version is loaded on demand for actual human review.
    document_content: str | None = Field(default=None, max_length=200000)
