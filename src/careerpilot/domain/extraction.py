"""Unreviewed extraction schemas: source evidence is required, never user confirmation."""

from enum import StrEnum
from math import isfinite
from typing import Self

from pydantic import StrictBool, StrictFloat, field_validator, model_validator

from careerpilot.domain.common import Model
from careerpilot.domain.profile import LanguageLevel, SkillProficiency


class ExtractionState(StrEnum):
    MISSING = "missing"
    UNKNOWN = "unknown"
    PRESENT = "present"
    CONFLICTING = "conflicting"


class SourceSnippet(Model):
    quote: str

    @field_validator("quote")
    @classmethod
    def bounded_quote(cls, value: str) -> str:
        if not value.strip() or len(value) > 1000:
            raise ValueError("Evidence quote must contain 1–1000 characters")
        return value


class ExtractedValue[T](Model):
    # All fields are required, including nullable ones, for native strict JSON schemas.
    state: ExtractionState
    value: T | None
    evidence: tuple[SourceSnippet, ...]
    alternatives: tuple[T, ...]

    @model_validator(mode="after")
    def consistent_value(self) -> Self:
        if len(self.evidence) > 8 or len(self.alternatives) > 8:
            raise ValueError("Too many evidence quotes or alternatives")
        if self.state == ExtractionState.PRESENT:
            if self.value is None or not self.evidence or self.alternatives:
                raise ValueError("Present values require evidence and no alternatives")
        elif self.state == ExtractionState.CONFLICTING:
            distinct = {str(item) for item in self.alternatives}
            if (
                self.value is not None
                or len(distinct) < 2
                or len({item.quote for item in self.evidence}) < 2
            ):
                raise ValueError("Conflicts require distinct alternatives and multiple quotes")
        elif self.state == ExtractionState.UNKNOWN:
            if self.value is not None or not self.evidence or self.alternatives:
                raise ValueError("Unknown values require ambiguity evidence, not a value")
        elif self.value is not None or self.evidence or self.alternatives:
            raise ValueError("Missing values cannot assert facts or evidence")
        for value in (self.value, *self.alternatives):
            if isinstance(value, str) and (not value.strip() or len(value) > 2000):
                raise ValueError("Extracted strings must contain 1–2000 characters")
        return self


class ExtractedSkill(Model):
    name: ExtractedValue[str]
    minimum_proficiency: ExtractedValue[SkillProficiency]
    mandatory: ExtractedValue[StrictBool]


class ExtractedQualification(Model):
    description: ExtractedValue[str]
    mandatory: ExtractedValue[StrictBool]


class ExtractedExperience(Model):
    description: ExtractedValue[str]
    minimum_years: ExtractedValue[StrictFloat]
    mandatory: ExtractedValue[StrictBool]

    @model_validator(mode="after")
    def valid_years(self) -> Self:
        for value in (self.minimum_years.value, *self.minimum_years.alternatives):
            if value is not None and (not isfinite(value) or not 0 <= value <= 80):
                raise ValueError("Experience years must be finite and between 0 and 80")
        return self


class ExtractedLanguage(Model):
    name: ExtractedValue[str]
    minimum_level: ExtractedValue[LanguageLevel]
    mandatory: ExtractedValue[StrictBool]


class WorkArrangement(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    TEMPORARY = "temporary"
    INTERNSHIP = "internship"
    OTHER = "other"


class ExtractionWarning(StrEnum):
    MISSING_INFORMATION = "missing_information"
    AMBIGUOUS_INFORMATION = "ambiguous_information"
    CONTRADICTORY_INFORMATION = "contradictory_information"
    INSTRUCTION_LIKE_CONTENT = "instruction_like_content"
    UNSUPPORTED_REQUIREMENT = "unsupported_requirement"


class JobExtraction(Model):
    title: ExtractedValue[str]
    employer: ExtractedValue[str]
    skills: tuple[ExtractedSkill, ...]
    qualifications: tuple[ExtractedQualification, ...]
    experience: tuple[ExtractedExperience, ...]
    languages: tuple[ExtractedLanguage, ...]
    location: ExtractedValue[str]
    work_arrangement: ExtractedValue[WorkArrangement]
    employment_type: ExtractedValue[EmploymentType]
    warnings: tuple[ExtractionWarning, ...]

    @model_validator(mode="after")
    def bounded_records(self) -> Self:
        for skill in self.skills:
            if skill.name.state == ExtractionState.MISSING:
                raise ValueError("Skill entries require a stated or ambiguous name")
        for language in self.languages:
            if language.name.state == ExtractionState.MISSING:
                raise ValueError("Language entries require a stated or ambiguous name")
        described: tuple[ExtractedQualification | ExtractedExperience, ...] = (
            *self.qualifications,
            *self.experience,
        )
        for record in described:
            if record.description.state == ExtractionState.MISSING:
                raise ValueError("Requirement entries require a stated or ambiguous description")
        if any(
            len(items) > 50
            for items in (self.skills, self.qualifications, self.experience, self.languages)
        ):
            raise ValueError("At most 50 entries per extraction category")
        return self
