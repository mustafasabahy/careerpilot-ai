"""Bounded retry policy, cooperative budgets, safe failure reports and operation metadata."""

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from uuid import uuid4

import pytest
import test_workflow
from pydantic import ValidationError
from test_action_approval import Setup
from test_action_approval import setup as setup
from test_discovery import discovery_request
from test_discovery import discovery_setup as discovery_setup

from careerpilot.application.action_contracts import (
    ActionConflict,
    ActionDenied,
    AmbiguousExecution,
)
from careerpilot.application.analysis import AnalysisService
from careerpilot.application.discovery import DiscoveryTools
from careerpilot.application.discovery_contracts import AccessGrant, SourceTimeout
from careerpilot.application.execution import (
    Resource,
    RunControl,
    consume,
    current_run,
    execution_scope,
    remaining_timeout,
    retry_read,
)
from careerpilot.application.extraction import (
    InvalidModelOutput,
    ProviderAuthenticationError,
    ProviderFailure,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderTransientFailure,
)
from careerpilot.application.failures import (
    BudgetExceeded,
    DeadlineExceeded,
    RunCancelled,
    failure_report,
)
from careerpilot.config import ReliabilitySettings
from careerpilot.domain.reliability import ExecutionLimits, RetryPolicy
from careerpilot.integrations.synthetic import SyntheticBundle
from careerpilot.observability import SafeJsonFormatter
from careerpilot.persistence.run_journal import SQLiteRunJournal
from careerpilot.persistence.sqlite import SQLiteDatabase
from careerpilot.providers.openai import retry_after
from careerpilot.workflows.discovery import DiscoveryWorkflow
from careerpilot.workflows.job_analysis import JobAnalysisWorkflow

analysis_setup = test_workflow.setup


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.waited = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.now += delay
        self.waited += delay


@pytest.mark.parametrize(
    "error,code,retryable,uncertain",
    [
        (ValueError("PRIVATE"), "invalid_input", False, False),
        (ActionDenied("PRIVATE"), "unauthorized", False, False),
        (ActionConflict("PRIVATE"), "conflict", False, False),
        (InvalidModelOutput("PRIVATE"), "invalid_output", False, False),
        (ProviderAuthenticationError("PRIVATE"), "unauthorized", False, False),
        (ProviderFailure("PRIVATE"), "unavailable", False, False),
        (ProviderTransientFailure("PRIVATE"), "unavailable", True, False),
        (ProviderTimeout("PRIVATE"), "timeout", True, False),
        (SourceTimeout(), "timeout", True, False),
        (ProviderRateLimited("PRIVATE"), "rate_limited", True, False),
        (ProviderRateLimited("PRIVATE", retryable=False), "rate_limited", False, False),
        (AmbiguousExecution("PRIVATE"), "unknown_outcome", False, True),
        (BudgetExceeded(), "limit_exceeded", False, False),
        (DeadlineExceeded(), "timeout", False, False),
        (RunCancelled(), "cancelled", False, False),
        (RuntimeError("PRIVATE"), "internal", False, False),
    ],
)
def test_error_taxonomy_does_not_echo_private_errors(
    error: Exception, code: str, retryable: bool, uncertain: bool
) -> None:
    report = failure_report(error)
    assert (report.code, report.retryable, report.uncertain) == (code, retryable, uncertain)
    assert "PRIVATE" not in report.model_dump_json()


@pytest.mark.parametrize(
    "error", [ActionDenied(), ValueError(), AmbiguousExecution(), ProviderFailure()]
)
def test_permanent_or_uncertain_errors_never_retry(error: Exception) -> None:
    attempts: list[int] = []

    def fail() -> int:
        attempts.append(1)
        raise error

    with pytest.raises(type(error)):
        retry_read(fail, RetryPolicy(max_retries=2, initial_delay=0))
    assert attempts == [1]


def test_rate_limit_wait_respects_guidance_and_transient_failure_recovers() -> None:
    clock = Clock()
    attempts: list[int] = []
    control = RunControl(clock=clock)

    def read() -> int:
        attempts.append(1)
        if len(attempts) == 1:
            raise ProviderRateLimited("PRIVATE", retry_after_seconds=2)
        return 42

    with execution_scope(control):
        assert (
            retry_read(read, RetryPolicy(max_retries=1), resource="model_calls", sleep=clock.sleep)
            == 42
        )
    assert len(attempts) == 2 and clock.waited >= 2 - 1e-8
    assert control.record.model_calls == 2 and control.record.status == "completed"


@pytest.mark.parametrize("guidance", [11, 200])
def test_large_retry_after_is_not_capped_into_an_early_retry(guidance: float) -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    def read() -> None:
        attempts.append(1)
        raise ProviderRateLimited("PRIVATE", retry_after_seconds=guidance)

    with pytest.raises(ProviderRateLimited):
        retry_read(read, RetryPolicy(max_retries=2), sleep=sleeps.append)
    assert attempts == [1] and sleeps == []


