"""Optional structured intent classification before deterministic supervision."""

from collections.abc import Callable
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from careerpilot.application.agent_contracts import AgentCapability, AgentReference, AgentRequest
from careerpilot.application.conversations import parse_command
from careerpilot.domain.common import Model, Text

Intent = Literal["jobs", "analyze", "prepare", "approvals"]


class IntentClassification(Model):
    """Strict model output; IDs must come from application context, never the model."""

    intent: Intent
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    resource_references: tuple[AgentReference, ...] = Field(default=(), max_length=8)
    missing_information: tuple[Text, ...] = Field(default=(), max_length=8)
    clarification_required: bool = False

    @model_validator(mode="after")
    def low_confidence_requires_clarification(self) -> "IntentClassification":
        if self.confidence < 0.75 and not self.clarification_required:
            raise ValueError("Low-confidence intent requires clarification")
        return self


class IntentModel(Protocol):
    def classify(self, text: str) -> IntentClassification | object: ...


class DeterministicIntentModel:
    """Offline fallback using the existing bounded command parser only."""

    def classify(self, text: str) -> IntentClassification:
        command = parse_command(text)
        mapping = {
            "jobs": "jobs",
            "analyze": "analyze",
            "prepare": "prepare",
            "approvals": "approvals",
        }
        intent = mapping.get(command or "")
        if intent is None:
            raise ValueError("Intent requires clarification")
        return IntentClassification(intent=intent, confidence=1.0)


class IntentRouter:
    """Classify text, then build an AgentRequest for the existing supervisor."""

    def __init__(
        self,
        classifier: IntentModel,
        supervisor: Callable[[AgentRequest], object],
        reference_validator: Callable[[tuple[AgentReference, ...]], tuple[AgentReference, ...]]
        | None = None,
    ) -> None:
        self.classifier = classifier
        self.supervisor = supervisor
        self.reference_validator = reference_validator

    def classify(self, text: str) -> IntentClassification:
        try:
            result = self.classifier.classify(text)
            return IntentClassification.model_validate(result)
        except Exception:
            # The only fallback is the bounded deterministic parser; it never guesses.
            try:
                return DeterministicIntentModel().classify(text)
            except Exception:
                return IntentClassification(
                    intent="jobs",
                    confidence=0,
                    missing_information=("A supported intent is required.",),
                    clarification_required=True,
                )

    def request(
        self,
        text: str,
        *,
        user_id: UUID,
        conversation_id: UUID,
        task_id: UUID,
        allowed_capabilities: tuple[AgentCapability, ...],
    ) -> AgentRequest | IntentClassification:
        classification = self.classify(text)
        if classification.clarification_required or classification.missing_information:
            return classification
        references = classification.resource_references
        if references:
            if self.reference_validator is None:
                return IntentClassification(
                    intent=classification.intent,
                    confidence=classification.confidence,
                    missing_information=("The referenced resources must be validated first.",),
                    clarification_required=True,
                )
            try:
                references = self.reference_validator(references)
            except Exception:
                return IntentClassification(
                    intent=classification.intent,
                    confidence=classification.confidence,
                    missing_information=("The referenced resource is unavailable.",),
                    clarification_required=True,
                )
        capability = {
            "jobs": AgentCapability.JOB_DISCOVERY,
            "analyze": AgentCapability.JOB_ANALYSIS,
            "prepare": AgentCapability.CAREER_PREPARATION,
            "approvals": AgentCapability.APPROVAL_COORDINATION,
        }[classification.intent]
        return AgentRequest(
            user_id=user_id,
            conversation_id=conversation_id,
            task_id=task_id,
            requested_capability=capability,
            allowed_capabilities=allowed_capabilities,
            input_references=references,
        )

    def route(
        self,
        text: str,
        *,
        user_id: UUID,
        conversation_id: UUID,
        task_id: UUID,
        allowed_capabilities: tuple[AgentCapability, ...],
    ) -> object:
        """Classify then invoke the deterministic supervisor, or return clarification."""

        request = self.request(
            text,
            user_id=user_id,
            conversation_id=conversation_id,
            task_id=task_id,
            allowed_capabilities=allowed_capabilities,
        )
        if isinstance(request, AgentRequest):
            return self.supervisor(request)
        return request
