"""Deterministic capability routing over existing CareerPilot workflows.

Phase B deliberately keeps workflow construction in the composition root. The
supervisor owns routing and safe result conversion; adapters own the existing
workflow input construction and resource authorization.
"""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import get_ident
from uuid import uuid4

from careerpilot.application.agent_contracts import (
    AgentCapability,
    AgentError,
    AgentErrorCode,
    AgentReference,
    AgentRequest,
    AgentResult,
    AgentResultStatus,
    DelegationContextItem,
    DelegationEvent,
    DelegationEventStatus,
)
from careerpilot.application.execution import (
    RunControl,
    current_run,
    execution_scope,
    scoped_limits,
)
from careerpilot.application.failures import failure_report
from careerpilot.domain.reliability import ExecutionLimits

WorkflowInvoker = Callable[[AgentRequest], object]


@dataclass
class _DelegationState:
    owner: tuple[int, int | None]
    events: dict[object, DelegationEvent] = field(default_factory=dict)
    order: list[object] = field(default_factory=list)
    depth: int = 0


_DELEGATION_STATE: ContextVar[_DelegationState | None] = ContextVar(
    "careerpilot_delegation", default=None
)


def _execution_owner() -> tuple[int, int | None]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return get_ident(), id(task) if task is not None else None


ROUTED_CAPABILITIES = frozenset(
    {
        AgentCapability.JOB_DISCOVERY,
        AgentCapability.JOB_ANALYSIS,
        AgentCapability.CAREER_PREPARATION,
        AgentCapability.APPROVAL_COORDINATION,
    }
)


