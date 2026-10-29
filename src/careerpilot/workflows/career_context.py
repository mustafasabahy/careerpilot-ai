"""Authorized reference-only context review with persistent interrupt/resume."""

from typing import Literal, TypedDict
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot, interrupt
from langsmith import tracing_context
from pydantic import Field

from careerpilot.application.context import (
    BoundedContext,
    ContextBuilder,
    ContextMessage,
    ProfileContextSelection,
)
from careerpilot.application.execution import bounded_workflow, counted_node
from careerpilot.application.memory import CareerMemory, MemoryConflict, MemoryDenied
from careerpilot.domain.common import Model
from careerpilot.domain.memory import MemoryKind


class ContextSelection(Model):
    kinds: tuple[MemoryKind, ...] = Field(
        default=(
            "job_preferences",
            "location_preferences",
            "career_fact",
            "career_goal",
            "correction",
        ),
        min_length=1,
        max_length=5,
    )
    limit: int = Field(default=10, ge=1, le=20, strict=True)


class ContextState(TypedDict, total=False):
    workflow_version: str
    owner_id: str
    thread_id: str
    candidate_id: str | None
    revision: int
    memory_ids: list[str]
    accepted: bool
    finished: bool


class ThreadState(Model):
    thread_id: UUID
    candidate_id: UUID | None
    revision: int = Field(ge=0)
    memory_ids: tuple[UUID, ...] = Field(max_length=20)
    status: Literal["paused", "completed", "cancelled", "pending"]
    checkpoint_id: str
    next_nodes: tuple[str, ...]


