"""Sensitive-action ports, trusted authorization and normalized adapter errors."""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from careerpilot.application.storage import CareerRepository
from careerpilot.domain.actions import (
    ActionAudit,
    ActionDecision,
    ActionKind,
    ActionOutcome,
    ActionProposal,
    ActionStatus,
    ShortText,
)
from careerpilot.domain.common import Model


class ActionDenied(PermissionError):
    """The trusted principal does not authorize this resource/action."""


class ActionConflict(ValueError):
    """The action version, decision, prerequisite or lifecycle has changed."""


class AdapterUnavailable(RuntimeError):
    """No safe external adapter has been configured; nothing was attempted."""


class AmbiguousExecution(RuntimeError):
    """An external effect may have happened; do not automatically retry."""


class DefiniteAdapterFailure(RuntimeError):
    """Adapter guarantees no effect occurred."""


@dataclass(frozen=True)
class ActionPrincipal:
    # Established by an authenticated application boundary, never by an LLM.
    owner_id: UUID
    actor_id: UUID
    candidate_ids: frozenset[UUID]
    permissions: frozenset[ActionKind]
    target_ids: frozenset[UUID] = frozenset()


class AdapterReceipt(Model):
    status: Literal["simulated"] = "simulated"
    receipt_id: ShortText


class ActionAdapter(Protocol):
    @property
    def identifier(self) -> str: ...
    @property
    def enabled(self) -> bool: ...
    def execute(
        self, proposal: ActionProposal, idempotency_key: str, *, timeout_seconds: float = 30
    ) -> AdapterReceipt: ...


class ActionRepository(CareerRepository, Protocol):
    def proposal(self, owner_id: UUID, identifier: UUID) -> ActionProposal: ...
    def thread_proposal(self, owner_id: UUID, thread_id: UUID) -> ActionProposal: ...
    def add_proposal(self, proposal: ActionProposal, actor_id: UUID) -> None: ...
    def status(self, proposal_id: UUID) -> ActionStatus: ...
    def decision(self, proposal_id: UUID) -> ActionDecision | None: ...
    def outcome(self, proposal_id: UUID) -> ActionOutcome | None: ...
    def decide(self, event: ActionDecision) -> None: ...
    def claim(self, proposal_id: UUID, actor_id: UUID) -> bool: ...
    def finish(self, outcome: ActionOutcome, actor_id: UUID) -> None: ...
    def audit(self, proposal_id: UUID) -> tuple[ActionAudit, ...]: ...


class ActionDatabase(Protocol):
    def transaction(self) -> AbstractContextManager[ActionRepository]: ...
