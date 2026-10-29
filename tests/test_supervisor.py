"""Phase B deterministic supervisor tests with isolated workflow fakes."""

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from time import sleep
from uuid import uuid4

import pytest
from pydantic import ValidationError

from careerpilot.application.agent_contracts import (
    AgentCapability,
    AgentRequest,
    AgentResultStatus,
    DelegationEvent,
    DelegationEventStatus,
    GlobalExecutionBudget,
)
from careerpilot.application.execution import RunControl, consume, execution_scope, scoped_limits
from careerpilot.application.failures import BudgetExceeded, DeadlineExceeded
from careerpilot.application.supervisor import CareerSupervisor
from careerpilot.domain.reliability import ExecutionLimits


def request(capability: AgentCapability, **changes: object) -> AgentRequest:
    values: dict[str, object] = {
        "user_id": uuid4(),
        "conversation_id": uuid4(),
        "task_id": uuid4(),
        "requested_capability": capability,
        "allowed_capabilities": (capability,),
    }
    values.update(changes)
    return AgentRequest.model_validate(values)


def test_routes_each_supported_capability_to_only_its_invoker() -> None:
    calls: list[str] = []

    def invoke(name: str) -> Callable[[AgentRequest], object]:
        def run(_: AgentRequest) -> object:
            calls.append(name)
            return type("Outcome", (), {"status": "completed"})()

        return run

    supervisor = CareerSupervisor(
        {
            AgentCapability.JOB_DISCOVERY: invoke("jobs"),
            AgentCapability.JOB_ANALYSIS: invoke("analyze"),
            AgentCapability.CAREER_PREPARATION: invoke("prepare"),
            AgentCapability.APPROVAL_COORDINATION: invoke("approvals"),
        }
    )
    for capability in (
        AgentCapability.JOB_DISCOVERY,
        AgentCapability.JOB_ANALYSIS,
        AgentCapability.CAREER_PREPARATION,
        AgentCapability.APPROVAL_COORDINATION,
    ):
        assert supervisor.run(request(capability)).status == AgentResultStatus.COMPLETED
    assert calls == ["jobs", "analyze", "prepare", "approvals"]


def test_unsupported_capability_does_not_invoke_any_workflow() -> None:
    called = False

    def invoke(_: AgentRequest) -> object:
        nonlocal called
        called = True
        return type("Outcome", (), {"status": "completed"})()

    result = CareerSupervisor({AgentCapability.JOB_ANALYSIS: invoke}).run(
        request(AgentCapability.INTERVIEW_PREPARATION)
    )
    assert result.status == AgentResultStatus.FAILED
    assert result.errors[0].code.value == "unauthorized_capability"
    assert not called


def test_empty_parent_allow_list_is_rejected() -> None:
    with pytest.raises(ValidationError):
        request(AgentCapability.JOB_ANALYSIS, allowed_capabilities=())


def test_parent_allow_list_rejects_one_unauthorized_child() -> None:
    supervisor = CareerSupervisor(
        {
            AgentCapability.JOB_DISCOVERY: lambda _: object(),
            AgentCapability.JOB_ANALYSIS: lambda _: object(),
        }
    )
    result = supervisor.delegate(
        request(AgentCapability.JOB_DISCOVERY),
        (AgentCapability.JOB_DISCOVERY, AgentCapability.JOB_ANALYSIS),
    )
    assert result.errors[0].code.value == "unauthorized_capability"


def test_parent_allow_list_allows_every_requested_child() -> None:
    supervisor = CareerSupervisor(
        {
            AgentCapability.JOB_DISCOVERY: lambda _: type("Outcome", (), {"status": "completed"})(),
            AgentCapability.JOB_ANALYSIS: lambda _: type("Outcome", (), {"status": "completed"})(),
        }
    )
    result = supervisor.delegate(
        request(
            AgentCapability.JOB_DISCOVERY,
            allowed_capabilities=(AgentCapability.JOB_DISCOVERY, AgentCapability.JOB_ANALYSIS),
        ),
        (AgentCapability.JOB_DISCOVERY, AgentCapability.JOB_ANALYSIS),
    )
    assert result.status == AgentResultStatus.COMPLETED


