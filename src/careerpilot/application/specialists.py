"""Thin specialist facades over existing CareerPilot workflows.

Specialists are intentionally adapters, not new business-logic owners.  The
composition root supplies an owner-authorized workflow invoker; this module
only validates the shared contract, enforces the declared capability, and
converts the workflow outcome into the shared result contract.
"""

from collections.abc import Callable

from careerpilot.application.agent_contracts import (
    AgentCapability,
    AgentError,
    AgentErrorCode,
    AgentRequest,
    AgentResult,
    AgentResultStatus,
)
from careerpilot.application.failures import failure_report
from careerpilot.application.supervisor import CareerSupervisor

SpecialistInvoker = Callable[[AgentRequest], object]


class WorkflowSpecialist:
    """A stable, capability-scoped wrapper around one existing workflow."""

    identity: str
    capability: AgentCapability

    def __init__(
        self, *, identity: str, capability: AgentCapability, invoker: SpecialistInvoker
    ) -> None:
        self.identity = identity
        self.capability = capability
        self._invoker = invoker

    def run(self, request: AgentRequest) -> AgentResult:
        try:
            validated = AgentRequest.model_validate(request)
            if validated.requested_capability != self.capability:
                return CareerSupervisor.error_result(
                    AgentErrorCode.UNAUTHORIZED_CAPABILITY,
                    "The request capability does not match this specialist.",
                )
            if validated.budget and (
                validated.budget.exhausted("specialists")
                or validated.budget.exhausted("delegation_depth")
            ):
                return CareerSupervisor.error_result(
                    AgentErrorCode.BUDGET_EXHAUSTED,
                    "The execution budget does not allow this specialist.",
                )
            return self._convert(self._invoker(validated))
        except Exception as error:
            report = failure_report(error)
            code = {
                "invalid_input": AgentErrorCode.INVALID_REQUEST,
                "unauthorized": AgentErrorCode.UNAUTHORIZED_CAPABILITY,
                "not_found": AgentErrorCode.NOT_FOUND,
                "limit_exceeded": AgentErrorCode.BUDGET_EXHAUSTED,
                "timeout": AgentErrorCode.DEADLINE_EXCEEDED,
                "cancelled": AgentErrorCode.CANCELLED,
                "unknown_outcome": AgentErrorCode.UNKNOWN_OUTCOME,
                "unavailable": AgentErrorCode.RETRYABLE_FAILURE
                if report.retryable
                else AgentErrorCode.PERMANENT_FAILURE,
            }.get(report.code, AgentErrorCode.INTERNAL_FAILURE)
            return CareerSupervisor.error_result(
                code, report.message, retryable=report.retryable, uncertain=report.uncertain
            )

    @staticmethod
    def _convert(outcome: object) -> AgentResult:
        if getattr(outcome, "paused", False):
            return AgentResult(
                status=AgentResultStatus.WAITING_FOR_USER_ACTION,
                summary="The workflow is paused for explicit human approval.",
                required_user_action=(
                    "Review and explicitly approve, reject, or revise the pending action."
                ),
            )
        review = getattr(outcome, "review", None)
        if getattr(review, "status", None) == "unknown":
            return AgentResult(
                status=AgentResultStatus.PARTIAL,
                summary="The external action outcome is unknown and requires reconciliation.",
                errors=(
                    AgentError(
                        code=AgentErrorCode.UNKNOWN_OUTCOME,
                        message="The external system did not provide a confirmed outcome.",
                        uncertain=True,
                    ),
                ),
            )
        return CareerSupervisor.convert_outcome(outcome)


class DiscoverySpecialist(WorkflowSpecialist):
    def __init__(self, invoker: SpecialistInvoker) -> None:
        super().__init__(
            identity="discovery-specialist",
            capability=AgentCapability.JOB_DISCOVERY,
            invoker=invoker,
        )


class AnalysisSpecialist(WorkflowSpecialist):
    def __init__(self, invoker: SpecialistInvoker) -> None:
        super().__init__(
            identity="analysis-specialist", capability=AgentCapability.JOB_ANALYSIS, invoker=invoker
        )


class PreparationSpecialist(WorkflowSpecialist):
    def __init__(self, invoker: SpecialistInvoker) -> None:
        super().__init__(
            identity="preparation-specialist",
            capability=AgentCapability.CAREER_PREPARATION,
            invoker=invoker,
        )


class ApprovalSpecialist(WorkflowSpecialist):
    def __init__(self, invoker: SpecialistInvoker) -> None:
        super().__init__(
            identity="approval-specialist",
            capability=AgentCapability.APPROVAL_COORDINATION,
            invoker=invoker,
        )


class ContextSpecialist(WorkflowSpecialist):
    def __init__(self, invoker: SpecialistInvoker) -> None:
        super().__init__(
            identity="context-specialist",
            capability=AgentCapability.CAREER_CONTEXT,
            invoker=invoker,
        )
