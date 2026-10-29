"""Owner-bound memory use cases and ports; no model or graph dependency."""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from careerpilot.domain.common import utc_now
from careerpilot.domain.memory import MemoryDelete, MemoryItem, MemoryWrite


class MemoryDenied(PermissionError):
    """No authorized resource exists within the bound user's scope."""


class MemoryConflict(ValueError):
    """An optimistic version, context revision or thread lifecycle changed."""


@dataclass(frozen=True)
class MemoryPrincipal:
    # Construct only after authenticating the user; never from model/tool input.
    owner_id: UUID
    candidate_ids: frozenset[UUID] = frozenset()


class MemoryRepository(Protocol):
    def items(self, owner_id: UUID) -> tuple[MemoryItem, ...]: ...
    def revision(self, owner_id: UUID) -> int: ...
    def put(self, owner_id: UUID, item: MemoryItem, expected_version: int) -> None: ...
    def delete(self, owner_id: UUID, identifier: UUID, expected_version: int) -> None: ...
    def clear(self, owner_id: UUID) -> None: ...
    def register_thread(
        self, owner_id: UUID, thread_id: UUID, candidate_id: UUID | None
    ) -> None: ...
    def require_thread(self, owner_id: UUID, thread_id: UUID) -> UUID | None: ...
    def threads(self, owner_id: UUID) -> tuple[UUID, ...]: ...
    def mark_thread_deleted(self, owner_id: UUID, thread_id: UUID) -> None: ...


class MemoryDatabase(Protocol):
    def transaction(self) -> AbstractContextManager[MemoryRepository]: ...


class CareerMemory:
    def __init__(self, principal: MemoryPrincipal, database: MemoryDatabase) -> None:
        self.principal = principal
        self.database = database

    def list(self) -> tuple[MemoryItem, ...]:
        with self.database.transaction() as repository:
            return repository.items(self.principal.owner_id)

    def confirm(self, raw: object, *, explicitly_confirmed: bool) -> MemoryItem:
        # This is a trusted application operation, deliberately not a LangChain tool.
        if explicitly_confirmed is not True:
            raise MemoryDenied("Explicit user confirmation is required")
        if isinstance(raw, MemoryWrite):
            raw = raw.model_dump()
        request = MemoryWrite.model_validate(raw)
        item = MemoryItem(
            id=request.id,
            owner_id=self.principal.owner_id,
            version=request.expected_version + 1,
            confirmed_at=utc_now(),
            value=request.value,
        )
        with self.database.transaction() as repository:
            repository.put(self.principal.owner_id, item, request.expected_version)
        return item

    def delete(self, identifier: UUID, *, expected_version: int) -> None:
        request = MemoryDelete(id=identifier, expected_version=expected_version)
        with self.database.transaction() as repository:
            repository.delete(self.principal.owner_id, request.id, request.expected_version)

    def transaction(self) -> AbstractContextManager[MemoryRepository]:
        return self.database.transaction()

    def selected(
        self, repository: MemoryRepository, kinds: tuple[str, ...], limit: int
    ) -> tuple[MemoryItem, ...]:
        return tuple(
            item for item in repository.items(self.principal.owner_id) if item.value.kind in kinds
        )[:limit]