def test_budget_exhaustion_prevents_workflow_invocation() -> None:
    called = False

    def invoke(_: AgentRequest) -> object:
        nonlocal called
        called = True
        return type("Outcome", (), {"status": "completed"})()

    budget = GlobalExecutionBudget(max_specialists=0)
    result = CareerSupervisor({AgentCapability.JOB_ANALYSIS: invoke}).run(
        request(AgentCapability.JOB_ANALYSIS, budget=budget)
    )
    assert result.errors[0].code.value == "budget_exhausted"
    assert not called


def test_global_tool_budget_is_enforced_at_supervisor_boundary() -> None:
    def invoke(_: AgentRequest) -> object:
        consume("tool_calls")
        return type("Outcome", (), {"status": "completed"})()

    result = CareerSupervisor({AgentCapability.JOB_ANALYSIS: invoke}).run(
        request(
            AgentCapability.JOB_ANALYSIS,
            budget=GlobalExecutionBudget(max_tool_calls=0),
        )
    )
    assert result.status == AgentResultStatus.FAILED
    assert result.errors[0].code.value == "budget_exhausted"


@pytest.mark.parametrize("resource", ["model_calls", "steps"])
def test_global_budget_is_exhausted_during_supervisor_execution(resource: str) -> None:
    def invoke(_: AgentRequest) -> object:
        consume(resource)  # type: ignore[arg-type]
        consume(resource)  # type: ignore[arg-type]
        return type("Outcome", (), {"status": "completed"})()

    budget = GlobalExecutionBudget(**{f"max_{resource}": 1})
    result = CareerSupervisor({AgentCapability.JOB_ANALYSIS: invoke}).run(
        request(AgentCapability.JOB_ANALYSIS, budget=budget)
    )
    assert result.status == AgentResultStatus.FAILED
    assert result.errors[0].code.value == "budget_exhausted"


def test_time_budget_is_exhausted_during_supervisor_execution() -> None:
    def invoke(_: AgentRequest) -> object:
        sleep(0.01)
        consume("steps")
        return type("Outcome", (), {"status": "completed"})()

    result = CareerSupervisor({AgentCapability.JOB_ANALYSIS: invoke}).run(
        request(AgentCapability.JOB_ANALYSIS, budget=GlobalExecutionBudget(max_seconds=0.001))
    )
    assert result.status == AgentResultStatus.FAILED
    assert result.errors[0].code.value == "deadline_exceeded"


@pytest.mark.parametrize(
    ("budget", "resource"),
    [
        (GlobalExecutionBudget(max_model_calls=0), "model_calls"),
        (GlobalExecutionBudget(max_seconds=0), "seconds"),
        (GlobalExecutionBudget(max_steps=0), "steps"),
    ],
)
def test_global_budget_exhaustion_is_rejected_before_execution(
    budget: GlobalExecutionBudget, resource: str
) -> None:
    result = CareerSupervisor({AgentCapability.JOB_ANALYSIS: lambda _: object()}).run(
        request(AgentCapability.JOB_ANALYSIS, budget=budget)
    )
    assert result.status == AgentResultStatus.FAILED
    assert result.errors[0].code.value == "budget_exhausted", resource


def test_missing_workflow_is_safe_configuration_error() -> None:
    result = CareerSupervisor({}).run(request(AgentCapability.JOB_ANALYSIS))
    assert result.status == AgentResultStatus.FAILED
    assert result.errors[0].code.value == "internal_failure"


def test_outcome_conversion_preserves_waiting_state_and_reference() -> None:
    assessment_id = uuid4()
    outcome = type("Outcome", (), {"status": "needs_information", "assessment_id": assessment_id})()
    result = CareerSupervisor({AgentCapability.JOB_ANALYSIS: lambda _: outcome}).run(
        request(AgentCapability.JOB_ANALYSIS)
    )
    assert result.status == AgentResultStatus.WAITING_FOR_USER_ACTION
    assert result.required_user_action
    assert result.references[0].id == assessment_id


def test_workflow_failure_is_converted_without_leaking_details() -> None:
    def invoke(_: AgentRequest) -> object:
        raise PermissionError("secret-token must not escape")

    result = CareerSupervisor({AgentCapability.JOB_ANALYSIS: invoke}).run(
        request(AgentCapability.JOB_ANALYSIS)
    )
    assert result.errors[0].code.value == "unauthorized_capability"
    assert "secret-token" not in result.errors[0].message


