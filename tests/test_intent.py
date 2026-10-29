"""Phase C structured intent classification tests."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from careerpilot.application.agent_contracts import AgentCapability, AgentReference, AgentRequest
from careerpilot.application.intent import (
    DeterministicIntentModel,
    IntentClassification,
    IntentRouter,
)


def test_valid_classification_maps_to_a_supported_capability() -> None:
    classifier = IntentClassification(intent="analyze", confidence=0.95)
    router = IntentRouter(
        type("Classifier", (), {"classify": lambda self, _: classifier})(), lambda request: request
    )
    result = router.request(
        "analyze this job",
        user_id=uuid4(),
        conversation_id=uuid4(),
        task_id=uuid4(),
        allowed_capabilities=(AgentCapability.JOB_ANALYSIS,),
    )
    assert result.requested_capability == AgentCapability.JOB_ANALYSIS  # type: ignore[union-attr]


def test_route_invokes_supervisor_only_after_successful_classification() -> None:
    classifier = type(
        "Classifier",
        (),
        {"classify": lambda self, _: IntentClassification(intent="jobs", confidence=1)},
    )()
    calls: list[AgentCapability] = []

    def supervisor(request: AgentRequest) -> str:
        calls.append(request.requested_capability)
        return "routed"

    router = IntentRouter(classifier, supervisor)
    assert (
        router.route(
            "Show my jobs",
            user_id=uuid4(),
            conversation_id=uuid4(),
            task_id=uuid4(),
            allowed_capabilities=(AgentCapability.JOB_DISCOVERY,),
        )
        == "routed"
    )
    assert calls == [AgentCapability.JOB_DISCOVERY]


def test_low_confidence_requires_clarification() -> None:
    with pytest.raises(ValidationError, match="requires clarification"):
        IntentClassification(intent="jobs", confidence=0.4)
    router = IntentRouter(
        type(
            "Classifier",
            (),
            {
                "classify": lambda self, _: IntentClassification(
                    intent="jobs", confidence=0.4, clarification_required=True
                )
            },
        )(),
        lambda request: request,
    )
    result = router.request(
        "find something",
        user_id=uuid4(),
        conversation_id=uuid4(),
        task_id=uuid4(),
        allowed_capabilities=(AgentCapability.JOB_DISCOVERY,),
    )
    assert isinstance(result, IntentClassification) and result.clarification_required


def test_malformed_model_output_uses_only_safe_deterministic_fallback() -> None:
    router = IntentRouter(
        type("Classifier", (), {"classify": lambda self, _: {"unsupported": True}})(),
        lambda request: request,
    )
    result = router.request(
        "Show my jobs",
        user_id=uuid4(),
        conversation_id=uuid4(),
        task_id=uuid4(),
        allowed_capabilities=(AgentCapability.JOB_DISCOVERY,),
    )
    assert result.requested_capability == AgentCapability.JOB_DISCOVERY  # type: ignore[union-attr]


def test_unknown_text_falls_back_to_clarification_not_a_guess() -> None:
    router = IntentRouter(
        type(
            "Classifier", (), {"classify": lambda self, _: (_ for _ in ()).throw(TimeoutError())}
        )(),
        lambda request: request,
    )
    result = router.request(
        "Do something useful",
        user_id=uuid4(),
        conversation_id=uuid4(),
        task_id=uuid4(),
        allowed_capabilities=tuple(AgentCapability),
    )
    assert isinstance(result, IntentClassification)
    assert result.clarification_required


def test_fabricated_reference_requires_validation_and_is_not_forwarded() -> None:
    reference = AgentReference(kind="job", id=uuid4())
    classification = IntentClassification(
        intent="analyze", confidence=0.95, resource_references=(reference,)
    )
    router = IntentRouter(
        type("Classifier", (), {"classify": lambda self, _: classification})(),
        lambda request: request,
    )
    result = router.request(
        "analyze job",
        user_id=uuid4(),
        conversation_id=uuid4(),
        task_id=uuid4(),
        allowed_capabilities=(AgentCapability.JOB_ANALYSIS,),
    )
    assert isinstance(result, IntentClassification) and result.clarification_required


def test_unauthorized_reference_is_rejected_by_application_validator() -> None:
    reference = AgentReference(kind="job", id=uuid4())
    classification = IntentClassification(
        intent="analyze", confidence=0.95, resource_references=(reference,)
    )
    router = IntentRouter(
        type("Classifier", (), {"classify": lambda self, _: classification})(),
        lambda request: request,
        reference_validator=lambda _: (_ for _ in ()).throw(PermissionError()),
    )
    result = router.request(
        "analyze job",
        user_id=uuid4(),
        conversation_id=uuid4(),
        task_id=uuid4(),
        allowed_capabilities=(AgentCapability.JOB_ANALYSIS,),
    )
    assert isinstance(result, IntentClassification) and result.clarification_required


def test_deterministic_fallback_supports_only_existing_commands() -> None:
    assert DeterministicIntentModel().classify("Prepare my application").intent == "prepare"
    with pytest.raises(ValueError):
        DeterministicIntentModel().classify("Approve it")
