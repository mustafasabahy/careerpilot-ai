"""Bounded ephemeral context; summaries are never authoritative memory."""

from collections.abc import Callable
from typing import Literal
from uuid import UUID

from pydantic import Field

from careerpilot.application.memory import MemoryConflict, MemoryDenied, MemoryPrincipal
from careerpilot.domain.career_intelligence import profile_catalog
from careerpilot.domain.common import Model
from careerpilot.domain.memory import MemoryItem
from careerpilot.domain.preparation import FactKind, ProfileFact
from careerpilot.domain.profile import CandidateProfile


class ContextMessage(Model):
    role: Literal["user", "assistant", "tool"]
    text: str = Field(max_length=16000)


class ProfileContextSelection(Model):
    fact_ids: tuple[UUID, ...] = Field(default=(), max_length=20)
    kinds: tuple[FactKind, ...] = Field(
        default=(
            FactKind.SKILL,
            FactKind.LANGUAGE,
            FactKind.EXPERIENCE,
            FactKind.EDUCATION,
            FactKind.CERTIFICATION,
        ),
        max_length=7,
    )


class BoundedContext(Model):
    memories: tuple[MemoryItem, ...]
    profile_facts: tuple[ProfileFact, ...]
    messages: tuple[ContextMessage, ...]
    omission_summary: str
    authoritative_summary: Literal[False] = False
    trust: Literal["data_only_no_permissions"] = "data_only_no_permissions"


def clip_utf8(text: str, size: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= size:
        return text
    suffix = " [truncated]"
    return encoded[: size - len(suffix)].decode("utf-8", errors="ignore") + suffix


def trim_messages(raw: tuple[ContextMessage, ...]) -> tuple[tuple[ContextMessage, ...], str]:
    if len(raw) > 100:
        raise ValueError("At most 100 input messages are accepted")
    messages = tuple(ContextMessage.model_validate(item.model_dump()) for item in raw)
    selected: list[ContextMessage] = []
    budget = 8192
    shortened = 0
    for message in reversed(messages[-8:]):
        text = clip_utf8(message.text, min(2048, budget))
        if text != message.text:
            shortened += 1
        selected.append(ContextMessage(role=message.role, text=text))
        budget -= len(text.encode("utf-8"))
        if budget < 32:
            break
    omitted = len(messages) - len(selected)
    summary = f"Non-authoritative context accounting: {omitted} omitted; {shortened} truncated."
    return tuple(reversed(selected)), summary


ProfileLoader = Callable[[UUID], CandidateProfile]


class ContextBuilder:
    def __init__(self, principal: MemoryPrincipal, profile_loader: ProfileLoader | None = None):
        self.principal = principal
        self.profile_loader = profile_loader

    def build(
        self,
        memories: tuple[MemoryItem, ...],
        candidate_id: UUID | None,
        messages: tuple[ContextMessage, ...] = (),
        selection: ProfileContextSelection | None = None,
    ) -> BoundedContext:
        if any(item.owner_id != self.principal.owner_id for item in memories):
            raise MemoryDenied("Context cannot include another user's memory")
        if len(memories) > 20:
            raise MemoryConflict("Context memory count exceeded")
        # Memory evidence/metadata can be larger than the visible statement.
        if sum(len(item.model_dump_json().encode()) for item in memories) > 24000:
            raise MemoryConflict("Selected memory exceeds context budget; narrow selection")
        selection = ProfileContextSelection.model_validate(
            (selection or ProfileContextSelection()).model_dump()
        )
        facts: tuple[ProfileFact, ...] = ()
        omitted_facts = 0
        if candidate_id is not None:
            if candidate_id not in self.principal.candidate_ids or self.profile_loader is None:
                raise MemoryDenied("Profile context is not authorized")
            profile = self.profile_loader(candidate_id)
            if profile.id != candidate_id:
                raise MemoryDenied("Profile loader returned a different candidate")
            # Same evidence policy as Phase 6; do not merge memory into domain facts.
            catalog, _ = profile_catalog(profile)
            collected: list[ProfileFact] = []
            remaining = 16000
            for fact in catalog:
                if (
                    fact.verification != "user_confirmed"
                    or fact.kind == FactKind.AUTHORIZATION
                    or fact.kind not in selection.kinds
                    or (selection.fact_ids and fact.id not in selection.fact_ids)
                ):
                    continue
                size = len(fact.model_dump_json().encode())
                if len(collected) >= 20 or size > remaining:
                    omitted_facts += 1
                    continue
                collected.append(fact)
                remaining -= size
            facts = tuple(collected)
        trimmed, summary = trim_messages(messages)
        return BoundedContext(
            memories=memories,
            profile_facts=facts,
            messages=trimmed,
            omission_summary=summary + f" {omitted_facts} eligible profile facts omitted for size.",
        )