def test_request_validation_rejects_disallowed_capability_before_supervisor() -> None:
    with pytest.raises(ValidationError):
        request(
            AgentCapability.JOB_ANALYSIS,
            allowed_capabilities=(AgentCapability.JOB_DISCOVERY,),
        )


def test_sequential_delegation_tracks_children_and_aggregates_results() -> None:
    calls: list[tuple[AgentCapability, object, tuple[str, ...]]] = []

    def invoke(request_value: AgentRequest) -> object:
        calls.append(
            (
                request_value.requested_capability,
                request_value.parent_task_id,
                tuple(item.summary for item in request_value.delegation_context),
            )
        )
        return type("Outcome", (), {"status": "completed"})()

    supervisor = CareerSupervisor(
        {
            AgentCapability.JOB_DISCOVERY: invoke,
            AgentCapability.JOB_ANALYSIS: invoke,
        }
    )
    result = supervisor.delegate(
        request(
            AgentCapability.JOB_DISCOVERY,
            allowed_capabilities=(AgentCapability.JOB_DISCOVERY, AgentCapability.JOB_ANALYSIS),
        ),
        (AgentCapability.JOB_DISCOVERY, AgentCapability.JOB_ANALYSIS),
    )
    assert result.status == AgentResultStatus.COMPLETED
    assert [item[0] for item in calls] == [
        AgentCapability.JOB_DISCOVERY,
        AgentCapability.JOB_ANALYSIS,
    ]
    assert all(parent is not None for _, parent, _ in calls)
    assert calls[0][2] == ()
    assert calls[1][2] == ("The workflow completed successfully.",)
    assert len(supervisor.delegation_events) == 2
    assert all(
        event.parent_task_id != event.child_task_id for event in supervisor.delegation_events
    )


def test_delegation_enforces_limits_duplicates_and_unregistered_capabilities() -> None:
    called = 0

    def invoke(_: AgentRequest) -> object:
        nonlocal called
        called += 1
        return type("Outcome", (), {"status": "completed"})()

    supervisor = CareerSupervisor({AgentCapability.JOB_ANALYSIS: invoke})
    base = request(
        AgentCapability.JOB_ANALYSIS,
        budget=GlobalExecutionBudget(max_specialists=1),
    )
    limited = supervisor.delegate(
        base, (AgentCapability.JOB_ANALYSIS, AgentCapability.JOB_ANALYSIS)
    )
    assert limited.errors[0].code.value == "validation_failure"
    assert called == 0

    unregistered = supervisor.delegate(
        request(AgentCapability.JOB_ANALYSIS), (AgentCapability.JOB_DISCOVERY,)
    )
    assert unregistered.errors[0].code.value == "unauthorized_capability"

    depth = supervisor.delegate(
        request(
            AgentCapability.JOB_ANALYSIS,
            budget=GlobalExecutionBudget(max_delegation_depth=0),
        ),
        (AgentCapability.JOB_ANALYSIS,),
    )
    assert depth.errors[0].code.value == "budget_exhausted"


def test_delegation_preserves_child_failure_and_detects_conflicts() -> None:
    def failed(_: AgentRequest) -> object:
        return type(
            "Outcome", (), {"status": "error", "code": "not_found", "message": "not found"}
        )()

    supervisor = CareerSupervisor({AgentCapability.JOB_ANALYSIS: failed})
    result = supervisor.delegate(
        request(AgentCapability.JOB_ANALYSIS), (AgentCapability.JOB_ANALYSIS,)
    )
    assert result.status == AgentResultStatus.FAILED
    assert result.errors[0].code.value == "not_found"

    first_id, second_id = uuid4(), uuid4()
    outcomes = iter(
        (
            type("Outcome", (), {"status": "completed", "assessment_id": first_id})(),
            type("Outcome", (), {"status": "completed", "assessment_id": second_id})(),
        )
    )
    conflict = CareerSupervisor(
        {
            AgentCapability.JOB_DISCOVERY: lambda _: next(outcomes),
            AgentCapability.JOB_ANALYSIS: lambda _: next(outcomes),
        }
    ).delegate(
        request(
            AgentCapability.JOB_DISCOVERY,
            allowed_capabilities=(AgentCapability.JOB_DISCOVERY, AgentCapability.JOB_ANALYSIS),
        ),
        (AgentCapability.JOB_DISCOVERY, AgentCapability.JOB_ANALYSIS),
    )
    assert conflict.status == AgentResultStatus.PARTIAL
    assert any(error.code.value == "conflicting_results" for error in conflict.errors)


