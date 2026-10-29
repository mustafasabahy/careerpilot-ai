"""Job source identity, explicit requirements and evidence-based fit outcomes."""

from enum import StrEnum
from typing import Self
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from pydantic import Field, HttpUrl, StrictBool, field_validator, model_validator

from careerpilot.domain.common import Entity, Evidence, Model, Text, UtcTimestamp, utc_now


def canonical_url(url: HttpUrl) -> str:
    """Conservative normalization: preserve scheme/path and nontracking query values."""
    parts = urlsplit(str(url))
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"gclid", "fbclid"}
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(sorted(query)), ""))


class JobSource(Model):
    name: Text
    external_id: Text | None = None
    url: HttpUrl | None = None
    retrieved_at: UtcTimestamp = Field(default_factory=utc_now)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return value.casefold()

    @model_validator(mode="after")
    def safe_url(self) -> Self:
        if self.url is not None and (
            self.url.username is not None or self.url.password is not None
        ):
            raise ValueError("Source URLs cannot contain credentials")
        return self

    @property
    def canonical_url(self) -> str | None:
        return canonical_url(self.url) if self.url is not None else None


class RequirementCategory(StrEnum):
    SKILL = "skill"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    LANGUAGE = "language"
    ELIGIBILITY = "eligibility"


class JobRequirement(Entity):
    description: Text
    category: RequirementCategory
    mandatory: StrictBool | None = None  # None means not specified by source.
    evidence: tuple[Evidence, ...] = Field(min_length=1)


class JobPosting(Entity):
    title: Text
    employer: Text
    source: JobSource
    requirements: tuple[JobRequirement, ...] = ()

    @model_validator(mode="after")
    def unique_requirements(self) -> Self:
        if len({item.id for item in self.requirements}) != len(self.requirements):
            raise ValueError("Duplicate requirement identifiers")
        return self


class RequirementOutcome(StrEnum):
    MET = "met"
    UNMET = "unmet"
    UNKNOWN = "unknown"


class RequirementResult(Model):
    requirement_id: UUID
    outcome: RequirementOutcome
    evidence: tuple[Evidence, ...] = ()

    @model_validator(mode="after")
    def evidence_for_decision(self) -> Self:
        if self.outcome != RequirementOutcome.UNKNOWN and not self.evidence:
            raise ValueError("Decided requirement results require evidence")
        return self


class FitDecision(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    NEEDS_REVIEW = "needs_review"


class JobFitAssessment(Entity):
    candidate_id: UUID
    job_id: UUID
    results: tuple[RequirementResult, ...] = ()
    ai_score: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)

    @model_validator(mode="after")
    def unique_results(self) -> Self:
        if len({item.requirement_id for item in self.results}) != len(self.results):
            raise ValueError("Duplicate requirement results")
        return self

    def decision(self, job: JobPosting) -> FitDecision:
        if job.id != self.job_id:
            raise ValueError("Assessment belongs to another job")
        requirements = {item.id: item for item in job.requirements}
        if any(item.requirement_id not in requirements for item in self.results):
            raise ValueError("Result refers to a requirement absent from the job")
        results = {item.requirement_id: item for item in self.results}
        uncertain = not requirements
        for requirement in job.requirements:
            result = results.get(requirement.id)
            # AI inference alone cannot satisfy or reject an explicit rule.
            trusted = result is not None and any(
                item.origin.value != "ai_inference" for item in result.evidence
            )
            if requirement.mandatory is True:
                if trusted and result is not None and result.outcome == RequirementOutcome.UNMET:
                    return FitDecision.INELIGIBLE
                if not trusted or result is None or result.outcome != RequirementOutcome.MET:
                    uncertain = True
            elif requirement.mandatory is None:
                uncertain = True
        return FitDecision.NEEDS_REVIEW if uncertain else FitDecision.ELIGIBLE
