"""Cooperative budgets/cancellation and bounded retries, independent of graph/providers."""

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from threading import Event, RLock
from typing import Literal, Protocol
from uuid import UUID, uuid4

from careerpilot.application.failures import (
    BudgetExceeded,
    DeadlineExceeded,
    RunCancelled,
    failure_report,
)
from careerpilot.domain.reliability import ExecutionLimits, FailureReport, RetryPolicy, RunRecord

Resource = Literal["steps", "tool_calls", "retrieved_jobs", "model_calls"]


class RunObserver(Protocol):
    def observe(self, record: RunRecord) -> None: ...


class RunJournal(Protocol):
    def begin(self, record: RunRecord) -> None: ...
    def save(self, record: RunRecord) -> None: ...
    def cancelled(self, operation_id: UUID) -> bool: ...


_DEFAULT_LIMITS: ContextVar[ExecutionLimits | None] = ContextVar("execution_limits", default=None)

_CURRENT: ContextVar["RunControl | None"] = ContextVar("careerpilot_run", default=None)
_CEILING: ContextVar["ExecutionLimits | None"] = ContextVar("careerpilot_limits", default=None)
_CEILING_DEADLINE: ContextVar[float | None] = ContextVar("careerpilot_deadline", default=None)


class RunControl:
    def __init__(
        self,
        limits: ExecutionLimits | None = None,
        *,
        owner_id: UUID | None = None,
        operation_id: UUID | None = None,
        workflow_id: UUID | None = None,
        correlation_id: UUID | None = None,
        journal: RunJournal | None = None,
        observer: RunObserver | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = ExecutionLimits.model_validate(
            (limits or _DEFAULT_LIMITS.get() or ExecutionLimits()).model_dump()
        )
        self.observer = observer
        self.started = clock()
        self.clock = clock
        self.deadline = clock() + self.limits.overall_seconds
        self.record = RunRecord(
            operation_id=operation_id or uuid4(),
            owner_id=owner_id or uuid4(),
            workflow_id=workflow_id or uuid4(),
            correlation_id=correlation_id or uuid4(),
            status="running",
        )
        self.journal = journal
        self._cancel = Event()
        self._lock = RLock()
        if journal is not None:
            journal.begin(self.record)
        self.log("execution_started")

    def log(self, event: str, **safe: object) -> None:
        logging.getLogger("careerpilot").info(
            "",
            extra={
                "event": event,
                "operation_id": self.record.operation_id,
                "workflow_id": self.record.workflow_id,
                "correlation_id": self.record.correlation_id,
                **safe,
            },
        )

    def cancel(self) -> None:
        self._cancel.set()

    def check(self) -> None:
        if self._cancel.is_set() or (
            self.journal and self.journal.cancelled(self.record.operation_id)
        ):
            raise RunCancelled()
        ceiling_deadline = _CEILING_DEADLINE.get()
        if (
            self.clock() >= min(self.deadline, ceiling_deadline)
            if ceiling_deadline
            else self.clock() >= self.deadline
        ):
            raise DeadlineExceeded()

    def remaining(self, maximum: float) -> float:
        self.check()
        ceiling_deadline = _CEILING_DEADLINE.get()
        deadline = min(self.deadline, ceiling_deadline) if ceiling_deadline else self.deadline
        seconds = deadline - self.clock()
        if seconds <= 0:
            raise DeadlineExceeded()
        return min(maximum, seconds)

    def consume(self, resource: Resource, count: int = 1) -> None:
        with self._lock:
            self.check()
            maximum = getattr(self.limits, f"max_{resource}")
            if ceiling := _CEILING.get():
                maximum = min(maximum, getattr(ceiling, f"max_{resource}"))
            if (
                type(count) is not int
                or count < 0
                or getattr(self.record, resource) + count > maximum
            ):
                raise BudgetExceeded()
            self.record = RunRecord.model_validate(
                {
                    **self.record.model_dump(),
                    resource: getattr(self.record, resource) + count,
                }
            )
            if self.journal:
                self.journal.save(self.record)

    def note_node(self, name: str) -> None:
        if not name.isidentifier() or len(name) > 64:
            return
        with self._lock:
            nodes = {**self.record.nodes, name: self.record.nodes.get(name, 0) + 1}
            self.record = RunRecord.model_validate({**self.record.model_dump(), "nodes": nodes})

    def note_usage(self, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self.record = RunRecord.model_validate(
                {
                    **self.record.model_dump(),
                    "input_tokens": (self.record.input_tokens or 0) + input_tokens,
                    "output_tokens": (self.record.output_tokens or 0) + output_tokens,
                }
            )

    def note_retry(self) -> None:
        with self._lock:
            self.record = RunRecord.model_validate(
                {
                    **self.record.model_dump(),
                    "retry_count": self.record.retry_count + 1,
                }
            )

    def note_result(self, result: object) -> None:
        review = getattr(result, "review", result)
        status = getattr(review, "status", None)
        report = None
        if status == "unknown":
            report = FailureReport(
                code="unknown_outcome",
                uncertain=True,
                message="External outcome requires reconciliation",
            )
        elif status in {"failed", "invalidated"}:
            outcome = getattr(review, "outcome", None)
            cancelled = getattr(outcome, "code", None) == "cancelled"
            report = FailureReport(
                code="cancelled" if cancelled else "conflict",
                message="Action was not executed successfully",
            )
        elif status == "error":
            code = str(getattr(review, "code", "internal"))
            report = FailureReport(
                code="unauthorized"
                if code in {"access_denied"}
                else "invalid_input"
                if code in {"invalid_request", "invalid_input"}
                else "internal",
                message="Workflow returned a typed failure result",
            )
        if report is not None:
            self.record = RunRecord.model_validate({**self.record.model_dump(), "failure": report})

    def finish(self, error: BaseException | None = None) -> None:
        report = failure_report(error) if error else self.record.failure
        self.record = RunRecord.model_validate(
            {
                **self.record.model_dump(),
                "status": "cancelled"
                if report and report.code == "cancelled"
                else "failed"
                if report
                else "completed",
                "failure": report,
                "elapsed_seconds": max(0, self.clock() - self.started),
            }
        )
        if self.journal:
            self.journal.save(self.record)
        if self.observer:
            try:
                self.observer.observe(self.record)
            except Exception:
                self.log("telemetry_failed")
        self.log(
            "execution_failed" if report else "execution_completed",
            error_code=report.code if report else None,
        )


@contextmanager
def configured_limits(limits: ExecutionLimits) -> Iterator[None]:
    token = _DEFAULT_LIMITS.set(ExecutionLimits.model_validate(limits.model_dump()))
    try:
        yield
    finally:
        _DEFAULT_LIMITS.reset(token)


def _workflow_id(arguments: tuple[object, ...]) -> UUID | None:
    for argument in arguments:
        value = argument
        if isinstance(value, dict):
            value = value.get("thread_id", value.get("id"))
        elif not isinstance(value, UUID):
            value = getattr(value, "thread_id", getattr(value, "id", None))
        if isinstance(value, UUID):
            return value
        if isinstance(value, str):
            try:
                return UUID(value)
            except ValueError:
                pass
    return None


def current_run() -> RunControl | None:
    return _CURRENT.get()


def consume(resource: Resource, count: int = 1) -> None:
    if control := current_run():
        control.consume(resource, count)


def remaining_timeout(maximum: float) -> float:
    control = current_run()
    return control.remaining(maximum) if control is not None else maximum


@contextmanager
def execution_scope(control: RunControl | None = None) -> Iterator[RunControl]:
    if active := current_run():
        yield active
        return
    selected = control or RunControl()
    if selected.record.status != "running":
        raise BudgetExceeded()
    token = _CURRENT.set(selected)
    try:
        selected.check()
        yield selected
        selected.check()
    except BaseException as error:
        selected.finish(error)
        raise
    else:
        selected.finish()
    finally:
        _CURRENT.reset(token)


@contextmanager
def scoped_limits(limits: ExecutionLimits) -> Iterator[None]:
    """Apply a child execution ceiling without replacing the active RunControl."""

    requested = ExecutionLimits.model_validate(limits.model_dump())
    previous = _CEILING.get()
    ceiling = requested
    if previous is not None:
        ceiling = ExecutionLimits(
            max_steps=min(previous.max_steps, requested.max_steps),
            max_tool_calls=min(previous.max_tool_calls, requested.max_tool_calls),
            max_retrieved_jobs=min(previous.max_retrieved_jobs, requested.max_retrieved_jobs),
            max_model_calls=min(previous.max_model_calls, requested.max_model_calls),
            overall_seconds=min(previous.overall_seconds, requested.overall_seconds),
        )
    limit_token = _CEILING.set(ceiling)
    current = current_run()
    requested_deadline = current.clock() + ceiling.overall_seconds if current else None
    previous_deadline = _CEILING_DEADLINE.get()
    deadline = (
        min(previous_deadline, requested_deadline)
        if previous_deadline is not None and requested_deadline is not None
        else previous_deadline or requested_deadline
    )
    deadline_token = _CEILING_DEADLINE.set(deadline)
    try:
        if current:
            current.check()
        yield
    finally:
        _CEILING_DEADLINE.reset(deadline_token)
        _CEILING.reset(limit_token)


def storage_timeout(maximum: float = 5) -> float:
    # Reserve a small window for outcome/rollback bookkeeping after cancellation
    # or deadline; never turn a known receipt into an unrecorded effect on purpose.
    control = current_run()
    return min(maximum, max(0.01, control.deadline - control.clock())) if control else maximum


def bounded_workflow[**P, T](operation: Callable[P, T]) -> Callable[P, T]:
    @wraps(operation)
    def execute(*args: P.args, **kwargs: P.kwargs) -> T:
        selected = current_run() or RunControl(workflow_id=_workflow_id((*args, *kwargs.values())))
        with execution_scope(selected):
            result = operation(*args, **kwargs)
            selected.note_result(result)
            return result

    return execute


def counted_node[**P, T](operation: Callable[P, T]) -> Callable[P, T]:
    @wraps(operation)
    def execute(*args: P.args, **kwargs: P.kwargs) -> T:
        consume("steps")
        if control := current_run():
            control.note_node(operation.__name__)
        return operation(*args, **kwargs)

    return execute


def bounded_stream[**P, T](operation: Callable[P, Iterator[T]]) -> Callable[P, Iterator[T]]:
    @wraps(operation)
    def stream(*args: P.args, **kwargs: P.kwargs) -> Iterator[T]:
        # Bind only while driving next(); never leak runtime context into caller code.
        parent = current_run()
        selected = parent or RunControl(workflow_id=_workflow_id((*args, *kwargs.values())))
        iterator = operation(*args, **kwargs)
        ended = False
        try:
            while True:
                token = _CURRENT.set(selected)
                try:
                    selected.check()
                    value = next(iterator)
                except StopIteration:
                    if parent is None:
                        selected.finish()
                    ended = True
                    return
                finally:
                    _CURRENT.reset(token)
                yield value
        except BaseException as error:
            if parent is None and not ended:
                selected.finish(error)
            raise
        finally:
            if hasattr(iterator, "close"):
                iterator.close()

    return stream


def retry_read[T](
    operation: Callable[[], T],
    policy: RetryPolicy,
    *,
    resource: Resource | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    policy = RetryPolicy.model_validate(policy.model_dump())
    for attempt in range(policy.max_retries + 1):
        if control := current_run():
            control.check()
        if resource:
            consume(resource)
        try:
            result = operation()
        except Exception as error:
            report = failure_report(error)
            if not report.retryable or report.uncertain or attempt >= policy.max_retries:
                raise
            delay = max(policy.initial_delay * 2**attempt, report.retry_after_seconds or 0)
            # Never shorten a provider's Retry-After to sneak an earlier retry.
            if delay > policy.maximum_delay:
                raise
            if current_run() and delay >= remaining_timeout(600):
                raise
            if control := current_run():
                control.note_retry()
                control.log("retry_scheduled", error_code=report.code)
            left = delay
            while left > 0:
                if control := current_run():
                    control.check()
                interval = min(left, 0.1)
                sleep(interval)
                left -= interval
            continue
        if control := current_run():
            control.check()
        return result
    raise RuntimeError("Unreachable bounded retry state")
