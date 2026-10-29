"""Validated execution policy and safe failure reports; no runtime dependencies."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from careerpilot.domain.common import Model

FailureCode = Literal[
    "invalid_input",
    "unauthorized",
    "not_found",
    "conflict",
    "configuration",
    "invalid_output",
    "timeout",
    "rate_limited",
    "unavailable",
    "storage_busy",
    "integrity",
    "cancelled",
    "limit_exceeded",
    "unknown_outcome",
    "internal",
]


class FailureReport(Model):
    code: FailureCode
    retryable: bool = False
    uncertain: bool = False
    message: str
    retry_after_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ExecutionLimits(Model):
    max_steps: int = Field(default=32, ge=1, le=200, strict=True)
    max_tool_calls: int = Field(default=30, ge=0, le=100, strict=True)
    max_retrieved_jobs: int = Field(default=30, ge=0, le=100, strict=True)
    max_model_calls: int = Field(default=3, ge=0, le=10, strict=True)
    overall_seconds: float = Field(default=120, gt=0, le=600, allow_inf_nan=False)


class RetryPolicy(Model):
    max_retries: int = Field(default=0, ge=0, le=2, strict=True)
    initial_delay: float = Field(default=0.25, ge=0, le=2, allow_inf_nan=False)
    maximum_delay: float = Field(default=10, ge=0, le=30, allow_inf_nan=False)


class RunRecord(Model):
    operation_id: UUID
    owner_id: UUID
    correlation_id: UUID
    workflow_id: UUID
    status: Literal["running", "completed", "failed", "cancelled"]
    steps: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    retrieved_jobs: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = None
    nodes: dict[str, int] = Field(default_factory=dict)
    failure: FailureReport | None = None
