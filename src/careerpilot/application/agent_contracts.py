"""Provider-neutral contracts for future specialist-agent delegation.

These models are intentionally passive. Phase A does not route, persist, authorize,
or execute delegation; existing application services remain the authority for those
concerns.
"""

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from careerpilot.domain.common import Model, Text, UtcTimestamp


class AgentCapability(StrEnum):
    """A capability label, not an authorization grant."""

    JOB_DISCOVERY = "job_discovery"
    JOB_ANALYSIS = "job_analysis"
    CAREER_PREPARATION = "career_preparation"
    INTERVIEW_PREPARATION = "interview_preparation"
    APPLICATION_TRACKING = "application_tracking"
    APPROVAL_COORDINATION = "approval_coordination"
    CAREER_CONTEXT = "career_context"


class AgentReference(Model):
    """A bounded reference to a persisted resource; never a copied document."""

    kind: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    id: UUID


class GlobalExecutionBudget(Model):
    """Shared future budget limits and consumption counters.

    A ``None`` limit means no limit was supplied; it is different from a zero limit.
    This model does not enforce accounting until a future supervisor consumes it.
    """

    max_model_calls: int | None = Field(default=None, ge=0, strict=True)
    max_tool_calls: int | None = Field(default=None, ge=0, strict=True)
    max_steps: int | None = Field(default=None, ge=0, strict=True)
    max_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_delegation_depth: int | None = Field(default=None, ge=0, strict=True)
    max_specialists: int | None = Field(default=None, ge=0, strict=True)
    max_parallel_specialists: int | None = Field(default=None, ge=0, strict=True)
    max_input_tokens: int | None = Field(default=None, ge=0, strict=True)
    max_output_tokens: int | None = Field(default=None, ge=0, strict=True)
    max_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    model_calls: int = Field(default=0, ge=0, strict=True)
    tool_calls: int = Field(default=0, ge=0, strict=True)
    steps: int = Field(default=0, ge=0, strict=True)
    elapsed_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    delegation_depth: int = Field(default=0, ge=0, strict=True)
    specialists: int = Field(default=0, ge=0, strict=True)
    parallel_specialists: int = Field(default=0, ge=0, strict=True)
    input_tokens: int = Field(default=0, ge=0, strict=True)
    output_tokens: int = Field(default=0, ge=0, strict=True)
    cost_usd: float = Field(default=0, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def counters_do_not_exceed_limits(self) -> "GlobalExecutionBudget":
        pairs = (
            ("model_calls", self.model_calls, self.max_model_calls),
            ("tool_calls", self.tool_calls, self.max_tool_calls),
            ("steps", self.steps, self.max_steps),
            ("elapsed_seconds", self.elapsed_seconds, self.max_seconds),
            ("delegation_depth", self.delegation_depth, self.max_delegation_depth),
            ("specialists", self.specialists, self.max_specialists),
            ("parallel_specialists", self.parallel_specialists, self.max_parallel_specialists),
            ("input_tokens", self.input_tokens, self.max_input_tokens),
            ("output_tokens", self.output_tokens, self.max_output_tokens),
            ("cost_usd", self.cost_usd, self.max_cost_usd),
        )
        exceeded = tuple(name for name, used, limit in pairs if limit is not None and used > limit)
        if exceeded:
            raise ValueError(f"Budget usage exceeds configured limit: {', '.join(exceeded)}")
        return self

    def exhausted(self, resource: str) -> bool:
        """Report exhaustion without treating an unspecified limit as exhausted."""

        limits = {
            "model_calls": (self.model_calls, self.max_model_calls),
            "tool_calls": (self.tool_calls, self.max_tool_calls),
            "steps": (self.steps, self.max_steps),
            "seconds": (self.elapsed_seconds, self.max_seconds),
            "delegation_depth": (self.delegation_depth, self.max_delegation_depth),
            "specialists": (self.specialists, self.max_specialists),
            "parallel_specialists": (self.parallel_specialists, self.max_parallel_specialists),
            "input_tokens": (self.input_tokens, self.max_input_tokens),
            "output_tokens": (self.output_tokens, self.max_output_tokens),
            "cost_usd": (self.cost_usd, self.max_cost_usd),
        }
        if resource not in limits:
            raise ValueError(f"Unknown budget resource: {resource}")
        used, limit = limits[resource]
        return limit is not None and used >= limit


class AgentRequest(Model):
    """Bounded future delegation input using owner-scoped persisted references."""

    user_id: UUID
    conversation_id: UUID
    task_id: UUID
    parent_task_id: UUID | None = None
    requested_capability: AgentCapability
    allowed_capabilities: tuple[AgentCapability, ...] = Field(min_length=1, max_length=16)
    input_references: tuple[AgentReference, ...] = Field(default=(), max_length=32)
    delegation_context: tuple["DelegationContextItem", ...] = Field(default=(), max_length=16)
    deadline: UtcTimestamp | None = None
    budget: GlobalExecutionBudget | None = None

    @model_validator(mode="after")
    def capability_is_allowed(self) -> "AgentRequest":
        if self.allowed_capabilities and self.requested_capability not in self.allowed_capabilities:
            raise ValueError("Requested capability is not in the allowed capability set")
        if self.parent_task_id == self.task_id:
            raise ValueError("Parent and child task IDs must differ")
        return self


class AgentErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED_CAPABILITY = "unauthorized_capability"
    NOT_FOUND = "not_found"
    VALIDATION_FAILURE = "validation_failure"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"
    CANCELLED = "cancelled"
    UNKNOWN_OUTCOME = "unknown_outcome"
    INTERNAL_FAILURE = "internal_failure"
    CONFLICTING_RESULTS = "conflicting_results"


class DelegationContextItem(Model):
    """Bounded, reference-first output passed from an earlier specialist."""

    specialist_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    status: Literal["completed", "partial", "failed", "waiting_for_user_action"]
    summary: Text
    references: tuple[AgentReference, ...] = Field(default=(), max_length=32)
    provenance: tuple[AgentReference, ...] = Field(default=(), max_length=32)


class AgentError(Model):
    """Safe, bounded delegation error; secrets and raw provider details are excluded."""

    code: AgentErrorCode
    message: Text
    retryable: bool = False
    uncertain: bool = False
    reference: AgentReference | None = None


class AgentSideEffect(Model):
    """Describes an effect without claiming that a proposal was executed."""

    kind: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    status: Literal["proposed", "confirmed", "failed", "unknown"]


class AgentResultStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    WAITING_FOR_USER_ACTION = "waiting_for_user_action"


class AgentResult(Model):
    """Serializable specialist output with explicit incomplete/action states."""

    status: AgentResultStatus
    summary: Text
    references: tuple[AgentReference, ...] = Field(default=(), max_length=32)
    required_user_action: Text | None = None
    side_effects: tuple[AgentSideEffect, ...] = Field(default=(), max_length=16)
    errors: tuple[AgentError, ...] = Field(default=(), max_length=8)
    provenance: tuple[AgentReference, ...] = Field(default=(), max_length=32)


class DelegationEventStatus(StrEnum):
    REQUESTED = "requested"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class DelegationEvent(Model):
    """Future parent/child delegation event; Phase A does not persist or emit it."""

    event_id: UUID
    parent_task_id: UUID
    child_task_id: UUID
    parent_agent_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    child_agent_id: str | None = Field(default=None, max_length=64)
    capability: AgentCapability
    reason: Text
    timestamp: UtcTimestamp
    status: DelegationEventStatus
    input_references: tuple[AgentReference, ...] = Field(default=(), max_length=32)
    output_references: tuple[AgentReference, ...] = Field(default=(), max_length=32)
    error: AgentError | None = None

    @model_validator(mode="after")
    def task_ids_differ(self) -> "DelegationEvent":
        if self.parent_task_id == self.child_task_id:
            raise ValueError("Parent and child task IDs must differ")
        return self