class CareerContextWorkflow:
    def __init__(
        self,
        memory: CareerMemory,
        checkpointer: BaseCheckpointSaver[str],
        context_builder: ContextBuilder | None = None,
    ) -> None:
        self.memory = memory
        self.builder = context_builder or ContextBuilder(memory.principal)
        if self.builder.principal != memory.principal:
            raise MemoryDenied("Context builder and memory principal must agree")

        def review(state: ContextState) -> ContextState:
            accepted = interrupt(
                {
                    "action": "review_context_references",
                    "memory_ids": state["memory_ids"],
                    "revision": state["revision"],
                }
            )
            if type(accepted) is not bool:
                raise ValueError("Resume requires an explicit boolean review decision")
            return {"accepted": accepted}

        def finish(state: ContextState) -> ContextState:
            return {"finished": True}

        graph = StateGraph(ContextState)
        graph.add_node("review_context", counted_node(review))
        graph.add_node("finish", counted_node(finish))
        graph.add_edge(START, "review_context")
        graph.add_edge("review_context", "finish")
        graph.add_edge("finish", END)
        self._graph: CompiledStateGraph[ContextState, None, ContextState, ContextState] = (
            graph.compile(checkpointer=checkpointer)
        )
        self._checkpointer = checkpointer

    def _config(self, thread_id: UUID) -> RunnableConfig:
        # User input cannot select a checkpoint namespace, owner or time-travel ID.
        return {"configurable": {"thread_id": str(thread_id), "checkpoint_ns": ""}}

    def _view(self, snapshot: StateSnapshot, thread_id: UUID) -> ThreadState:
        values = snapshot.values
        if not values or values.get("owner_id") != str(self.memory.principal.owner_id):
            raise MemoryDenied("Checkpoint owner does not match authenticated principal")
        if values.get("thread_id") != str(thread_id):
            raise MemoryDenied("Checkpoint thread does not match registry")
        if values.get("workflow_version") != "career-context-v1":
            raise MemoryConflict("Checkpoint workflow version is unsupported")
        candidate = values.get("candidate_id")
        if candidate and UUID(candidate) not in self.memory.principal.candidate_ids:
            raise MemoryDenied("Candidate authorization has changed")
        status: Literal["paused", "completed", "cancelled", "pending"] = "pending"
        if values.get("finished"):
            status = "completed" if values.get("accepted") else "cancelled"
        elif any(task.interrupts for task in snapshot.tasks):
            status = "paused"
        return ThreadState(
            thread_id=thread_id,
            candidate_id=candidate,
            revision=values["revision"],
            memory_ids=values["memory_ids"],
            status=status,
            checkpoint_id=snapshot.config["configurable"].get("checkpoint_id", ""),
            next_nodes=snapshot.next,
        )

    @bounded_workflow
    def start(
        self,
        thread_id: UUID,
        selection: ContextSelection | None = None,
        *,
        candidate_id: UUID | None = None,
    ) -> ThreadState:
        if not isinstance(thread_id, UUID):
            raise ValueError("Thread identifiers must be UUIDs")
        selection = ContextSelection.model_validate((selection or ContextSelection()).model_dump())
        if candidate_id is not None and candidate_id not in self.memory.principal.candidate_ids:
            raise MemoryDenied("Candidate context is not authorized")
        owner = self.memory.principal.owner_id
        # Commit ownership before checkpoint writes: failure must never allow an
        # orphan checkpoint to be adopted by another user under the same ID.
        with self.memory.transaction() as repository:
            repository.register_thread(owner, thread_id, candidate_id)
        with self.memory.transaction() as repository, tracing_context(enabled=False):
            repository.require_thread(owner, thread_id)
            selected = self.memory.selected(repository, selection.kinds, selection.limit)
            self._graph.invoke(
                {
                    "workflow_version": "career-context-v1",
                    "owner_id": str(owner),
                    "thread_id": str(thread_id),
                    "candidate_id": str(candidate_id) if candidate_id else None,
                    "revision": repository.revision(owner),
                    "memory_ids": [str(item.id) for item in selected],
                    "accepted": False,
                    "finished": False,
                },
                self._config(thread_id),
                durability="sync",
            )
            return self._view(self._graph.get_state(self._config(thread_id)), thread_id)

    @bounded_workflow
    def current(self, thread_id: UUID) -> ThreadState:
        with self.memory.transaction() as repository, tracing_context(enabled=False):
            repository.require_thread(self.memory.principal.owner_id, thread_id)
            return self._view(self._graph.get_state(self._config(thread_id)), thread_id)

    @bounded_workflow
    def history(self, thread_id: UUID, *, limit: int = 20) -> tuple[ThreadState, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("History limit must be between 1 and 100")
        with self.memory.transaction() as repository, tracing_context(enabled=False):
            repository.require_thread(self.memory.principal.owner_id, thread_id)
            return tuple(
                self._view(snapshot, thread_id)
                for snapshot in self._graph.get_state_history(self._config(thread_id), limit=limit)
                if snapshot.values.get("owner_id") is not None
            )

    @bounded_workflow
    def resume(self, thread_id: UUID, *, accepted: bool) -> ThreadState:
        if type(accepted) is not bool:
            raise ValueError("Review decision must be boolean")
        owner = self.memory.principal.owner_id
        with self.memory.transaction() as repository, tracing_context(enabled=False):
            repository.require_thread(owner, thread_id)
            snapshot = self._graph.get_state(self._config(thread_id))
            state = self._view(snapshot, thread_id)
            if repository.revision(owner) != state.revision:
                raise MemoryConflict("Memory changed; start a new context review")
            if state.status not in {"paused", "pending"}:
                raise MemoryConflict("Thread has already finished")
            # pending supports recovery from a failure before the interrupt checkpoint.
            command: Command[str] | None = (
                Command(resume=accepted) if state.status == "paused" else None
            )
            self._graph.invoke(command, self._config(thread_id), durability="sync")
            return self._view(self._graph.get_state(self._config(thread_id)), thread_id)

    @bounded_workflow
    def context(
        self,
        thread_id: UUID,
        messages: tuple[ContextMessage, ...] = (),
        *,
        profile_selection: ProfileContextSelection | None = None,
    ) -> BoundedContext:
        owner = self.memory.principal.owner_id
        with self.memory.transaction() as repository, tracing_context(enabled=False):
            candidate_id = repository.require_thread(owner, thread_id)
            state = self._view(self._graph.get_state(self._config(thread_id)), thread_id)
            if state.status == "cancelled":
                raise MemoryDenied("User declined this context")
            if repository.revision(owner) != state.revision:
                raise MemoryConflict("Memory changed; context references are stale")
            items = {item.id: item for item in repository.items(owner)}
            if any(identifier not in items for identifier in state.memory_ids):
                raise MemoryConflict("Selected memory has been deleted")
            return self.builder.build(
                tuple(items[identifier] for identifier in state.memory_ids),
                candidate_id,
                messages,
                profile_selection,
            )

    @bounded_workflow
    def delete(self, thread_id: UUID) -> None:
        with self.memory.transaction() as repository:
            repository.mark_thread_deleted(self.memory.principal.owner_id, thread_id)
        # Authorization tombstone is committed first. Physical purge is retryable.
        with self.memory.transaction(), tracing_context(enabled=False):
            self._checkpointer.delete_thread(str(thread_id))

    @bounded_workflow
    def forget_all(self) -> None:
        owner = self.memory.principal.owner_id
        with self.memory.transaction() as repository:
            threads = repository.threads(owner)
            for identifier in threads:
                repository.mark_thread_deleted(owner, identifier)
            repository.clear(owner)
        for identifier in threads:
            with self.memory.transaction(), tracing_context(enabled=False):
                self._checkpointer.delete_thread(str(identifier))
