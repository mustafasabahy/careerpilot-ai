"""Inspectable conditional paths for insufficient data, invalid plans and versioned drafts."""

from collections.abc import Callable, Iterator
from typing import Literal, TypedDict

from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langsmith import tracing_context
from pydantic import TypeAdapter, ValidationError

from careerpilot.application.execution import bounded_stream, bounded_workflow, counted_node
from careerpilot.application.preparation import PreparationContext, PreparationService
from careerpilot.application.preparation_contracts import (
    InvalidPreparationRequest,
    PreparationAccessDenied,
    PreparationConflict,
    PreparationFailure,
    PreparationGenerationError,
    PreparationNeedsInformation,
    PreparationSuccess,
    PreparationValidationError,
)
from careerpilot.application.storage import IntegrityViolation, NotFound
from careerpilot.domain.baseline import CriteriaError
from careerpilot.domain.preparation import DraftPlan

PreparationResult = PreparationSuccess | PreparationNeedsInformation | PreparationFailure


class PreparationInput(TypedDict):
    raw_request: object


class PreparationOutput(TypedDict):
    result: PreparationResult


class PreparationState(TypedDict, total=False):
    raw_request: object
    context: PreparationContext | None
    proposed: object
    plan: DraftPlan | None
    failure: PreparationFailure | None
    result: PreparationResult


class PreparationWorkflow:
    def __init__(self, service: PreparationService) -> None:
        def inspect(state: PreparationState) -> PreparationState:
            return {"context": service.inspect(state.get("raw_request")), "raw_request": None}

        def readiness(state: PreparationState) -> Literal["fail", "missing", "replay", "generate"]:
            if state.get("failure"):
                return "fail"
            context = state.get("context")
            if context is None:
                raise RuntimeError("Preparation context is required")
            if context.existing_record_id is not None:
                return "replay"
            return "missing" if context.intelligence.blocking_reasons else "generate"

        def context(state: PreparationState) -> PreparationContext:
            result = state.get("context")
            if result is None:
                raise RuntimeError("Preparation context is required")
            return result

        def missing(state: PreparationState) -> PreparationState:
            return {
                "result": PreparationNeedsInformation(intelligence=context(state).intelligence),
                "context": None,
            }

        def generate(state: PreparationState) -> PreparationState:
            return {"proposed": service.generate(context(state))}

        def validate(state: PreparationState) -> PreparationState:
            return {
                "plan": service.validate(context(state), state.get("proposed")),
                "proposed": None,
            }

        def persist(state: PreparationState) -> PreparationState:
            return {
                "result": service.persist(context(state), state.get("plan")),
                "context": None,
                "plan": None,
            }

        def replay(state: PreparationState) -> PreparationState:
            return {"result": service.replay(context(state).request.id), "context": None}

        def fail(state: PreparationState) -> PreparationState:
            failure = state.get("failure")
            if failure is None:
                raise RuntimeError("Failure route requires an error")
            return {
                "result": failure,
                "raw_request": None,
                "context": None,
                "proposed": None,
                "plan": None,
            }

        def guarded(
            operation: Callable[[PreparationState], PreparationState],
        ) -> RunnableLambda[PreparationState, PreparationState]:
            def execute(state: PreparationState) -> PreparationState:
                try:
                    return operation(state)
                except InvalidPreparationRequest:
                    failure = PreparationFailure(
                        code="invalid_request",
                        message="Preparation input does not match its schema",
                    )
                except PreparationAccessDenied:
                    failure = PreparationFailure(
                        code="access_denied",
                        message="Preparation or model export is not authorized",
                    )
                except PreparationConflict:
                    failure = PreparationFailure(
                        code="identity_conflict",
                        message="Preparation inputs changed or an ID was reused",
                    )
                except NotFound:
                    failure = PreparationFailure(
                        code="not_found",
                        message="A required profile, job, assessment or document is missing",
                    )
                except PreparationGenerationError:
                    failure = PreparationFailure(
                        code="generation_failed",
                        message="The configured planner failed; no drafts were accepted",
                    )
                except (
                    PreparationValidationError,
                    ValidationError,
                    IntegrityViolation,
                    CriteriaError,
                    ValueError,
                ):
                    failure = PreparationFailure(
                        code="validation_failed",
                        message="Input, evidence, assessment or draft validation failed",
                    )
                return {"failure": failure}

            return RunnableLambda(execute)

        def route(state: PreparationState) -> Literal["continue", "fail"]:
            return "fail" if state.get("failure") else "continue"

        builder = StateGraph(
            PreparationState, input_schema=PreparationInput, output_schema=PreparationOutput
        )
        for name, operation in (
            ("inspect_evidence", inspect),
            ("generate_plan", generate),
            ("validate_plan", validate),
            ("persist_drafts", persist),
            ("replay", replay),
        ):
            builder.add_node(name, guarded(counted_node(operation)))
        builder.add_node("clarify", counted_node(missing))
        builder.add_node("fail", counted_node(fail))
        builder.add_edge(START, "inspect_evidence")
        builder.add_conditional_edges(
            "inspect_evidence",
            readiness,
            {"fail": "fail", "missing": "clarify", "replay": "replay", "generate": "generate_plan"},
        )
        for source, target in (
            ("generate_plan", "validate_plan"),
            ("validate_plan", "persist_drafts"),
        ):
            builder.add_conditional_edges(source, route, {"continue": target, "fail": "fail"})
        builder.add_conditional_edges("persist_drafts", route, {"continue": END, "fail": "fail"})
        builder.add_conditional_edges("replay", route, {"continue": END, "fail": "fail"})
        for name in ("clarify", "fail"):
            builder.add_edge(name, END)
        self._graph: CompiledStateGraph[
            PreparationState, None, PreparationInput, PreparationOutput
        ] = builder.compile()

    @bounded_workflow
    def run(self, raw: object) -> PreparationResult:
        with tracing_context(enabled=False):
            return TypeAdapter(PreparationOutput).validate_python(
                self._graph.invoke({"raw_request": raw})
            )["result"]

    @bounded_stream
    def updates(self, raw: object) -> Iterator[dict[str, PreparationState]]:
        with tracing_context(enabled=False):
            stream = self._graph.stream({"raw_request": raw}, stream_mode="updates")
        while True:
            with tracing_context(enabled=False):
                try:
                    value = next(stream)
                except StopIteration:
                    return
                patch = TypeAdapter(dict[str, PreparationState]).validate_python(value)
            yield patch

    def mermaid(self) -> str:
        return self._graph.get_graph().draw_mermaid()
