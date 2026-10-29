"""Bounded discovery orchestration; authorization is bound outside state/LLM input."""

from collections.abc import Callable, Iterator
from typing import Literal, TypedDict

from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langsmith import tracing_context
from pydantic import TypeAdapter

from careerpilot.application.discovery import DiscoveryTools
from careerpilot.application.discovery_contracts import (
    DeduplicatedJobs,
    DiscoveryError,
    DiscoveryFailure,
    DiscoveryRequest,
    DiscoverySuccess,
    SearchBatch,
)
from careerpilot.application.execution import bounded_stream, bounded_workflow, counted_node


class DiscoveryInput(TypedDict):
    raw_request: object


class DiscoveryOutput(TypedDict):
    result: DiscoverySuccess | DiscoveryFailure


class DiscoveryState(TypedDict, total=False):
    raw_request: object
    request: DiscoveryRequest
    batch: SearchBatch | None
    jobs: DeduplicatedJobs | None
    failure: DiscoveryFailure | None
    result: DiscoverySuccess | DiscoveryFailure


def _request(state: DiscoveryState) -> DiscoveryRequest:
    return state["request"]


class DiscoveryNodes:
    def __init__(self, service: DiscoveryTools) -> None:
        self.service = service

    def validate(self, state: DiscoveryState) -> DiscoveryState:
        request = self.service.authorize_discovery(state.get("raw_request"))
        return {"raw_request": None, "request": request, "failure": None}

    def search(self, state: DiscoveryState) -> DiscoveryState:
        request = _request(state)
        return {
            "batch": self.service.search_candidates(
                {"source_ids": request.source_ids, "query": request.query, "limit": request.limit}
            )
        }

    def normalize(self, state: DiscoveryState) -> DiscoveryState:
        return {"batch": self.service.normalize_candidates(state["batch"])}

    def deduplicate(self, state: DiscoveryState) -> DiscoveryState:
        return {"jobs": self.service.deduplicate_candidates(state["batch"]), "batch": None}

    def assess(self, state: DiscoveryState) -> DiscoveryState:
        return {"result": self.service.assess(_request(state), state["jobs"]), "jobs": None}

    def fail(self, state: DiscoveryState) -> DiscoveryState:
        failure = state.get("failure")
        if failure is None:
            raise RuntimeError("Failure route requires a typed discovery error")
        return {"result": failure, "batch": None, "jobs": None, "raw_request": None}


def route(state: DiscoveryState) -> Literal["continue", "fail"]:
    return "fail" if state.get("failure") else "continue"


class DiscoveryWorkflow:
    def __init__(self, service: DiscoveryTools) -> None:
        nodes = DiscoveryNodes(service)

        def guarded(
            operation: Callable[[DiscoveryState], DiscoveryState],
        ) -> Callable[[DiscoveryState], DiscoveryState]:
            def execute(state: DiscoveryState) -> DiscoveryState:
                try:
                    return operation(state)
                except DiscoveryError as error:
                    return {
                        "failure": DiscoveryFailure(
                            code=error.code,
                            message=error.message,
                            retry_after_seconds=getattr(error, "retry_after_seconds", None),
                        )
                    }

            return execute

        builder = StateGraph(
            DiscoveryState, input_schema=DiscoveryInput, output_schema=DiscoveryOutput
        )
        for name, operation in (
            ("authorize", nodes.validate),
            ("search", nodes.search),
            ("normalize", nodes.normalize),
            ("deduplicate", nodes.deduplicate),
            ("assess", nodes.assess),
        ):
            builder.add_node(name, RunnableLambda(counted_node(guarded(operation))))
        builder.add_node("fail", counted_node(nodes.fail))
        builder.add_edge(START, "authorize")
        for source, target in (
            ("authorize", "search"),
            ("search", "normalize"),
            ("normalize", "deduplicate"),
            ("deduplicate", "assess"),
        ):
            builder.add_conditional_edges(source, route, {"continue": target, "fail": "fail"})
        builder.add_conditional_edges("assess", route, {"continue": END, "fail": "fail"})
        builder.add_edge("fail", END)
        self._graph: CompiledStateGraph[DiscoveryState, None, DiscoveryInput, DiscoveryOutput] = (
            builder.compile()
        )

    @bounded_workflow
    def run(self, raw_request: object) -> DiscoverySuccess | DiscoveryFailure:
        with tracing_context(enabled=False):
            output = self._graph.invoke({"raw_request": raw_request})
            return TypeAdapter(DiscoveryOutput).validate_python(output)["result"]

    @bounded_stream
    def updates(self, raw_request: object) -> Iterator[dict[str, DiscoveryState]]:
        with tracing_context(enabled=False):
            stream = self._graph.stream({"raw_request": raw_request}, stream_mode="updates")
        while True:
            with tracing_context(enabled=False):
                try:
                    update = next(stream)
                except StopIteration:
                    return
                patch = TypeAdapter(dict[str, DiscoveryState]).validate_python(update)
            yield patch

    def mermaid(self) -> str:
        return self._graph.get_graph().draw_mermaid()
