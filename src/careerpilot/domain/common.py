"""Identity, time and evidence shared by career concepts."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
UtcTimestamp = Annotated[AwareDatetime, AfterValidator(lambda value: value.astimezone(UTC))]


def utc_now() -> datetime:
    return datetime.now(UTC)


class Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)


class Entity(Model):
    id: UUID = Field(default_factory=uuid4)
    created_at: UtcTimestamp = Field(default_factory=utc_now)


class EvidenceOrigin(StrEnum):
    USER_STATEMENT = "user_statement"
    SOURCE_DOCUMENT = "source_document"
    AI_INFERENCE = "ai_inference"


class Evidence(Entity):
    origin: EvidenceOrigin
    reference: Text  # Synthetic/local evidence locator, not raw document contents.


EvidenceSet = Annotated[tuple[Evidence, ...], Field(min_length=1)]


class KnowledgeState(StrEnum):
    MISSING = "missing"
    UNKNOWN = "unknown"
    KNOWN = "known"


class Claim[T](Model):
    state: KnowledgeState = KnowledgeState.MISSING
    value: T | None = None
    evidence: tuple[Evidence, ...] = ()

    @model_validator(mode="after")
    def consistent_claim(self) -> Self:
        if self.state == KnowledgeState.KNOWN:
            if self.value is None or not self.evidence:
                raise ValueError("Known claims require a value and evidence")
        elif self.value is not None or self.evidence:
            raise ValueError("Missing/unknown claims cannot assert a value or evidence")
        return self

    @property
    def user_confirmed(self) -> bool:
        return any(item.origin == EvidenceOrigin.USER_STATEMENT for item in self.evidence)
