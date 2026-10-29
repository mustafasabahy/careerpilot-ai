"""One safe taxonomy over existing public errors; unknown errors are never retried."""

from pydantic import ValidationError

from careerpilot.application.action_contracts import (
    ActionConflict,
    ActionDenied,
    AdapterUnavailable,
    AmbiguousExecution,
    DefiniteAdapterFailure,
)
from careerpilot.application.discovery_contracts import (
    AccessDenied,
    DiscoveryConflict,
    InvalidToolInput,
    PayloadLimitExceeded,
    ResourceNotFound,
    SourceDataError,
    SourceFailure,
    SourceRateLimited,
    SourceTimeout,
    UnknownSource,
)
from careerpilot.application.extraction import (
    InvalidModelOutput,
    ModelRefusal,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderFailure,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderTransientFailure,
)
from careerpilot.application.memory import MemoryConflict, MemoryDenied
from careerpilot.application.preparation_contracts import (
    PreparationAccessDenied,
    PreparationConflict,
    PreparationGenerationError,
    PreparationValidationError,
)
from careerpilot.application.storage import IntegrityViolation, NotFound
from careerpilot.domain.reliability import FailureReport


class ExecutionFailure(RuntimeError):
    def __init__(self, report: FailureReport) -> None:
        self.report = report
        super().__init__(report.message)


class RunCancelled(ExecutionFailure):
    def __init__(self) -> None:
        super().__init__(FailureReport(code="cancelled", message="Execution was cancelled"))


class DeadlineExceeded(ExecutionFailure):
    def __init__(self) -> None:
        super().__init__(
            FailureReport(code="timeout", message="Execution time budget was exhausted")
        )


class BudgetExceeded(ExecutionFailure):
    def __init__(self) -> None:
        super().__init__(
            FailureReport(code="limit_exceeded", message="Execution resource limit reached")
        )


class StorageBusy(ExecutionFailure):
    def __init__(self) -> None:
        super().__init__(
            FailureReport(
                code="storage_busy", retryable=True, message="Local storage is temporarily busy"
            )
        )


def failure_report(error: BaseException) -> FailureReport:
    if isinstance(error, (KeyboardInterrupt, GeneratorExit)):
        return FailureReport(code="cancelled", message="Execution was interrupted")
    # SQLite adapters/checkpointers expose stable numeric codes; never inspect SQL text.
    if type(error).__module__ == "sqlite3":
        code = getattr(error, "sqlite_errorcode", 0) & 255
        if code in {5, 6}:
            return StorageBusy().report
        if code == 19:
            return FailureReport(code="integrity", message="Local persistence integrity failed")
    if isinstance(error, ExecutionFailure):
        return error.report
    if isinstance(
        error,
        (
            PermissionError,
            ActionDenied,
            MemoryDenied,
            PreparationAccessDenied,
            AccessDenied,
            ProviderAuthenticationError,
        ),
    ):
        return FailureReport(code="unauthorized", message="Operation is not authorized")
    if isinstance(error, AmbiguousExecution):
        return FailureReport(
            code="unknown_outcome", uncertain=True, message="An external effect may have occurred"
        )
    if isinstance(error, (ProviderRateLimited, SourceRateLimited)):
        return FailureReport(
            code="rate_limited",
            retryable=getattr(error, "retryable", True),
            retry_after_seconds=getattr(error, "retry_after_seconds", None),
            message="Provider rate limit or quota prevents this operation",
        )
    if isinstance(error, (ProviderTimeout, SourceTimeout, TimeoutError)):
        return FailureReport(
            code="timeout", retryable=True, message="Read/model operation timed out"
        )
    if isinstance(error, ProviderTransientFailure):
        return FailureReport(
            code="unavailable",
            retryable=True,
            message="Provider is temporarily unavailable",
            retry_after_seconds=error.retry_after_seconds,
        )
    if isinstance(error, (ProviderConfigurationError, UnknownSource, AdapterUnavailable)):
        return FailureReport(
            code="configuration", message="A required adapter is not safely configured"
        )
    if isinstance(error, (NotFound, ResourceNotFound)):
        return FailureReport(code="not_found", message="Requested resource is unavailable")
    if isinstance(error, (ActionConflict, MemoryConflict, PreparationConflict, DiscoveryConflict)):
        return FailureReport(code="conflict", message="Resource version or lifecycle has changed")
    if isinstance(error, IntegrityViolation):
        return FailureReport(code="integrity", message="A persistence integrity rule was violated")
    if isinstance(error, (InvalidModelOutput, ModelRefusal, SourceDataError)):
        return FailureReport(code="invalid_output", message="Untrusted output failed validation")
    if isinstance(
        error, (ProviderFailure, SourceFailure, DefiniteAdapterFailure, PreparationGenerationError)
    ):
        return FailureReport(
            code="unavailable", message="Adapter failed without a safe retry guarantee"
        )
    if isinstance(error, PayloadLimitExceeded):
        return FailureReport(code="limit_exceeded", message="Adapter payload limit exceeded")
    if isinstance(
        error, (ValidationError, InvalidToolInput, PreparationValidationError, ValueError)
    ):
        return FailureReport(code="invalid_input", message="Input or business validation failed")
    return FailureReport(
        code="internal", message="Execution failed; inspect correlation identifiers"
    )
