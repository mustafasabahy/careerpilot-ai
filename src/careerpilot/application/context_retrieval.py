"""Bounded retrieval of confirmed, owner-scoped career memory."""

from uuid import UUID

from pydantic import Field

from careerpilot.application.memory import CareerMemory
from careerpilot.domain.common import Model
from careerpilot.domain.memory import MemoryItem, MemoryKind


class MemoryReference(Model):
    id: UUID
    version: int = Field(ge=1, strict=True)
    kind: MemoryKind
    provenance: str = "explicit_user_confirmation"


class RetrievedMemory(Model):
    reference: MemoryReference
    value: str = Field(min_length=1, max_length=500)
    confirmed: bool = True


class MemoryRetrieval(Model):
    items: tuple[RetrievedMemory, ...] = Field(default=(), max_length=20)
    truncated: bool = False
    revision: int = Field(ge=0, strict=True)


class ConfirmedMemoryRetriever:
    """Retrieve only confirmed memories through an authenticated CareerMemory."""

    def __init__(self, memory: CareerMemory) -> None:
        self.memory = memory

    def retrieve(
        self,
        query: str = "",
        *,
        kinds: tuple[MemoryKind, ...] = (),
        limit: int = 10,
        max_context_chars: int = 4000,
    ) -> MemoryRetrieval:
        if type(query) is not str or len(query) > 500:
            raise ValueError("Memory query must be a string of at most 500 characters")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Memory result limit must be between 1 and 20")
        if type(max_context_chars) is not int or not 1 <= max_context_chars <= 10000:
            raise ValueError("Memory context limit must be between 1 and 10000")
        requested_kinds = set(kinds)
        if len(requested_kinds) != len(kinds):
            raise ValueError("Memory kinds must be unique")
        terms = {part.casefold() for part in query.split() if part.strip()}
        with self.memory.transaction() as repository:
            items = repository.items(self.memory.principal.owner_id)
            revision = repository.revision(self.memory.principal.owner_id)
            candidates = [
                item for item in items if not requested_kinds or item.value.kind in requested_kinds
            ]
            ranked = sorted(
                candidates,
                key=lambda item: (-self._matches(item, terms), str(item.id)),
            )
            selected: list[RetrievedMemory] = []
            used = 0
            for item in ranked:
                if terms and self._matches(item, terms) == 0:
                    continue
                value = self._display(item)
                if used + len(value) > max_context_chars:
                    break
                selected.append(
                    RetrievedMemory(
                        reference=MemoryReference(
                            id=item.id,
                            version=item.version,
                            kind=item.value.kind,
                        ),
                        value=value,
                    )
                )
                used += len(value)
                if len(selected) == limit:
                    break
            return MemoryRetrieval(
                items=tuple(selected),
                truncated=len(selected) < len(candidates),
                revision=revision,
            )

    @staticmethod
    def _display(item: MemoryItem) -> str:
        value = item.value
        data = value.model_dump()
        data.pop("kind", None)
        return "; ".join(f"{key}: {raw}" for key, raw in data.items())

    @staticmethod
    def _matches(item: MemoryItem, terms: set[str]) -> int:
        if not terms:
            return 0
        haystack = ConfirmedMemoryRetriever._display(item).casefold()
        return sum(term in haystack for term in terms)