class CareerSupervisor:
    """Route one validated request to exactly one existing workflow adapter.

    This class does not authenticate users, construct workflow payloads, execute
    side effects, or delegate recursively. Those responsibilities remain with the
    existing Portal/application services and approval workflow.
    """

    def __init__(self, invokers: Mapping[AgentCapability, WorkflowInvoker]) -> None:
        self._invokers = dict(invokers)

    @property
    def delegation_events(self) -> tuple[DelegationEvent, ...]:
        """Events from the last delegation run, for logging or persistence by callers."""

        state = _DELEGATION_STATE.get()
        return tuple(state.events[event_id] for event_id in state.order) if state else ()

    def delegate(
        self, request: AgentRequest, capabilities: Sequence[AgentCapability]
    ) -> AgentResult:
        """Run registered specialists sequentially under one shared budget.

        This method only coordinates existing invokers. It never executes a side
        effect and does not claim durable exactly-once execution; callers may
        persist ``delegation_events`` using their existing transaction boundary.
        """

        state = _DELEGATION_STATE.get()
        owner = _execution_owner()
        outer = state is None or state.depth == 0 or state.owner != owner
        if outer:
            state = _DelegationState(owner=owner)
            _DELEGATION_STATE.set(state)
        assert state is not None
        state.depth += 1
        try:
            request = AgentRequest.model_validate(request)
            requested = tuple(capabilities)
            if not requested or len(set(requested)) != len(requested):
                return self.error_result(
                    AgentErrorCode.VALIDATION_FAILURE,
                    "Delegation must contain distinct specialist capabilities.",
                )
            if request.budget and request.budget.exhausted("delegation_depth"):
                return self.error_result(
                    AgentErrorCode.BUDGET_EXHAUSTED,
                    "The maximum delegation depth has been reached.",
                )
            if (
                request.budget
                and request.budget.max_specialists is not None
                and (request.budget.specialists + len(requested) > request.budget.max_specialists)
            ):
                return self.error_result(
                    AgentErrorCode.BUDGET_EXHAUSTED,
                    "The maximum specialist count would be exceeded.",
                )
            results: list[AgentResult] = []
            seen_tasks: set[object] = set()
            for capability in requested:
                if (
                    capability not in ROUTED_CAPABILITIES
                    or capability not in self._invokers
                    or capability not in request.allowed_capabilities
                ):
                    return self.error_result(
                        AgentErrorCode.UNAUTHORIZED_CAPABILITY,
                        "A requested specialist is not registered or allowed.",
                    )
                child_task_id = uuid4()
                if child_task_id in seen_tasks:
                    return self.error_result(
                        AgentErrorCode.VALIDATION_FAILURE, "A duplicate child task was detected."
                    )
                seen_tasks.add(child_task_id)
                child_budget = request.budget
                if child_budget is not None:
                    next_depth = child_budget.delegation_depth + 1
                    if child_budget.max_delegation_depth is not None and (
                        next_depth > child_budget.max_delegation_depth
                    ):
                        return self.error_result(
                            AgentErrorCode.BUDGET_EXHAUSTED,
                            "The maximum delegation depth would be exceeded.",
                        )
                    child_budget = child_budget.model_copy(
                        update={
                            "delegation_depth": child_budget.delegation_depth + 1,
                            "specialists": child_budget.specialists + len(results),
                        }
                    )
                child = request.model_copy(
                    update={
                        "task_id": child_task_id,
                        "parent_task_id": request.task_id,
                        "requested_capability": capability,
                        "allowed_capabilities": (capability,),
                        "budget": child_budget,
                        "delegation_context": request.delegation_context
                        + tuple(
                            DelegationContextItem(
                                specialist_id=previous_capability.value.replace("_", "-"),
                                status=previous.status.value,
                                summary=previous.summary,
                                references=previous.references,
                                provenance=previous.provenance,
                            )
                            for previous_capability, previous in zip(
                                requested[: len(results)], results, strict=True
                            )
                        ),
                    }
                )
                event = DelegationEvent(
                    event_id=uuid4(),
                    parent_task_id=request.task_id,
                    child_task_id=child_task_id,
                    parent_agent_id="career-supervisor",
                    child_agent_id=capability.value.replace("_", "-"),
                    capability=capability,
                    reason="Sequential capability delegation",
                    timestamp=datetime.now(UTC),
                    status=DelegationEventStatus.RUNNING,
                    input_references=child.input_references,
                )
                state.events[event.event_id] = event
                state.order.append(event.event_id)
                try:
                    result = self.run(child)
                except Exception as error:
                    report = failure_report(error)
                    self._update_event(
                        state, event.event_id, DelegationEventStatus.FAILED, error_report=report
                    )
                    return self.error_result(
                        AgentErrorCode.INTERNAL_FAILURE,
                        report.message,
                        retryable=report.retryable,
                        uncertain=report.uncertain,
                    )
                results.append(result)
                terminal_status = {
                    AgentResultStatus.COMPLETED: DelegationEventStatus.COMPLETED,
                    AgentResultStatus.PARTIAL: DelegationEventStatus.PARTIAL,
                    AgentResultStatus.WAITING_FOR_USER_ACTION: DelegationEventStatus.PARTIAL,
                    AgentResultStatus.FAILED: DelegationEventStatus.FAILED,
                }[result.status]
                self._update_event(
                    state,
                    event.event_id,
                    terminal_status,
                    output_references=result.references,
                    error=result.errors[0] if result.errors else None,
                )
                if result.status == AgentResultStatus.FAILED:
                    return self._aggregate(results)
            return self._aggregate(results)
        except Exception as error:
            report = failure_report(error)
            result = self.error_result(
                AgentErrorCode.CANCELLED
                if report.code == "cancelled"
                else AgentErrorCode.INTERNAL_FAILURE,
                report.message,
                retryable=report.retryable,
                uncertain=report.uncertain,
            )
            for event_id in state.order:
                if state.events[event_id].status == DelegationEventStatus.RUNNING:
                    self._update_event(
                        state, event_id, DelegationEventStatus.FAILED, error_report=report
                    )
            return result
        finally:
            state.depth -= 1

    def _update_event(
        self,
        state: _DelegationState,
        event_id: object,
        status: DelegationEventStatus,
        *,
        output_references: tuple[AgentReference, ...] = (),
        error: AgentError | None = None,
        error_report: object | None = None,
    ) -> None:
        event = state.events[event_id]
        if error is None and error_report is not None:
            error = AgentError(
                code=AgentErrorCode.INTERNAL_FAILURE,
                message="Delegated specialist execution failed.",
                retryable=bool(getattr(error_report, "retryable", False)),
                uncertain=bool(getattr(error_report, "uncertain", False)),
            )
        state.events[event_id] = event.model_copy(
            update={"status": status, "output_references": output_references, "error": error}
        )

    @staticmethod
    def _aggregate(results: Sequence[AgentResult]) -> AgentResult:
        references = tuple(ref for result in results for ref in result.references)
        provenance = tuple(ref for result in results for ref in result.provenance)
        errors = tuple(error for result in results for error in result.errors)
        kinds: dict[str, object] = {}
        conflicts: list[AgentError] = []
        for ref in references:
            previous = kinds.get(ref.kind)
            if previous is not None and previous != ref.id:
                conflicts.append(
                    AgentError(
                        code=AgentErrorCode.CONFLICTING_RESULTS,
                        message="Specialists returned conflicting resource references.",
                    )
                )
            kinds[ref.kind] = ref.id
        errors += tuple(conflicts)
        status = (
            AgentResultStatus.FAILED
            if any(result.status == AgentResultStatus.FAILED for result in results)
            else AgentResultStatus.PARTIAL
            if any(result.status != AgentResultStatus.COMPLETED for result in results) or conflicts
            else AgentResultStatus.COMPLETED
        )
        return AgentResult(
            status=status,
            summary="Delegated specialists completed sequentially."
            if not conflicts
            else "Delegated specialists returned conflicting results.",
            references=references,
            provenance=provenance,
            errors=errors,
        )

    def run(self, request: AgentRequest) -> AgentResult:
        try:
            request = AgentRequest.model_validate(request.model_dump())
            if request.requested_capability not in ROUTED_CAPABILITIES:
                return self.error_result(
                    AgentErrorCode.UNAUTHORIZED_CAPABILITY,
                    "This capability is not available through the deterministic supervisor.",
                )
            if request.requested_capability not in self._invokers:
                return self.error_result(
                    AgentErrorCode.INTERNAL_FAILURE,
                    "The requested workflow is not configured.",
                )
            if request.budget and any(
                request.budget.exhausted(resource)
                for resource in (
                    "specialists",
                    "delegation_depth",
                    "model_calls",
                    "tool_calls",
                    "steps",
                    "seconds",
                    "input_tokens",
                    "output_tokens",
                    "cost_usd",
                )
            ):
                return self.error_result(
                    AgentErrorCode.BUDGET_EXHAUSTED,
                    "The execution budget does not allow this workflow.",
                )
            outcome = self._invoke(request)
            return self.convert_outcome(outcome)
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
            return self.error_result(
                code, report.message, retryable=report.retryable, uncertain=report.uncertain
            )

    def _invoke(self, request: AgentRequest) -> object:
        """Run an adapter inside the existing cooperative execution budget."""

        if request.budget is None:
            return self._invokers[request.requested_capability](request)
        budget = request.budget
        limits = ExecutionLimits(
            max_steps=budget.max_steps if budget.max_steps is not None else 32,
            max_tool_calls=budget.max_tool_calls if budget.max_tool_calls is not None else 30,
            max_retrieved_jobs=30,
            max_model_calls=budget.max_model_calls if budget.max_model_calls is not None else 3,
            overall_seconds=budget.max_seconds if budget.max_seconds is not None else 120,
        )
        if current_run() is not None:
            with scoped_limits(limits):
                return self._invokers[request.requested_capability](request)
        control = RunControl(limits, owner_id=request.user_id, workflow_id=request.task_id)
        with execution_scope(control):
            return self._invokers[request.requested_capability](request)

    @staticmethod
    def error_result(
        code: AgentErrorCode,
        message: str,
        *,
        retryable: bool = False,
        uncertain: bool = False,
    ) -> AgentResult:
        return AgentResult(
            status=AgentResultStatus.FAILED,
            summary="The requested workflow could not be completed.",
            errors=(
                AgentError(code=code, message=message, retryable=retryable, uncertain=uncertain),
            ),
        )

    @staticmethod
    def convert_outcome(outcome: object) -> AgentResult:
        if isinstance(outcome, AgentResult):
            return outcome
        status = str(getattr(outcome, "status", "completed"))
        if status == "error":
            code = str(getattr(outcome, "code", "internal_failure"))
            error_code = (
                AgentErrorCode.NOT_FOUND
                if code == "not_found"
                else AgentErrorCode.VALIDATION_FAILURE
                if code in {"invalid_request", "validation_failed"}
                else AgentErrorCode.PERMANENT_FAILURE
            )
            return CareerSupervisor.error_result(
                error_code, str(getattr(outcome, "message", "Workflow failed."))
            )
        result_status = (
            AgentResultStatus.WAITING_FOR_USER_ACTION
            if status in {"needs_information", "pending"}
            else AgentResultStatus.PARTIAL
            if status == "partial"
            else AgentResultStatus.COMPLETED
        )
        references = CareerSupervisor.references(outcome)
        summary = {
            "completed": "The workflow completed successfully.",
            "empty": "The workflow completed with no matching items.",
            "needs_information": "More information is required to continue.",
            "pending": "The workflow is waiting for an explicit user action.",
        }.get(status, "The workflow completed.")
        return AgentResult(
            status=result_status,
            summary=summary,
            references=references,
            provenance=references,
            required_user_action="Provide the missing information or review the pending action."
            if result_status == AgentResultStatus.WAITING_FOR_USER_ACTION
            else None,
        )

    @staticmethod
    def references(outcome: object) -> tuple[AgentReference, ...]:
        values: list[AgentReference] = []
        for name, kind in (
            ("assessment_id", "assessment"),
            ("record", "preparation"),
            ("proposal_id", "proposal"),
        ):
            value = getattr(outcome, name, None)
            if value is not None and hasattr(value, "id"):
                value = value.id
            if value is not None:
                try:
                    values.append(AgentReference(kind=kind, id=value))
                except (TypeError, ValueError):
                    continue
        return tuple(values)
