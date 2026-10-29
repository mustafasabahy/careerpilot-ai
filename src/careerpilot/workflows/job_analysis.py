"""Small deterministic nodes with explicit validation and missing-data routes."""

from collections.abc import Iterator
from typing import Literal, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langsmith import tracing_context
from pydantic import TypeAdapter, ValidationError

from careerpilot.application.analysis import (
    AnalysisError,
    AnalysisFailure,
    AnalysisRequest,
    AnalysisResult,
    AnalysisService,
)
from careerpilot.application.execution import bounded_stream, bounded_workflow, counted_node
from careerpilot.application.storage import NotFound
from careerpilot.domain.baseline import BaselineAssessment, NormalizedCriterion


class AnalysisInput(TypedDict):
    raw_request: object


class AnalysisOutput(TypedDict):
    result: AnalysisResult


class AnalysisState(TypedDict, total=False):
    raw_request: object
    request: AnalysisRequest
    job_id: UUID
    candidate_id: UUID
    criteria: tuple[NormalizedCriterion, ...]
    baseline: BaselineAssessment | None
    assessment_id: UUID
    needs_information: bool
    failure: AnalysisFailure | None
    result: AnalysisResult


def _request(state: AnalysisState) -> AnalysisRequest:
    request = state.get("request")
    if request is None:
        raise AnalysisError("Request validation must precede this node")
    return request


def _baseline(state: AnalysisState) -> BaselineAssessment:
    report = state.get("baseline")
    if report is None:
        raise AnalysisError("Baseline calculation must precede this node")
    return report


class AnalysisNodes:
    def __init__(self, service: AnalysisService) -> None:
        self.service = service

    def validate_request(self, state: AnalysisState) -> AnalysisState:
        try:
            # Revalidate objects too: do not trust model_construct/model_copy input.
            raw = state.get("raw_request")
            if isinstance(raw, AnalysisRequest):
                raw = raw.model_dump(warnings=False)
            request = AnalysisRequest.model_validate(raw)
        except ValidationError:
            return {"raw_request": None, "failure": AnalysisFailure(code="invalid_request")}
        return {"raw_request": None, "request": request, "failure": None}

    def load_job(self, state: AnalysisState) -> AnalysisState:
        try:
            return {"job_id": self.service.load_job(_request(state).job_id)}
        except NotFound:
            return {"failure": AnalysisFailure(code="job_not_found")}

    def load_profile(self, state: AnalysisState) -> AnalysisState:
        try:
            return {"candidate_id": self.service.load_profile(_request(state).candidate_id)}
        except NotFound:
            return {"failure": AnalysisFailure(code="profile_not_found")}

    def normalize(self, state: AnalysisState) -> AnalysisState:
        return {"criteria": self.service.normalize(state["job_id"])}

    def calculate(self, state: AnalysisState) -> AnalysisState:
        return {"baseline": self.service.calculate(_request(state), state["criteria"])}

    def validate_assessment(self, state: AnalysisState) -> AnalysisState:
        report = _baseline(state)
        identifier = self.service.validate_and_store(_request(state), state["criteria"], report)
        return {"assessment_id": identifier, "needs_information": report.needs_information}

    def return_result(self, state: AnalysisState) -> AnalysisState:
        result = self.service.result(
            _request(state), state["criteria"], _baseline(state), state["assessment_id"]
        )
        return {"result": result, "baseline": None}

    def missing_information(self, state: AnalysisState) -> AnalysisState:
        # A valid partial analysis remains explicitly labeled needs_information.
        return self.return_result(state)

    def fail(self, state: AnalysisState) -> AnalysisState:
        failure = state.get("failure")
        if failure is None:
            raise AnalysisError("Failure route requires a typed failure")
        return {"result": failure}


def route_failure(state: AnalysisState) -> Literal["continue", "fail"]:
    return "fail" if state.get("failure") is not None else "continue"


def route_information(state: AnalysisState) -> Literal["complete", "missing_information"]:
    return "missing_information" if state["needs_information"] else "complete"


class JobAnalysisWorkflow:
    """Public local runner. Tracing is disabled even if host tracing env vars are set."""

    def __init__(self, service: AnalysisService) -> None:
        nodes = AnalysisNodes(service)
        builder = StateGraph(
            AnalysisState, input_schema=AnalysisInput, output_schema=AnalysisOutput
        )
        builder.add_node("validate_request", counted_node(nodes.validate_request))
        builder.add_node("load_job", counted_node(nodes.load_job))
        builder.add_node("load_profile", counted_node(nodes.load_profile))
        builder.add_node("normalize_requirements", counted_node(nodes.normalize))
        builder.add_node("calculate_baseline", counted_node(nodes.calculate))
        builder.add_node("validate_assessment", counted_node(nodes.validate_assessment))
        builder.add_node("return_result", counted_node(nodes.return_result))
        builder.add_node("missing_information", counted_node(nodes.missing_information))
        builder.add_node("fail", counted_node(nodes.fail))
        builder.add_edge(START, "validate_request")
        for node, destination in (
            ("validate_request", "load_job"),
            ("load_job", "load_profile"),
            ("load_profile", "normalize_requirements"),
        ):
            builder.add_conditional_edges(
                node, route_failure, {"continue": destination, "fail": "fail"}
            )
        builder.add_edge("normalize_requirements", "calculate_baseline")
        builder.add_edge("calculate_baseline", "validate_assessment")
        builder.add_conditional_edges(
            "validate_assessment",
            route_information,
            {
                "complete": "return_result",
                "missing_information": "missing_information",
            },
        )
        for node in ("return_result", "missing_information", "fail"):
            builder.add_edge(node, END)
        self._graph: CompiledStateGraph[AnalysisState, None, AnalysisInput, AnalysisOutput] = (
            builder.compile()
        )

    @bounded_workflow
    def run(self, raw_request: object) -> AnalysisResult:
        with tracing_context(enabled=False):
            output = self._graph.invoke({"raw_request": raw_request})
            return TypeAdapter(AnalysisOutput).validate_python(output)["result"]

    @bounded_stream
    def updates(self, raw_request: object) -> Iterator[dict[str, AnalysisState]]:
        """Stream per-node patches for local diagnosis; no document contents are in state."""
        with tracing_context(enabled=False):
            updates = self._graph.stream({"raw_request": raw_request}, stream_mode="updates")
        while True:
            # Scope each next() separately; do not leave caller tracing disabled at yield.
            with tracing_context(enabled=False):
                try:
                    update = next(updates)
                except StopIteration:
                    return
                patch = TypeAdapter(dict[str, AnalysisState]).validate_python(update)
            yield patch

    def mermaid(self) -> str:
        return self._graph.get_graph().draw_mermaid()
