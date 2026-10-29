"""Replay-safe interrupt review; approval authority stays in authenticated events."""

from typing import Literal, TypedDict
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot, interrupt
from langsmith import tracing_context

from careerpilot.application.action_contracts import ActionConflict, ActionDenied
from careerpilot.application.actions import ActionService
from careerpilot.application.execution import bounded_workflow, counted_node
from careerpilot.domain.actions import ActionReview, ReviewDecision
from careerpilot.domain.common import Model


class ApprovalState(TypedDict, total=False):
    workflow_version: str
    owner_id: str
    thread_id: str
    proposal_id: str
    content_hash: str
    decision_id: str
    decision: str
    finished: bool


class ApprovalThread(Model):
    review: ActionReview
    paused: bool
    next_nodes: tuple[str, ...]


class ActionApprovalWorkflow:
    def __init__(self, service: ActionService, checkpointer: BaseCheckpointSaver[str]) -> None:
        self.service = service

        def review(state: ApprovalState) -> ApprovalState:
            # Nothing before interrupt writes data or attempts an external action.
            event_id = interrupt(
                {
                    "action": "review_sensitive_action",
                    "proposal_id": state["proposal_id"],
                    "content_sha256": state["content_hash"],
                    "thread_id": state["thread_id"],
                }
            )
            event = service.verify_event(UUID(state["thread_id"]), UUID(str(event_id)))
            return {"decision_id": str(event.id), "decision": event.decision}

        def execute(state: ApprovalState) -> ApprovalState:
            event = service.verify_event(UUID(state["thread_id"]), UUID(state["decision_id"]))
            if event.decision != "approve":
                raise ActionDenied("Only approval events can enter execution")
            service.execute(UUID(state["thread_id"]))
            return {"finished": True}

        def finish(state: ApprovalState) -> ApprovalState:
            return {"finished": True}

        def route(state: ApprovalState) -> Literal["execute", "finish"]:
            return "execute" if state["decision"] == "approve" else "finish"

        builder = StateGraph(ApprovalState)
        builder.add_node("human_review", counted_node(review))
        builder.add_node("execute_action", counted_node(execute))
        builder.add_node("finish_without_execution", counted_node(finish))
        builder.add_edge(START, "human_review")
        builder.add_conditional_edges(
            "human_review",
            route,
            {
                "execute": "execute_action",
                "finish": "finish_without_execution",
            },
        )
        builder.add_edge("execute_action", END)
        builder.add_edge("finish_without_execution", END)
        self._graph: CompiledStateGraph[ApprovalState, None, ApprovalState, ApprovalState] = (
            builder.compile(checkpointer=checkpointer)
        )

    @staticmethod
    def _config(thread_id: UUID) -> RunnableConfig:
        if not isinstance(thread_id, UUID):
            raise ValueError("Approval thread identifiers must be UUIDs")
        # Prevent collisions with Phase 7 context threads using the same saver.
        return {"configurable": {"thread_id": f"approval:{thread_id}", "checkpoint_ns": ""}}

    def _checked(self, thread_id: UUID, snapshot: StateSnapshot) -> ActionReview:
        review = self.service.review(thread_id)
        values = snapshot.values
        if values and (
            values.get("workflow_version") != "action-approval-v1"
            or values.get("owner_id") != str(review.proposal.owner_id)
            or values.get("thread_id") != str(thread_id)
            or values.get("proposal_id") != str(review.proposal.id)
            or values.get("content_hash") != review.proposal.content_sha256
        ):
            raise ActionDenied("Checkpoint does not match this authorized immutable proposal")
        return review

    @bounded_workflow
    def prepare(self, raw: object) -> ApprovalThread:
        # Durable proposal precedes any checkpoint. Failed graph start can be recovered.
        review = self.service.propose(raw)
        return self.start(review.proposal.thread_id)

    @bounded_workflow
    def start(self, thread_id: UUID) -> ApprovalThread:
        with tracing_context(enabled=False):
            snapshot = self._graph.get_state(self._config(thread_id))
            review = self._checked(thread_id, snapshot)
            if snapshot.values:
                raise ActionConflict("Thread is already started; use recovery or resume")
            self._graph.invoke(
                {
                    "workflow_version": "action-approval-v1",
                    "owner_id": str(review.proposal.owner_id),
                    "thread_id": str(thread_id),
                    "proposal_id": str(review.proposal.id),
                    "content_hash": review.proposal.content_sha256,
                    "finished": False,
                },
                self._config(thread_id),
                durability="sync",
            )
            return self.current(thread_id)

    @bounded_workflow
    def current(self, thread_id: UUID) -> ApprovalThread:
        with tracing_context(enabled=False):
            snapshot = self._graph.get_state(self._config(thread_id))
            review = self._checked(thread_id, snapshot)
            return ApprovalThread(
                review=review,
                paused=any(task.interrupts for task in snapshot.tasks),
                next_nodes=snapshot.next,
            )

    @bounded_workflow
    def resume(self, raw: object) -> ApprovalThread:
        if isinstance(raw, ReviewDecision):
            raw = raw.model_dump()
        request = ReviewDecision.model_validate(raw)
        with tracing_context(enabled=False):
            snapshot = self._graph.get_state(self._config(request.thread_id))
            self._checked(request.thread_id, snapshot)
            if not any(task.interrupts for task in snapshot.tasks):
                raise ActionConflict("Thread is not waiting for a human decision")
            event = self.service.decide(request)
            self._graph.invoke(
                Command(resume=str(event.id)), self._config(request.thread_id), durability="sync"
            )
            return self.current(request.thread_id)

    @bounded_workflow
    def recover(self, thread_id: UUID) -> ApprovalThread:
        # Reuse a persisted authenticated event after a crash, never invent approval.
        with tracing_context(enabled=False):
            snapshot = self._graph.get_state(self._config(thread_id))
            review = self._checked(thread_id, snapshot)
            if not snapshot.values:
                self.start(thread_id)
                snapshot = self._graph.get_state(self._config(thread_id))
            if review.decision is not None and snapshot.next:
                command: Command[str] | None = (
                    Command(resume=str(review.decision.id))
                    if any(task.interrupts for task in snapshot.tasks)
                    else None
                )
                self._graph.invoke(command, self._config(thread_id), durability="sync")
            return self.current(thread_id)

    def mermaid(self) -> str:
        return self._graph.get_graph().draw_mermaid()