def test_retry_wait_that_exceeds_deadline_does_not_start() -> None:
    control = RunControl(ExecutionLimits(overall_seconds=1))
    with pytest.raises(ProviderRateLimited), execution_scope(control):
        retry_read(
            lambda: (_ for _ in ()).throw(ProviderRateLimited("PRIVATE", retry_after_seconds=2)),
            RetryPolicy(max_retries=2),
        )
    assert control.record.status == "failed"


def test_retry_attempts_share_model_budget() -> None:
    attempts: list[int] = []
    control = RunControl(ExecutionLimits(max_model_calls=1))

    def fail() -> None:
        attempts.append(1)
        raise ProviderTimeout("PRIVATE")

    with pytest.raises(BudgetExceeded), execution_scope(control):
        retry_read(fail, RetryPolicy(max_retries=2, initial_delay=0), resource="model_calls")
    assert attempts == [1] and control.record.model_calls == 1


@pytest.mark.parametrize("resource", ["steps", "tool_calls", "retrieved_jobs", "model_calls"])
def test_resource_limits_prevent_overrun(resource: Resource) -> None:
    limits = ExecutionLimits.model_validate({f"max_{resource}": 1})
    control = RunControl(limits)
    with pytest.raises(BudgetExceeded), execution_scope(control):
        control.consume(resource)
        control.consume(resource)
    assert getattr(control.record, resource) == 1


def test_deadline_and_cancellation_checked_before_read_calls() -> None:
    clock = Clock()
    control = RunControl(ExecutionLimits(overall_seconds=1), clock=clock)
    with pytest.raises(DeadlineExceeded), execution_scope(control):
        assert remaining_timeout(30) == 1
        clock.now = 2
        consume("tool_calls")
    cancelled = RunControl()
    cancelled.cancel()
    with pytest.raises(RunCancelled), execution_scope(cancelled):
        raise AssertionError("Operation must not run")
    assert cancelled.record.status == "cancelled"


def test_run_journal_restart_scoping_and_persisted_cancel(tmp_path: Path) -> None:
    owner = uuid4()
    path = tmp_path / "runs.sqlite3"
    journal = SQLiteRunJournal(path, owner)
    control = RunControl(owner_id=owner, journal=journal)
    operation_id = control.record.operation_id
    reopened = SQLiteRunJournal(path, owner)
    assert reopened.read(operation_id).status == "running"
    assert reopened.request_cancel(operation_id)
    with pytest.raises(RunCancelled), execution_scope(control):
        consume("steps")
    assert reopened.read(operation_id).status == "cancelled"
    with pytest.raises(ActionConflict):
        RunControl(owner_id=owner, journal=journal, operation_id=operation_id)
    other = SQLiteRunJournal(path, uuid4())
    with pytest.raises(ActionDenied):
        other.read(operation_id)
    with pytest.raises(ActionDenied):
        other.request_cancel(operation_id)


def test_completed_operation_metadata_persists_and_id_cannot_be_reused(tmp_path: Path) -> None:
    owner = uuid4()
    journal = SQLiteRunJournal(tmp_path / "runs.sqlite3", owner)
    control = RunControl(owner_id=owner, journal=journal)
    with execution_scope(control):
        consume("steps", 2)
    restored = journal.read(control.record.operation_id)
    assert restored.status == "completed" and restored.steps == 2
    assert not journal.request_cancel(restored.operation_id)
    with pytest.raises(BudgetExceeded), execution_scope(control):
        raise AssertionError("A completed invocation is not replayed")


def test_logs_allow_only_uuid_correlation_fields() -> None:
    record = logging.LogRecord("careerpilot", logging.INFO, "", 0, "PRIVATE %s", ("PRIVATE",), None)
    record.event = "execution_failed"
    record.correlation_id = uuid4()
    record.workflow_id = uuid4()
    record.operation_id = uuid4()
    record.error_code = "unknown_outcome"
    record.exception_text = "PRIVATE"
    rendered = SafeJsonFormatter().format(record)
    assert "PRIVATE" not in rendered
    assert set(json.loads(rendered)) == {
        "timestamp",
        "level",
        "event",
        "correlation_id",
        "workflow_id",
        "operation_id",
        "error_code",
    }
    record.correlation_id = "PRIVATE"
    record.error_code = "PRIVATE"
    assert "PRIVATE" not in SafeJsonFormatter().format(record)


