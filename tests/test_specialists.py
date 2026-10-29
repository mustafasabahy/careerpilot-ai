"""Phase D specialist wrapper tests."""

from uuid import uuid4

from careerpilot.application.agent_contracts import (
    AgentCapability,
    AgentRequest,
    AgentResultStatus,
    GlobalExecutionBudget,
)
from careerpilot.application.specialists import (
    AnalysisSpecialist,
    ApprovalSpecialist,
    ContextSpecialist,
    DiscoverySpecialist,
    PreparationSpecialist,
)


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


def test_specialists_have_stable_identities_and_capabilities() -> None:
    specialists = (
        DiscoverySpecialist(lambda _: type("Outcome", (), {})()),
        AnalysisSpecialist(lambda _: type("Outcome", (), {})()),
        PreparationSpecialist(lambda _: type("Outcome", (), {})()),
        ApprovalSpecialist(lambda _: type("Outcome", (), {})()),
        ContextSpecialist(lambda _: type("Outcome", (), {})()),
    )
    assert [(item.identity, item.capability) for item in specialists] == [
        ("discovery-specialist", AgentCapability.JOB_DISCOVERY),
        ("analysis-specialist", AgentCapability.JOB_ANALYSIS),
        ("preparation-specialist", AgentCapability.CAREER_PREPARATION),
        ("approval-specialist", AgentCapability.APPROVAL_COORDINATION),
        ("context-specialist", AgentCapability.CAREER_CONTEXT),
    ]


def test_wrapper_invokes_only_its_workflow_and_converts_result() -> None:
    calls: list[str] = []

    def invoke(_: AgentRequest) -> object:
        calls.append("analysis")
        return type("Outcome", (), {"status": "completed"})()

    result = AnalysisSpecialist(invoke).run(request(AgentCapability.JOB_ANALYSIS))
    assert result.status == AgentResultStatus.COMPLETED
    assert calls == ["analysis"]


def test_wrong_capability_does_not_invoke_workflow() -> None:
    called = False

    def invoke(_: AgentRequest) -> object:
        nonlocal called
        called = True
        return type("Outcome", (), {})()

    result = AnalysisSpecialist(invoke).run(request(AgentCapability.JOB_DISCOVERY))
    assert result.status == AgentResultStatus.FAILED
    assert result.errors[0].code.value == "unauthorized_capability"
    assert not called


def test_budget_and_authorization_failures_are_safe() -> None:
    result = DiscoverySpecialist(lambda _: object()).run(
        request(
            AgentCapability.JOB_DISCOVERY,
            budget=GlobalExecutionBudget(max_specialists=0),
        )
    )
    assert result.errors[0].code.value == "budget_exhausted"

    denied = AnalysisSpecialist(lambda _: (_ for _ in ()).throw(PermissionError("secret"))).run(
        request(AgentCapability.JOB_ANALYSIS)
    )
    assert denied.errors[0].code.value == "unauthorized_capability"
    assert "secret" not in denied.errors[0].message


def test_approval_pause_and_unknown_outcome_are_not_claimed_as_success() -> None:
    paused = ApprovalSpecialist(lambda _: type("Outcome", (), {"paused": True})()).run(
        request(AgentCapability.APPROVAL_COORDINATION)
    )
    assert paused.status == AgentResultStatus.WAITING_FOR_USER_ACTION

    unknown = ApprovalSpecialist(
        lambda _: type(
            "Outcome",
            (),
            {"review": type("Review", (), {"status": "unknown"})()},
        )()
    ).run(request(AgentCapability.APPROVAL_COORDINATION))
    assert unknown.status == AgentResultStatus.PARTIAL
    assert unknown.errors[0].code.value == "unknown_outcome"
    assert unknown.errors[0].uncertain
