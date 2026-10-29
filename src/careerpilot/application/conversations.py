"""Bounded bilingual conversation orchestration; no model or approval authority."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import timedelta
from typing import Literal, Protocol, Self
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from careerpilot.application.analysis import AnalysisResult
from careerpilot.application.chat_agent_contracts import AgentMetadata
from careerpilot.application.preparation_contracts import (
    PreparationFailure,
    PreparationNeedsInformation,
    PreparationSuccess,
)
from careerpilot.application.security import SecurityDenied
from careerpilot.domain.actions import ActionReview
from careerpilot.domain.applications import JobApplication
from careerpilot.domain.common import Entity, Model, UtcTimestamp, utc_now
from careerpilot.domain.jobs import JobPosting
from careerpilot.domain.preparation import Clarification
from careerpilot.domain.profile import CandidateProfile

Command = Literal["help", "jobs", "analyze", "missing", "prepare", "applications", "approvals"]
Resource = Literal["job", "candidate"]


class Selection(Model):
    kind: Resource
    id: UUID


class MessageInput(Model):
    text: str = Field(min_length=1, max_length=4000)
    idempotency_key: UUID
    selection: Selection | None = None
    offset: int = Field(default=0, ge=0, le=100000)


class Reference(Model):
    kind: Literal[
        "job",
        "candidate",
        "assessment",
        "preparation",
        "document",
        "version",
        "application",
        "thread",
    ]
    id: UUID


class Choice(Model):
    selection: Selection
    label: str


class AnalysisPayload(Model):
    analysis: AnalysisResult
    job: JobPosting
    profile: CandidateProfile
    clarifications: tuple[Clarification, ...] = ()


class PagePayload(Model):
    offset: int = Field(default=0, ge=0)
    next_offset: int | None = Field(default=None, ge=0)


class ApprovalItem(Model):
    review: ActionReview
    paused: bool
    next_nodes: tuple[str, ...]


class ApprovalPayload(PagePayload):
    items: tuple[ApprovalItem, ...] = ()


class ApplicationPayload(PagePayload):
    items: tuple[JobApplication, ...] = ()


class Card(Model):
    kind: Literal[
        "help",
        "jobs",
        "selection",
        "analysis",
        "missing",
        "preparation",
        "applications",
        "approvals",
        "error",
        "operation",
        "details",
    ]
    text: str
    choices: tuple[Choice, ...] = ()
    references: tuple[Reference, ...] = ()
    payload: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        # The card discriminator selects its existing typed business result contract.
        if self.kind in {"analysis", "missing"}:
            AnalysisPayload.model_validate(self.payload)
        elif self.kind == "preparation":
            if self.payload.get("status") == "drafts_created":
                PreparationSuccess.model_validate(self.payload)
            elif self.payload.get("status") == "needs_information":
                PreparationNeedsInformation.model_validate(self.payload)
            else:
                PreparationFailure.model_validate(self.payload)
        elif self.kind == "approvals":
            ApprovalPayload.model_validate(self.payload)
        elif self.kind == "applications":
            ApplicationPayload.model_validate(self.payload)
        elif self.kind in {"jobs", "selection"}:
            PagePayload.model_validate(self.payload)
        elif self.payload:
            raise ValueError("This card kind cannot carry arbitrary payloads")
        return self


class Conversation(Entity):
    owner_id: UUID
    updated_at: UtcTimestamp = Field(default_factory=utc_now)
    job_id: UUID | None = None
    candidate_id: UUID | None = None
    assessment_id: UUID | None = None
    pending: Literal["analyze", "prepare", "missing"] | None = None
    awaiting: Resource | None = None
    language: Literal["en", "ar"] = "en"


class Message(Entity):
    conversation_id: UUID
    sequence: int = Field(ge=1)
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)
    card: Card | None = None
    cards: tuple[Card, ...] = ()
    mode: Literal["demo", "llm"] = "demo"


class Operation(Entity):
    conversation_id: UUID
    request: MessageInput
    command: Command | None = None
    status: Literal["running", "completed", "failed", "unknown", "recoverable"] = "running"
    response: Message | None = None
    context: Conversation
    task_status: Literal[
        "in_progress",
        "awaiting_clarification",
        "awaiting_approval",
        "completed",
        "failed",
        "unknown",
        "recoverable",
    ] = "in_progress"
    mode: Literal["demo", "llm"] = "demo"
    claim_expires_at: UtcTimestamp | None = None
    agent_progress: dict[str, object] = Field(default_factory=dict, exclude=True)
    claim_token: UUID | None = None
    agent_metadata: AgentMetadata = Field(default_factory=AgentMetadata)


class AgentResult(Model):
    text: str = Field(max_length=8000)
    cards: tuple[Card, ...] = ()
    context: Conversation
    status: Literal["completed", "failed", "unknown", "recoverable"] = "completed"
    metadata: AgentMetadata


class AgentRunner(Protocol):
    def run(
        self,
        owner: UUID,
        operation: Operation,
        history: tuple[Message, ...],
        journal: Callable[[AgentMetadata], None],
    ) -> AgentResult: ...


class Transcript(Model):
    conversation: Conversation
    messages: tuple[Message, ...]
    next_offset: int | None
    operations: tuple[Operation, ...] = ()


class ChatConflict(ValueError):
    pass


class ConversationRepository(Protocol):
    def create(self, value: Conversation) -> None: ...
    def get(self, owner: UUID, identifier: UUID) -> Conversation: ...
    def save(self, value: Conversation) -> None: ...
    def list(self, owner: UUID, offset: int, limit: int) -> tuple[Conversation, ...]: ...
    def messages(self, identifier: UUID, offset: int, limit: int) -> tuple[Message, ...]: ...
    def append(
        self,
        identifier: UUID,
        role: Literal["user", "assistant"],
        content: str,
        card: Card | None = None,
        *,
        cards: tuple[Card, ...] = (),
        mode: Literal["demo", "llm"] = "demo",
    ) -> Message: ...
    def operation(self, identifier: UUID, key: UUID) -> Operation | None: ...
    def operations(self, identifier: UUID) -> tuple[Operation, ...]: ...
    def put_operation(self, value: Operation) -> None: ...
    def delete(self, owner: UUID, identifier: UUID) -> None: ...


class ConversationStore(Protocol):
    def transaction(self) -> AbstractContextManager[ConversationRepository]: ...


class ChatBackend(Protocol):
    def choices(
        self, owner: UUID, kind: Resource, offset: int, limit: int
    ) -> tuple[Choice, ...]: ...
    def require(self, owner: UUID, kind: Resource, identifier: UUID) -> None: ...
    def execute(
        self, owner: UUID, command: Command, context: Conversation, operation: Operation
    ) -> tuple[Card, UUID | None]: ...
    def refresh(self, owner: UUID, card: Card) -> Card: ...


COMMANDS: dict[str, Command] = {
    "help": "help",
    "show my jobs": "jobs",
    "jobs": "jobs",
    "select job": "analyze",
    "analyze job": "analyze",
    "analyze": "analyze",
    "what information is missing": "missing",
    "missing information": "missing",
    "prepare my application": "prepare",
    "prepare": "prepare",
    "show my applications": "applications",
    "show my pending approvals": "approvals",
}


def parse_command(text: str) -> Command | None:
    return COMMANDS.get(text.strip().casefold().rstrip(".?!").strip())


def localized(context: Conversation, en: str, ar: str) -> str:
    """Return the English product message; the second argument is retained for API compatibility."""
    return en


class ConversationService:
    def __init__(
        self, store: ConversationStore, backend: ChatBackend, agent: AgentRunner | None = None
    ) -> None:
        self.store, self.backend, self.agent = store, backend, agent

    def create(self, owner: UUID) -> Conversation:
        value = Conversation(owner_id=owner)
        with self.store.transaction() as repository:
            if len(repository.list(owner, 0, 101)) >= 100:
                raise ChatConflict("Conversation limit reached")
            repository.create(value)
        return value

    def list(self, owner: UUID, offset: int = 0, limit: int = 20) -> tuple[Conversation, ...]:
        self._page(offset, limit)
        with self.store.transaction() as repository:
            return repository.list(owner, offset, limit)

    @staticmethod
    def _page(offset: int, limit: int) -> None:
        if offset < 0 or not 1 <= limit <= 50:
            raise ValueError("Invalid conversation page")

    def read(self, owner: UUID, identifier: UUID, offset: int = 0, limit: int = 30) -> Transcript:
        self._page(offset, limit)
        with self.store.transaction() as repository:
            conversation = repository.get(owner, identifier)
            raw_messages = repository.messages(identifier, offset, limit)
            messages = tuple(self._refresh_message(owner, message) for message in raw_messages)
            visible = {m.id for m in messages}
            operations = tuple(
                op
                for op in repository.operations(identifier)
                if op.response is None or op.response.id in visible
            )
            refreshed = {m.id: m for m in messages}
            operations = tuple(
                op.model_copy(
                    update={
                        "response": refreshed.get(op.response.id),
                        "task_status": task_status(op.status, refreshed[op.response.id]),
                    }
                )
                if op.response
                else op
                for op in operations
            )
            return Transcript(
                conversation=conversation,
                messages=messages,
                next_offset=offset + limit if len(messages) == limit else None,
                operations=operations,
            )

    def _refresh_message(self, owner: UUID, message: Message) -> Message:
        def refresh(card: Card) -> Card:
            try:
                return self.backend.refresh(owner, card)
            except Exception:
                return Card(kind="error", text="Referenced resources are no longer available.")

        return message.model_copy(
            update={
                "card": refresh(message.card) if message.card else None,
                "cards": tuple(refresh(c) for c in message.cards),
            }
        )

    def delete(self, owner: UUID, identifier: UUID) -> None:
        with self.store.transaction() as repository:
            repository.delete(owner, identifier)

    def submit(self, owner: UUID, identifier: UUID, request: MessageInput) -> Operation:
        request = MessageInput.model_validate(request.model_dump())
        with self.store.transaction() as repository:
            context = repository.get(owner, identifier)
            previous = repository.operation(identifier, request.idempotency_key)
            if previous:
                if previous.request != request:
                    raise ChatConflict("Idempotency key already identifies another message")
                if previous.response:
                    return self._refresh_operation(owner, previous)
                return previous
            if any(
                op.status in {"running", "recoverable"} for op in repository.operations(identifier)
            ):
                raise ChatConflict(
                    "Resolve the running operation before submitting another message"
                )
            context = context.model_copy(update={"language": "en"})
            command = parse_command(request.text)
            if request.text.strip().casefold().rstrip(".?!") in {"select job"}:
                context = context.model_copy(
                    update={"job_id": None, "candidate_id": None, "assessment_id": None}
                )
            if request.selection:
                if context.pending is None or context.awaiting != request.selection.kind:
                    raise ChatConflict("No matching resource clarification is pending")
                self.backend.require(owner, request.selection.kind, request.selection.id)
                context = context.model_copy(
                    update={
                        "job_id"
                        if request.selection.kind == "job"
                        else "candidate_id": request.selection.id,
                        "assessment_id": None,
                    }
                )
                command = context.pending
            if len(repository.messages(identifier, 0, 1001)) >= 1000:
                raise ChatConflict("Conversation limit reached; start another conversation")
            operation = Operation(
                conversation_id=identifier,
                request=request,
                command=command,
                context=context,
                mode="llm" if self.agent else "demo",
            )
            repository.append(identifier, "user", request.text)
            repository.put_operation(operation)
            repository.save(context.model_copy(update={"updated_at": utc_now()}))
        return self._complete(owner, operation)

    def _complete(self, owner: UUID, operation: Operation) -> Operation:
        if operation.mode == "llm":
            return self._complete_agent(owner, operation)
        # Serialize within the transcript DB, never hold a business DB transaction here.
        # The durable reservation precedes execution. Stable run IDs permit safe replay
        # of existing idempotent analysis/preparation workflows after a process crash.
        with self.store.transaction() as repository:
            repository.get(owner, operation.conversation_id)
            current = repository.operation(
                operation.conversation_id, operation.request.idempotency_key
            )
            if current is None:
                raise ChatConflict("Operation no longer available")
            if current.status != "running":
                return current
            context, command = current.context, current.command
            card: Card
            state: Literal["completed", "failed"] = "completed"
            try:
                card, context = run_command(self.backend, owner, command, context, current)
            except Exception:
                # Never persist provider exceptions, credentials or input echoes.
                state = "failed"
                card = Card(
                    kind="error",
                    text=localized(
                        context,
                        "Operation failed. No approval was granted. "
                        "Check resource access and prerequisites.",
                        "",
                    ),
                )
            if len(card.model_dump_json().encode("utf-8")) > 128 * 1024:
                state = "failed"
                card = Card(
                    kind="error", text="Result exceeds the chat display limit. Open details."
                )
            response = repository.append(context.id, "assistant", card.text, card)
            result = current.model_copy(
                update={
                    "status": state,
                    "response": response,
                    "task_status": task_status(state, response),
                }
            )
            repository.save(context.model_copy(update={"updated_at": utc_now()}))
            repository.put_operation(result)
            return result

    def refresh(self, owner: UUID, identifier: UUID, operation_id: UUID) -> Operation:
        with self.store.transaction() as repository:
            repository.get(owner, identifier)
            operation = next(
                (op for op in repository.operations(identifier) if op.id == operation_id), None
            )
            if operation is None:
                raise SecurityDenied("Operation unavailable")
        if operation.status in {"running", "recoverable"}:
            # Only read operations and idempotent deterministic workflows are dispatched.
            return self._complete(owner, operation)
        if operation.response:
            return self._refresh_operation(owner, operation)
        return operation

    def _refresh_operation(self, owner: UUID, operation: Operation) -> Operation:
        if operation.response is None:
            return operation
        response = self._refresh_message(owner, operation.response)
        return operation.model_copy(
            update={"response": response, "task_status": task_status(operation.status, response)}
        )

    def _complete_agent(self, owner: UUID, operation: Operation) -> Operation:
        with self.store.transaction() as repository:
            repository.get(owner, operation.conversation_id)
            current = repository.operation(
                operation.conversation_id, operation.request.idempotency_key
            )
            if current is None:
                raise ChatConflict("Operation unavailable")
            if current.status not in {"running", "recoverable"}:
                return current
            if current.claim_expires_at:
                if current.claim_expires_at > utc_now() and current.status != "recoverable":
                    return current
                if not (getattr(self.agent, "supports_recovery", False) and current.agent_progress):
                    response = repository.append(
                        current.conversation_id,
                        "assistant",
                        (
                            "Interrupted model operation: outcome unknown. Inspect saved "
                            "workflow status before retrying; no tool was replayed."
                        ),
                        mode="llm",
                    )
                    result = current.model_copy(
                        update={"status": "unknown", "response": response, "task_status": "unknown"}
                    )
                    repository.put_operation(result)
                    return result
            current = current.model_copy(
                update={
                    "claim_expires_at": utc_now() + timedelta(seconds=130),
                    "claim_token": uuid4(),
                    "status": "running",
                }
            )
            repository.put_operation(current)
            # Bounded transcript; the agent trims it further. No raw checkpoint context.
            history = repository.messages(current.conversation_id, 0, 1000)[-20:]

        def journal(metadata: AgentMetadata) -> None:
            with self.store.transaction() as repository:
                repository.get(owner, current.conversation_id)
                saved = repository.operation(
                    current.conversation_id, current.request.idempotency_key
                )
                if (
                    saved is None
                    or saved.status != "running"
                    or saved.claim_token != current.claim_token
                ):
                    raise ChatConflict("Operation no longer active")
                repository.put_operation(saved.model_copy(update={"agent_metadata": metadata}))

        if self.agent is None:
            answer = AgentResult(
                text="LLM operation cannot resume in demo mode. Restore LLM configuration.",
                context=current.context,
                status="failed",
                metadata=AgentMetadata(error_code="configuration"),
            )
        else:
            answer = self.agent.run(owner, current, history, journal)
        with self.store.transaction() as repository:
            repository.get(owner, current.conversation_id)
            saved = repository.operation(current.conversation_id, current.request.idempotency_key)
            if (
                saved is None
                or saved.status != "running"
                or saved.claim_token != current.claim_token
            ):
                raise ChatConflict("Operation no longer active")
            response = repository.append(
                current.conversation_id, "assistant", answer.text, cards=answer.cards, mode="llm"
            )
            result = saved.model_copy(
                update={
                    "status": answer.status,
                    "task_status": task_status(answer.status, response),
                    "response": response,
                    "agent_metadata": answer.metadata,
                }
            )
            repository.save(answer.context.model_copy(update={"updated_at": utc_now()}))
            repository.put_operation(result)
            return result


def demo_help(context: Conversation) -> Card:
    return Card(
        kind="help",
        text=localized(
            context,
            "Demo assistant: use the suggested commands. Select resources with buttons. "
            'Free-form requests and "yes" do not execute actions or grant approval.',
            "The demo assistant supports bounded commands. Select resources with buttons. "
            'Free-form requests and "yes" do not execute actions or grant approval.',
        ),
    )


def run_command(
    backend: ChatBackend,
    owner: UUID,
    command: Command | None,
    context: Conversation,
    operation: Operation,
) -> tuple[Card, Conversation]:
    if command in {"analyze", "prepare", "missing"}:
        resources: tuple[tuple[Resource, UUID | None], ...] = (
            ("job", context.job_id),
            ("candidate", context.candidate_id),
        )
        for selected_kind, selected in resources:
            if selected:
                backend.require(owner, selected_kind, selected)
        if context.job_id is None or context.candidate_id is None:
            kind: Resource = "job" if context.job_id is None else "candidate"
            choices = backend.choices(owner, kind, operation.request.offset, 20)
            context = context.model_copy(update={"pending": command, "awaiting": kind})
            card = Card(
                kind="selection",
                text=localized(
                    context,
                    f"Select a {kind} to continue. If none exist, open workspace details.",
                    "Select the job or profile to continue. If the list is empty, "
                    "open workspace details.",
                ),
                choices=choices,
                payload={
                    "next_offset": operation.request.offset + 20 if len(choices) == 20 else None
                },
            )
        else:
            card, assessment = backend.execute(owner, command, context, operation)
            context = context.model_copy(
                update={
                    "pending": None,
                    "awaiting": None,
                    "assessment_id": assessment or context.assessment_id,
                }
            )
    elif command:
        card, _ = backend.execute(owner, command, context, operation)
        if command == "help":
            card = demo_help(context)
    else:
        card = demo_help(context)
    return card, context


def task_status(status: str, response: Message) -> str:
    if status != "completed":
        return "in_progress" if status == "running" else status
    cards = ((response.card,) if response.card else ()) + response.cards
    for card in cards:
        if card.kind == "selection" or (
            card.kind == "preparation" and card.payload.get("status") == "needs_information"
        ):
            return "awaiting_clarification"
        if card.kind == "approvals":
            payload = ApprovalPayload.model_validate(card.payload)
            if any(item.paused and item.review.status == "pending" for item in payload.items):
                return "awaiting_approval"
    return "completed"