def test_header_parser_handles_seconds_milliseconds_dates_and_invalid_values() -> None:
    assert retry_after({"retry-after-ms": "1250"}) == 1.25
    assert retry_after({"retry-after": "3"}) == 3
    date = format_datetime(datetime.now(UTC) + timedelta(seconds=20), usegmt=True)
    assert 18 <= (retry_after({"retry-after": date}) or 0) <= 20
    for value in ("nan", "inf", "-1", "not-a-date"):
        assert retry_after({"retry-after": value}) is None


def test_environment_limits_are_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAREERPILOT_RUN_MAX_MODEL_CALLS", "0")
    assert ReliabilitySettings().limits().max_model_calls == 0
    monkeypatch.setenv("CAREERPILOT_RUN_MAX_STEPS", "0")
    with pytest.raises(ValidationError):
        ReliabilitySettings().limits()


def test_actual_graph_steps_and_stream_context_isolation(
    analysis_setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
) -> None:
    _, bundle, _, workflow = analysis_setup
    with pytest.raises(BudgetExceeded), execution_scope(RunControl(ExecutionLimits(max_steps=1))):
        workflow.run(bundle.request())
    stream = workflow.updates(bundle.request())
    next(stream)
    assert current_run() is None
    list(stream)


def test_actual_discovery_tool_and_retrieval_budgets(
    discovery_setup: tuple[SQLiteDatabase, SyntheticBundle, AccessGrant, DiscoveryTools],
) -> None:
    _, bundle, _, service = discovery_setup
    workflow = DiscoveryWorkflow(service)
    for limits in (ExecutionLimits(max_tool_calls=0), ExecutionLimits(max_retrieved_jobs=0)):
        with pytest.raises(BudgetExceeded), execution_scope(RunControl(limits)):
            workflow.run(discovery_request(bundle))


@pytest.mark.parametrize("stage", ["pending", "approved", "executing"])
def test_durable_action_cancellation_preserves_uncertainty(setup: Setup, stage: str) -> None:
    review = setup.workflow.prepare(setup.request()).review
    if stage != "pending":
        setup.service.decide(setup.decision(review))
    if stage == "executing":
        with setup.actions.transaction() as repository:
            repository.claim(review.proposal.id, setup.service.principal.actor_id)
    result = setup.service.cancel(review.proposal.thread_id)
    assert (
        result.status
        == {"pending": "rejected", "approved": "invalidated", "executing": "unknown"}[stage]
    )
    assert setup.workflow.recover(review.proposal.thread_id).review.status == result.status
    assert setup.adapter.effect_count() == 0


def test_unknown_typed_outcome_marks_run_uncertain_not_success(setup: Setup) -> None:
    setup.adapter.behavior = "ack_lost"
    review = setup.workflow.prepare(setup.request()).review
    control = RunControl()
    with execution_scope(control):
        result = setup.workflow.resume(setup.decision(review))
    assert result.review.status == "unknown"
    assert control.record.status == "failed" and control.record.failure is not None
    assert control.record.failure.uncertain


def test_interrupted_write_rolls_back_without_automatic_retry(setup: Setup) -> None:
    review = setup.workflow.prepare(setup.request()).review
    setup.service.decide(setup.decision(review))
    with sqlite3.connect(setup.database.path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_outcome BEFORE INSERT ON action_outcomes "
            "BEGIN SELECT RAISE(ABORT,'PRIVATE'); END"
        )
    with pytest.raises(sqlite3.IntegrityError) as error:
        setup.service.execute(review.proposal.thread_id)
    assert failure_report(error.value).code == "integrity"
    assert setup.adapter.effect_count() == 1
    with setup.actions.transaction() as repository:
        assert repository.status(review.proposal.id) == "executing"
        assert repository.outcome(review.proposal.id) is None
    with sqlite3.connect(setup.database.path) as connection:
        connection.execute("DROP TRIGGER fail_outcome")
    assert setup.workflow.recover(review.proposal.thread_id).review.status == "unknown"
    assert setup.adapter.effect_count() == 1


def test_model_timeout_retries_are_bounded_and_counted() -> None:
    import httpx2 as httpx
    from langchain_core.messages import AIMessage, BaseMessage
    from openai import APITimeoutError

    from careerpilot.integrations.extraction_fixture import mock_extraction_payload
    from careerpilot.providers.openai import OpenAIJobModel

    calls: list[int] = []

    def invoke(messages: list[BaseMessage]) -> object:
        calls.append(1)
        if len(calls) == 1:
            raise APITimeoutError(request=httpx.Request("POST", "https://example.invalid"))
        return {
            "raw": AIMessage(content=""),
            "parsed": mock_extraction_payload(),
            "parsing_error": None,
        }

    control = RunControl()
    with execution_scope(control):
        OpenAIJobModel("synthetic", invoke, max_retries=1).extract("Synthetic source")
    assert calls == [1, 1] and control.record.model_calls == 2
