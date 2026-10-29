"""Phase B deterministic supervisor tests with isolated workflow fakes."""

from collections.abc import Callable
from uuid import uuid4

import pytest
from pydantic import ValidationError

from careerpilot.application.agent_contracts import (
    AgentCapability,
    AgentRequest,
    AgentResultStatus,
    GlobalExecutionBudget,
)
from careerpilot.application.supervisor import CareerSupervisor


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
    calls: list[tuple[AgentCapability, object]] = []

    def invoke(request_value: AgentRequest) -> object:
        calls.append((request_value.requested_capability, request_value.parent_task_id))
        return type("Outcome", (), {"status": "completed"})()

    supervisor = CareerSupervisor(
        {
            AgentCapability.JOB_DISCOVERY: invoke,
            AgentCapability.JOB_ANALYSIS: invoke,
        }
    )
    result = supervisor.delegate(
        request(AgentCapability.JOB_DISCOVERY),
        (AgentCapability.JOB_DISCOVERY, AgentCapability.JOB_ANALYSIS),
    )
    assert result.status == AgentResultStatus.COMPLETED
    assert [item[0] for item in calls] == [
        AgentCapability.JOB_DISCOVERY,
        AgentCapability.JOB_ANALYSIS,
    ]
    assert all(parent is not None for _, parent in calls)
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
        request(AgentCapability.JOB_DISCOVERY),
        (AgentCapability.JOB_DISCOVERY, AgentCapability.JOB_ANALYSIS),
    )
    assert conflict.status == AgentResultStatus.PARTIAL
    assert any(error.code.value == "conflicting_results" for error in conflict.errors)