def test_nested_delegation_updates_exact_events_and_failure_is_terminal() -> None:
    supervisor: CareerSupervisor
    nested = False

    def invoke(request_value: AgentRequest) -> object:
        nonlocal nested
        if not nested:
            nested = True
            supervisor.delegate(request_value, (AgentCapability.JOB_ANALYSIS,))
        raise RuntimeError("synthetic failure")

    supervisor = CareerSupervisor({AgentCapability.JOB_ANALYSIS: invoke})
    result = supervisor.delegate(
        request(AgentCapability.JOB_ANALYSIS), (AgentCapability.JOB_ANALYSIS,)
    )
    assert result.status == AgentResultStatus.FAILED
    assert len(supervisor.delegation_events) == 2
    assert all(
        event.status == DelegationEventStatus.FAILED for event in supervisor.delegation_events
    )
    assert all(event.error is not None for event in supervisor.delegation_events)


def test_concurrent_delegations_isolate_event_state_per_execution() -> None:
    supervisor = CareerSupervisor(
        {AgentCapability.JOB_ANALYSIS: lambda _: type("Outcome", (), {"status": "completed"})()}
    )

    def execute() -> tuple[DelegationEvent, ...]:
        result = supervisor.delegate(
            request(AgentCapability.JOB_ANALYSIS), (AgentCapability.JOB_ANALYSIS,)
        )
        assert result.status == AgentResultStatus.COMPLETED
        return supervisor.delegation_events

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(lambda _: execute(), (1, 2))
    assert len(first) == len(second) == 1
    assert first[0].event_id != second[0].event_id
    assert first[0].status == second[0].status == DelegationEventStatus.COMPLETED


def test_asyncio_tasks_isolate_inherited_delegation_state() -> None:
    supervisor = CareerSupervisor(
        {AgentCapability.JOB_ANALYSIS: lambda _: type("Outcome", (), {"status": "completed"})()}
    )
    # Seed a parent context so gather tasks inherit a mutable-state candidate.
    supervisor.delegate(request(AgentCapability.JOB_ANALYSIS), (AgentCapability.JOB_ANALYSIS,))

    async def execute() -> tuple[DelegationEvent, ...]:
        await asyncio.sleep(0)
        result = supervisor.delegate(
            request(AgentCapability.JOB_ANALYSIS), (AgentCapability.JOB_ANALYSIS,)
        )
        assert result.status == AgentResultStatus.COMPLETED
        return supervisor.delegation_events

    async def run() -> tuple[tuple[DelegationEvent, ...], tuple[DelegationEvent, ...]]:
        return await asyncio.gather(execute(), execute())

    first, second = asyncio.run(run())
    assert len(first) == len(second) == 1
    assert first[0].event_id != second[0].event_id
    assert first[0].status == second[0].status == DelegationEventStatus.COMPLETED


def test_nested_execution_ceilings_compose_and_restore() -> None:
    with execution_scope(
        RunControl(ExecutionLimits(max_model_calls=3, max_tool_calls=3, max_steps=3))
    ):
        with scoped_limits(ExecutionLimits(max_model_calls=2, max_tool_calls=2, max_steps=2)):
            with scoped_limits(
                ExecutionLimits(max_model_calls=10, max_tool_calls=10, max_steps=10)
            ):
                consume("model_calls", 2)
                consume("tool_calls", 2)
                consume("steps", 2)
                with pytest.raises(BudgetExceeded):
                    consume("model_calls")
                with pytest.raises(BudgetExceeded):
                    consume("tool_calls")
                with pytest.raises(BudgetExceeded):
                    consume("steps")
        consume("model_calls")
        consume("tool_calls")
        consume("steps")
        with pytest.raises(BudgetExceeded):
            consume("model_calls")
        with pytest.raises(BudgetExceeded):
            consume("tool_calls")
        with pytest.raises(BudgetExceeded):
            consume("steps")


def test_nested_deadline_cannot_be_extended() -> None:
    with execution_scope(RunControl(ExecutionLimits(overall_seconds=1))):
        with scoped_limits(ExecutionLimits(overall_seconds=0.01)):
            with scoped_limits(ExecutionLimits(overall_seconds=1)):
                sleep(0.02)
                with pytest.raises(DeadlineExceeded):
                    consume("steps")
