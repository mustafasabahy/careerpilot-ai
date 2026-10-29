"""Explicitly confirmed user memory; never inferred from assistant messages."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from careerpilot.domain.common import Evidence, EvidenceOrigin, Model, UtcTimestamp

MemoryText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class JobPreferences(Model):
    kind: Literal["job_preferences"] = "job_preferences"
    roles: tuple[MemoryText, ...] = Field(default=(), max_length=10)
    skills: tuple[MemoryText, ...] = Field(default=(), max_length=10)


class LocationPreferences(Model):
    kind: Literal["location_preferences"] = "location_preferences"
    locations: tuple[MemoryText, ...] = Field(default=(), max_length=10)
    arrangements: tuple[Literal["remote", "hybrid", "onsite"], ...] = Field(
        default=(), max_length=3
    )


class ConfirmedCareerFact(Model):
    kind: Literal["career_fact"] = "career_fact"
    statement: MemoryText
    evidence: tuple[Evidence, ...] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def explicitly_confirmed(self) -> Self:
        if any(item.origin != EvidenceOrigin.USER_STATEMENT for item in self.evidence):
            raise ValueError("Durable career facts require explicit user-statement evidence")
        return self


class CareerGoal(Model):
    kind: Literal["career_goal"] = "career_goal"
    description: MemoryText
    target_date: MemoryText | None = None


class ExplicitCorrection(Model):
    kind: Literal["correction"] = "correction"
    replaces_id: UUID
    replaces_version: int = Field(ge=1, strict=True)
    replacement: ConfirmedCareerFact
    reason: MemoryText


MemoryValue = Annotated[
    JobPreferences | LocationPreferences | ConfirmedCareerFact | CareerGoal | ExplicitCorrection,
    Field(discriminator="kind"),
]
MemoryKind = Literal[
    "job_preferences", "location_preferences", "career_fact", "career_goal", "correction"
]


class MemoryWrite(Model):
    id: UUID
    value: MemoryValue
    expected_version: int = Field(ge=0, strict=True)


class MemoryItem(Model):
    id: UUID
    owner_id: UUID
    version: int = Field(ge=1, strict=True)
    confirmed_at: UtcTimestamp
    value: MemoryValue
    origin: Literal["explicit_user_confirmation"] = "explicit_user_confirmation"


class MemoryDelete(Model):
    id: UUID
    expected_version: int = Field(ge=1, strict=True)
