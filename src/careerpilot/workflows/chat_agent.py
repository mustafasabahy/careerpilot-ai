"""Reference-only durable graph; authoritative progress stays in the chat journal."""

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Literal, TypedDict
from uuid import UUID

from langchain_core.runnables import RunnableConfig, RunnableLambda
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langsmith import tracing_context

from careerpilot.application.chat_agent import AgentProgress, ConversationalAgent
from careerpilot.application.chat_agent_contracts import AgentMetadata
from careerpilot.application.conversations import (
    AgentResult,
    ChatConflict,
    ConversationStore,
    Message,
    Operation,
)
from careerpilot.application.execution import counted_node
from careerpilot.application.security import SecurityDenied
from careerpilot.domain.common import utc_now


class ChatGraphState(TypedDict):
    version: str
    owner_id: str
    conversation_id: str
    operation_id: str
    stage: Literal["model", "tools", "final"]


class ChatAgentGraph:
    """One graph thread per operation; resume never imports caller-supplied state."""

    supports_recovery = True

    def __init__(
        self,
        agent: ConversationalAgent,
        store: ConversationStore,
        checkpointer: Callable[[], AbstractContextManager[BaseCheckpointSaver[str]]],
    ) -> None:
        self.agent, self.store, self.checkpointer = agent, store, checkpointer

    @staticmethod
    def config(operation: Operation) -> RunnableConfig:
        return {
            "configurable": {"thread_id": f"chat-agent:{operation.id}"},
            "recursion_limit": 32,
            "callbacks": [],
        }

    def load(self, owner: UUID, operation: Operation) -> Operation:
        with self.store.transaction() as repo:
            repo.get(owner, operation.conversation_id)
            saved = repo.operation(operation.conversation_id, operation.request.idempotency_key)
            if saved is None or saved.id != operation.id or saved.context.owner_id != owner:
                raise SecurityDenied("Agent operation unavailable")
            return saved

    def save(self, owner: UUID, operation: Operation, progress: AgentProgress) -> None:
        with self.store.transaction() as repo:
            repo.get(owner, operation.conversation_id)
            saved = repo.operation(operation.conversation_id, operation.request.idempotency_key)
            if (
                saved is None
                or saved.status != "running"
                or saved.claim_token != operation.claim_token
            ):
                raise ChatConflict("Agent operation no longer active")
            repo.put_operation(
                saved.model_copy(
                    update={
                        "agent_progress": progress.model_dump(mode="json"),
                        "agent_metadata": progress.metadata,
                    }
                )
            )

    def build(
        self, owner: UUID, operation: Operation, saver: BaseCheckpointSaver[str]
    ) -> CompiledStateGraph[ChatGraphState, None, ChatGraphState, ChatGraphState]:
        def node(
            stage: Literal["model", "tools", "final"],
        ) -> Callable[[ChatGraphState], ChatGraphState]:
            def advance(state: ChatGraphState) -> ChatGraphState:
                self.check(state, owner, operation)
                saved = self.load(owner, operation)
                progress = AgentProgress.model_validate(saved.agent_progress)
                # DB outcome commit may precede the graph checkpoint. Skip committed work.
                if progress.stage == stage and stage != "final":
                    self.agent.advance(
                        progress, owner, operation, lambda p: self.save(owner, operation, p)
                    )
                return {**state, "stage": progress.stage}

            advance.__name__ = "chat_" + stage
            return counted_node(advance)

        builder = StateGraph(ChatGraphState)
        for stage in ("model", "tools", "final"):
            builder.add_node(stage, RunnableLambda(node(stage)))
        builder.add_conditional_edges(
            START, lambda s: s["stage"], {"model": "model", "tools": "tools", "final": "final"}
        )
        for stage in ("model", "tools"):
            builder.add_conditional_edges(
                stage, lambda s: s["stage"], {"model": "model", "tools": "tools", "final": "final"}
            )
        builder.add_edge("final", END)
        return builder.compile(checkpointer=saver)

    @staticmethod
    def check(state: Mapping[str, object], owner: UUID, operation: Operation) -> None:
        if (
            state.get("version") != "chat-agent-v1"
            or state.get("owner_id") != str(owner)
            or state.get("conversation_id") != str(operation.conversation_id)
            or state.get("operation_id") != str(operation.id)
        ):
            raise SecurityDenied("Incompatible or unauthorized agent checkpoint")

    def run(
        self,
        owner: UUID,
        operation: Operation,
        history: tuple[Message, ...],
        journal: Callable[[AgentMetadata], None],
    ) -> AgentResult:
        saved = self.load(owner, operation)
        progress: AgentProgress | None = None
        try:
            resumed = bool(saved.agent_progress)
            progress = (
                AgentProgress.model_validate(saved.agent_progress)
                if resumed
                else self.agent.initialize(operation, history)
            )
            if (
                progress.context.owner_id != owner
                or progress.context.id != operation.conversation_id
            ):
                raise SecurityDenied("Agent context mismatch")
            if resumed:
                # New explicit recovery attempt: elapsed wall time excludes process downtime.
                # Model/tool counts and write claims are retained across attempts.
                progress.started_at = utc_now()
            with self.checkpointer() as saver, tracing_context(enabled=False):
                graph = self.build(owner, operation, saver)
                config = self.config(operation)
                snapshot = graph.get_state(config)
                if snapshot.values:
                    self.check(snapshot.values, owner, operation)
                elif resumed:
                    raise ValueError("Missing agent checkpoint")
                self.save(owner, operation, progress)
                if not snapshot.values or (not snapshot.next and progress.stage != "final"):
                    # A failed checkpoint commit can leave no scheduled task. Re-enter
                    # routing from the durable journal; completed effects are not replayed.
                    graph.invoke(
                        {
                            "version": "chat-agent-v1",
                            "owner_id": str(owner),
                            "conversation_id": str(operation.conversation_id),
                            "operation_id": str(operation.id),
                            "stage": progress.stage,
                        },
                        config,
                        durability="sync",
                    )
                elif snapshot.next:
                    graph.invoke(None, config, durability="sync")
                finished = AgentProgress.model_validate(self.load(owner, operation).agent_progress)
                if finished.stage != "final":
                    raise ValueError("Agent checkpoint did not terminate")
                return finished.result()
        except SecurityDenied:
            raise
        except Exception:
            # Do not expose checkpoint payloads/errors. Progress remains for explicit recovery.
            current = self.load(owner, operation)
            metadata = current.agent_metadata.model_copy(update={"error_code": "checkpoint"})
            return AgentResult(
                text=(
                    "Agent storage/checkpoint failure. Inspect saved status and use Refresh "
                    "to recover; uncertain tools are never replayed."
                ),
                context=progress.context if progress else operation.context,
                cards=progress.cards if progress else (),
                status="recoverable",
                metadata=metadata,
            )
