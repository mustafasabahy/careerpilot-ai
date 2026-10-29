"""Explicit approval of an application version and minimal audit events."""

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import ValidationInfo, field_validator, model_validator

from careerpilot.domain.applications import TRANSITIONS, ApplicationStatus
from careerpilot.domain.common import Entity, UtcTimestamp, utc_now


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalRequest(Entity):
    application_id: UUID
    document_version_id: UUID
    expires_at: UtcTimestamp
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def valid_decision(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("Approval must expire after creation")
        if (self.status == ApprovalStatus.PENDING) != (self.decided_at is None):
            raise ValueError("Only pending approvals may omit decision time")
        if self.decided_at is not None:
            if not self.created_at <= self.decided_at < self.expires_at:
                raise ValueError("Approval decision must occur within its validity window")
        return self

    def decide(self, approved: bool, *, at: UtcTimestamp | None = None) -> Self:
        if self.status != ApprovalStatus.PENDING:
            raise ValueError("Approval has already been decided")
        return type(self).model_validate(
            {
                **self.model_dump(),
                "status": ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED,
                "decided_at": at if at is not None else utc_now(),
            }
        )


class AuditEvent(Entity):
    application_id: UUID
    actor: Literal["user", "system"]
    action: Literal[
        "application_created",
        "status_changed",
        "document_attached",
        "approval_requested",
        "approval_decided",
    ]
    from_status: ApplicationStatus | ApprovalStatus | None = None
    to_status: ApplicationStatus | ApprovalStatus | None = None

    @field_validator("from_status", "to_status", mode="before")
    @classmethod
    def status_for_action(
        cls, value: object, info: ValidationInfo
    ) -> ApplicationStatus | ApprovalStatus | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Audit status must be a status code")
        # 'rejected' exists in both enums; interpret it according to the action.
        if info.data.get("action") == "approval_decided":
            return ApprovalStatus(value)
        return ApplicationStatus(value)

    @model_validator(mode="after")
    def consistent_status_change(self) -> Self:
        status_type: type[ApplicationStatus] | type[ApprovalStatus] | None = None
        if self.action == "status_changed":
            status_type = ApplicationStatus
        elif self.action == "approval_decided":
            status_type = ApprovalStatus
        if status_type is None:
            if self.from_status is not None or self.to_status is not None:
                raise ValueError("This event must not carry status fields")
        elif not isinstance(self.from_status, status_type) or not isinstance(
            self.to_status, status_type
        ):
            raise ValueError("Status change requires correctly typed before/after states")
        if self.action == "status_changed" and isinstance(self.from_status, ApplicationStatus):
            if self.to_status not in TRANSITIONS[self.from_status]:
                raise ValueError("Audit event describes an invalid application transition")
        if self.action == "approval_decided":
            if self.from_status != ApprovalStatus.PENDING or self.to_status not in {
                ApprovalStatus.APPROVED,
                ApprovalStatus.REJECTED,
            }:
                raise ValueError("Audit event describes an invalid approval decision")
        return self
