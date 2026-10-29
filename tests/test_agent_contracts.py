"""Behavior-neutral Phase A contract tests."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from careerpilot.application.agent_contracts import (
    AgentCapability,
    AgentError,
    AgentErrorCode,
    AgentReference,
    AgentRequest,
    AgentResult,
    AgentResultStatus,
    AgentSideEffect,
    DelegationEvent,
    DelegationEventStatus,
    GlobalExecutionBudget,
)


def request(**changes: object) -> AgentRequest:
    values: dict[str, object] = {
        "user_id": uuid4(),
        "conversation_id": uuid4(),
        "task_id": uuid4(),
        "requested_capability": AgentCapability.JOB_ANALYSIS,
        "allowed_capabilities": (AgentCapability.JOB_ANALYSIS,),
    }
    values.update(changes)
    return AgentRequest.model_validate(values)


def test_request_uses_ids_and_round_trips() -> None:
    value = request(input_references=(AgentReference(kind="job", id=uuid4()),))
    restored = AgentRequest.model_validate_json(value.model_dump_json())
    assert restored == value


def test_request_rejects_disallowed_capability_and_same_parent() -> None:
    with pytest.raises(ValidationError, match="not in the allowed"):
        request(allowed_capabilities=(AgentCapability.JOB_DISCOVERY,))
    task_id = uuid4()
    with pytest.raises(ValidationError, match="must differ"):
        request(task_id=task_id, parent_task_id=task_id)


def test_result_supports_partial_and_waiting_states() -> None:
    reference = AgentReference(kind="assessment", id=uuid4())
    for status in (AgentResultStatus.PARTIAL, AgentResultStatus.WAITING_FOR_USER_ACTION):
        value = AgentResult(
            status=status,
            summary="More information is required.",
            references=(reference,),
            required_user_action="Select a job",
        )
        assert AgentResult.model_validate_json(value.model_dump_json()) == value


def test_error_is_structured_bounded_and_safe() -> None:
    value = AgentError(
        code=AgentErrorCode.RETRYABLE_FAILURE,
        message="The source is temporarily unavailable.",
        retryable=True,
    )
    assert value.model_dump(mode="json")["code"] == "retryable_failure"
    assert "token" not in value.message.casefold()


def test_result_effect_does_not_claim_unconfirmed_execution() -> None:
    value = AgentResult(
        status=AgentResultStatus.COMPLETED,
        summary="A proposal is ready for review.",
        side_effects=(AgentSideEffect(kind="recruiter_message", status="proposed"),),
    )
    assert value.side_effects[0].status == "proposed"


def test_delegation_event_validates_ids_and_round_trips() -> None:
    value = DelegationEvent(
        event_id=uuid4(),
        parent_task_id=uuid4(),
        child_task_id=uuid4(),
        parent_agent_id="career_supervisor",
        capability=AgentCapability.CAREER_PREPARATION,
        reason="Prepare evidence-linked drafts.",
        timestamp=datetime.now(UTC),
        status=DelegationEventStatus.REQUESTED,
    )
    assert DelegationEvent.model_validate_json(value.model_dump_json()) == value
    with pytest.raises(ValidationError, match="must differ"):
        DelegationEvent.model_validate(
            {**value.model_dump(), "child_task_id": value.parent_task_id}
        )


def test_budget_boundaries_and_missing_limits_are_distinct() -> None:
    empty = GlobalExecutionBudget()
    assert not empty.exhausted("model_calls")
    zero = GlobalExecutionBudget(max_model_calls=0)
    assert zero.exhausted("model_calls")
    assert GlobalExecutionBudget(max_model_calls=1, model_calls=1).exhausted("model_calls")
    with pytest.raises(ValidationError):
        GlobalExecutionBudget(max_tool_calls=-1)
    with pytest.raises(ValidationError, match="exceeds"):
        GlobalExecutionBudget(max_tool_calls=1, tool_calls=2)


def test_budget_unknown_resource_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown budget resource"):
        GlobalExecutionBudget().exhausted("not_a_resource")
