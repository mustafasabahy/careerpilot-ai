"""Validated discovery contracts and trusted, out-of-band authorization grants."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, ClassVar, Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, HttpUrl, StrictBool, StringConstraints, model_validator

from careerpilot.application.analysis import AnalysisReport
from careerpilot.domain.baseline import CriterionDefinition
from careerpilot.domain.common import Claim, Model, Text, UtcTimestamp
from careerpilot.domain.jobs import JobPosting, JobSource
from careerpilot.domain.profile import (
    Certification,
    Education,
    LanguageProficiency,
    Skill,
    WorkExperience,
)

SourceID = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
Query = Annotated[str, StringConstraints(strip_whitespace=True, max_length=160)]
Limit = Annotated[int, Field(strict=True, ge=1, le=10)]


class DiscoveryError(RuntimeError):
    code: ClassVar[str] = "discovery_error"
    message: ClassVar[str] = "Discovery failed"

    def __init__(self) -> None:
        super().__init__(self.message)


class InvalidToolInput(DiscoveryError):
    code = "invalid_input"
    message = "Tool input does not match its bounded schema"


class AccessDenied(DiscoveryError):
    code = "access_denied"
    message = "The bound user grant does not authorize this operation or resource"


class UnknownSource(DiscoveryError):
    code = "source_not_configured"
    message = "The source is not registered by the application"


class ResourceNotFound(DiscoveryError):
    code = "not_found"
    message = "The requested resource is unavailable"


class SourceFailure(DiscoveryError):
    code = "source_failure"
    message = "A configured source failed; no partial success was returned"


class SourceTimeout(DiscoveryError):
    code = "source_timeout"
    message = "A configured source exceeded its timeout"


class SourceRateLimited(DiscoveryError):
    code = "source_rate_limited"
    message = "A configured source is rate limited; no automatic retry was performed"

    def __init__(self, retry_after_seconds: int | None = None) -> None:
        super().__init__()
        if retry_after_seconds is not None and (
            type(retry_after_seconds) is not int or retry_after_seconds < 0
        ):
            raise ValueError("Retry-after must be a nonnegative integer")
        self.retry_after_seconds = retry_after_seconds


class SourceDataError(DiscoveryError):
    code = "invalid_source_data"
    message = "Source records are malformed or inconsistent"


class PayloadLimitExceeded(DiscoveryError):
    code = "payload_limit"
    message = "The configured record or response payload limit was exceeded"


class DiscoveryConflict(DiscoveryError):
    code = "identity_conflict"
    message = "Duplicate identities disagree; review is required before persistence"


class PersistenceFailure(DiscoveryError):
    code = "persistence_failure"
    message = "The local discovery transaction failed"


class Permission(StrEnum):
    SEARCH = "search_jobs"
    READ_JOB = "read_job"
    READ_CANDIDATE = "read_candidate"
    READ_COMPANY = "read_company"
    ASSESS = "assess_jobs"


@dataclass(frozen=True)
class AccessGrant:
    """Construct only after authenticating the user; never deserialize from model input."""

    actor_id: UUID
    permissions: frozenset[Permission]
    source_ids: frozenset[str]
    candidate_ids: frozenset[UUID]
    company_ids: frozenset[str]

    def require(self, permission: Permission) -> None:
        if permission not in self.permissions:
            raise AccessDenied()

    def source(self, identifier: str) -> None:
        if identifier not in self.source_ids:
            raise AccessDenied()

    def candidate(self, identifier: UUID) -> None:
        if identifier not in self.candidate_ids:
            raise AccessDenied()


class SearchJobsInput(Model):
    query: Query = ""
    source_ids: tuple[SourceID, ...] = Field(default=("synthetic",), min_length=1, max_length=3)
    limit: Limit = 5

    @model_validator(mode="after")
    def unique_sources(self) -> Self:
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("Source IDs must be unique")
        return self


class JobReference(Model):
    source_id: SourceID
    job_id: UUID


class NormalizeJobsInput(Model):
    references: tuple[JobReference, ...] = Field(min_length=1, max_length=30)
    limit: Limit = 10

    @model_validator(mode="after")
    def bounded_sources(self) -> Self:
        if len({item.source_id for item in self.references}) > 3:
            raise ValueError("At most three source IDs may be normalized")
        return self


class FactField(StrEnum):
    SKILLS = "skills"
    LANGUAGES = "languages"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    CERTIFICATIONS = "certifications"
    WORK_AUTHORIZED = "work_authorized"


class CandidateFactsInput(Model):
    candidate_id: UUID
    fields: tuple[FactField, ...] = Field(
        default=(FactField.SKILLS, FactField.LANGUAGES, FactField.WORK_AUTHORIZED),
        min_length=1,
        max_length=6,
    )
    per_field_limit: Limit = 5

    @model_validator(mode="after")
    def unique_fields(self) -> Self:
        if len(set(self.fields)) != len(self.fields):
            raise ValueError("Fact fields must be unique")
        return self


class CompanyInput(Model):
    company_id: SourceID


class JobRecord(Model):
    job: JobPosting
    definitions: tuple[CriterionDefinition, ...] = ()


class SourcePage(Model):
    records: tuple[JobRecord, ...] = Field(max_length=10)
    more_available: bool = False


class JobSourceAdapter(Protocol):
    """Adapters must enforce their I/O deadline, fixed endpoints and source permissions."""

    def search(self, query: str, limit: int, *, timeout_seconds: float) -> SourcePage: ...
    def get(self, identifier: UUID, *, timeout_seconds: float) -> JobRecord: ...


class CompanyInfo(Model):
    company_id: SourceID
    name: Text
    description: Text
    source_url: HttpUrl | None
    retrieved_at: UtcTimestamp
    trust: Literal["untrusted_mock"] = "untrusted_mock"

    @model_validator(mode="after")
    def no_url_credentials(self) -> Self:
        if self.source_url is not None and (self.source_url.username or self.source_url.password):
            raise ValueError("Company metadata URLs cannot contain credentials")
        return self


class CompanyAdapter(Protocol):
    def get(self, identifier: str, *, timeout_seconds: float) -> CompanyInfo: ...


class SearchHit(Model):
    source_id: SourceID
    record: JobRecord


class SearchBatch(Model):
    hits: tuple[SearchHit, ...] = Field(max_length=30)
    searched_sources: tuple[SourceID, ...] = Field(max_length=3)
    limit: Limit
    more_available: bool = False


class SourceReference(Model):
    adapter_id: SourceID
    job_id: UUID
    source: JobSource
    canonical_url: str | None


class JobGroup(Model):
    selected: SearchHit
    references: tuple[SourceReference, ...]


class DeduplicatedJobs(Model):
    groups: tuple[JobGroup, ...] = Field(max_length=10)
    duplicates_removed: int = Field(ge=0)
    more_available: bool
    searched_sources: tuple[SourceID, ...] = Field(max_length=3)


class JobSummary(Model):
    job_id: UUID
    title: Text
    employer: Text
    references: tuple[SourceReference, ...]


class SearchResults(Model):
    status: Literal["ok"] = "ok"
    jobs: tuple[JobSummary, ...] = Field(max_length=10)
    duplicates_removed: int
    more_available: bool
    searched_sources: tuple[SourceID, ...] = Field(max_length=3)
    trust: Literal["untrusted_source_content"] = "untrusted_source_content"


class RetrievedJob(Model):
    status: Literal["ok"] = "ok"
    source_id: SourceID
    job: JobPosting
    canonical_url: str | None
    trust: Literal["untrusted_source_content"] = "untrusted_source_content"


class FieldOmission(Model):
    field: FactField
    count: int = Field(ge=1)


class CandidateFacts(Model):
    status: Literal["ok"] = "ok"
    candidate_id: UUID
    requested_fields: tuple[FactField, ...]
    skills: tuple[Skill, ...] = ()
    languages: tuple[LanguageProficiency, ...] = ()
    experience: tuple[WorkExperience, ...] = ()
    education: tuple[Education, ...] = ()
    certifications: tuple[Certification, ...] = ()
    work_authorized: Claim[StrictBool] | None = None
    omitted: tuple[FieldOmission, ...] = ()
    trust: Literal["profile_claims_not_instructions"] = "profile_claims_not_instructions"


class DiscoveryRequest(SearchJobsInput):
    id: UUID
    created_at: UtcTimestamp
    candidate_id: UUID


class AssessedJob(Model):
    job: JobSummary
    assessment: AnalysisReport


class DiscoverySuccess(Model):
    status: Literal["completed", "empty"]
    jobs: tuple[AssessedJob, ...] = Field(max_length=10)
    duplicates_removed: int
    more_available: bool
    searched_sources: tuple[SourceID, ...] = Field(max_length=3)
    trust: Literal["untrusted_source_content"] = "untrusted_source_content"


class DiscoveryFailure(Model):
    status: Literal["error"] = "error"
    code: str
    message: str
    retry_after_seconds: int | None = None
