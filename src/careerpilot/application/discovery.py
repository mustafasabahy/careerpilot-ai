"""Bound discovery tools/use cases; no LangChain, filesystem, network or SQL imports."""

import math
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from uuid import uuid5

from pydantic import BaseModel, TypeAdapter, ValidationError

from careerpilot.application.analysis import AnalysisRequest, AnalysisService, TransactionFactory
from careerpilot.application.discovery_contracts import (
    AccessDenied,
    AccessGrant,
    AssessedJob,
    CandidateFacts,
    CandidateFactsInput,
    CompanyAdapter,
    CompanyInfo,
    CompanyInput,
    DeduplicatedJobs,
    DiscoveryConflict,
    DiscoveryError,
    DiscoveryRequest,
    DiscoverySuccess,
    FactField,
    FieldOmission,
    InvalidToolInput,
    JobGroup,
    JobRecord,
    JobReference,
    JobSourceAdapter,
    JobSummary,
    NormalizeJobsInput,
    PayloadLimitExceeded,
    Permission,
    PersistenceFailure,
    ResourceNotFound,
    RetrievedJob,
    SearchBatch,
    SearchHit,
    SearchJobsInput,
    SearchResults,
    SourceDataError,
    SourceFailure,
    SourceID,
    SourcePage,
    SourceTimeout,
    UnknownSource,
)
from careerpilot.application.discovery_normalization import deduplicate
from careerpilot.application.execution import consume, remaining_timeout, retry_read
from careerpilot.application.failures import ExecutionFailure
from careerpilot.application.storage import IntegrityViolation, NotFound
from careerpilot.domain.baseline import BaselineRules, CriteriaError, normalize_requirements
from careerpilot.domain.jobs import JobPosting
from careerpilot.domain.profile import CandidateProfile
from careerpilot.domain.reliability import RetryPolicy

MAX_OUTPUT_BYTES = 128 * 1024
MAX_RECORD_BYTES = 16 * 1024


def _parse[T: BaseModel](schema: type[T], value: object, *, source: bool = False) -> T:
    try:
        if isinstance(value, BaseModel):
            value = value.model_dump(warnings=False)
        return schema.model_validate(value)
    except ValidationError:
        if source:
            raise SourceDataError() from None
        raise InvalidToolInput() from None


def _bounded[T: BaseModel](value: T, maximum: int = MAX_OUTPUT_BYTES) -> T:
    if len(value.model_dump_json().encode()) > maximum:
        raise PayloadLimitExceeded()
    return value


def _source_once[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except (DiscoveryError, ExecutionFailure):
        raise
    except TimeoutError:
        raise SourceTimeout() from None
    except Exception:
        # Third-party adapter boundary: no raw source error/body is exposed.
        raise SourceFailure() from None


def _source_call[T](operation: Callable[[], T], policy: RetryPolicy) -> T:
    return retry_read(lambda: _source_once(operation), policy, resource="tool_calls")


def _summary(group: JobGroup) -> JobSummary:
    validated = group
    return JobSummary(
        job_id=validated.selected.record.job.id,
        title=validated.selected.record.job.title,
        employer=validated.selected.record.job.employer,
        references=validated.references,
    )


class DiscoveryTools:
    def __init__(
        self,
        grant: AccessGrant,
        sources: Mapping[str, JobSourceAdapter],
        companies: CompanyAdapter,
        transaction: TransactionFactory,
        rules: BaselineRules,
        *,
        timeout_seconds: float = 5,
        read_retries: int = 0,
    ) -> None:
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise ValueError("Source timeout must be finite and in (0, 30] seconds")
        for identifier in sources:
            TypeAdapter(SourceID).validate_python(identifier)
        self._grant = grant
        self._sources = dict(sources)
        self._companies = companies
        self._transaction = transaction
        self._rules = rules
        self._timeout = timeout_seconds
        self._retry = RetryPolicy(max_retries=read_retries)

    def _source(self, identifier: str, permission: Permission) -> JobSourceAdapter:
        self._grant.require(permission)
        self._grant.source(identifier)
        if identifier not in self._sources:
            raise UnknownSource()
        return self._sources[identifier]

    def authorize_discovery(self, raw: object) -> DiscoveryRequest:
        request = _parse(DiscoveryRequest, raw)
        for permission in (
            Permission.SEARCH,
            Permission.READ_JOB,
            Permission.READ_CANDIDATE,
            Permission.ASSESS,
        ):
            self._grant.require(permission)
        self._grant.candidate(request.candidate_id)
        for source in request.source_ids:
            self._source(source, Permission.SEARCH)
        try:
            with self._transaction() as repository:
                repository.get(CandidateProfile, request.candidate_id)
        except NotFound:
            raise ResourceNotFound() from None
        except DiscoveryError:
            raise
        except ExecutionFailure:
            raise
        except Exception:
            raise PersistenceFailure() from None
        return request

    def search_candidates(self, raw: object) -> SearchBatch:
        request = _parse(SearchJobsInput, raw)
        # Authorize all sources before contacting any, avoiding partial unauthorized work.
        adapters = [
            (identifier, self._source(identifier, Permission.SEARCH))
            for identifier in request.source_ids
        ]
        hits: list[SearchHit] = []
        more = False
        for identifier, adapter in adapters:

            def read_page(source: JobSourceAdapter = adapter) -> SourcePage:
                return source.search(
                    request.query,
                    request.limit,
                    timeout_seconds=remaining_timeout(self._timeout),
                )

            page = _parse(SourcePage, _source_call(read_page, self._retry), source=True)
            if len(page.records) > request.limit:
                raise SourceDataError()
            consume("retrieved_jobs", len(page.records))
            for record in page.records:
                _bounded(record, MAX_RECORD_BYTES)
                hits.append(SearchHit(source_id=identifier, record=record))
            more = more or page.more_available
        return _bounded(
            SearchBatch(
                hits=tuple(hits),
                searched_sources=request.source_ids,
                limit=request.limit,
                more_available=more,
            )
        )

    def normalize_candidates(self, raw: object) -> SearchBatch:
        batch = _parse(SearchBatch, raw, source=True)
        for hit in batch.hits:
            self._source(hit.source_id, Permission.SEARCH)
            # Reject bad definition IDs/categories; missing definitions stay explicitly unknown.
            try:
                normalize_requirements(hit.record.job, hit.record.definitions, self._rules)
            except (CriteriaError, ValidationError):
                raise SourceDataError() from None
        return _bounded(batch)

    def deduplicate_candidates(self, raw: object) -> DeduplicatedJobs:
        batch = self.normalize_candidates(raw)
        return _bounded(deduplicate(batch))

    def search_jobs(self, raw: object) -> SearchResults:
        groups = self.deduplicate_candidates(self.search_candidates(raw))
        return _bounded(
            SearchResults(
                jobs=tuple(_summary(group) for group in groups.groups),
                duplicates_removed=groups.duplicates_removed,
                more_available=groups.more_available,
                searched_sources=groups.searched_sources,
            )
        )

    def _get_record(self, request: JobReference) -> JobRecord:
        adapter = self._source(request.source_id, Permission.READ_JOB)
        record = _parse(
            JobRecord,
            _source_call(
                lambda: adapter.get(
                    request.job_id, timeout_seconds=remaining_timeout(self._timeout)
                ),
                self._retry,
            ),
            source=True,
        )
        consume("retrieved_jobs")
        if record.job.id != request.job_id:
            raise SourceDataError()
        return _bounded(record, MAX_RECORD_BYTES)

    def retrieve_job(self, raw: object) -> RetrievedJob:
        request = _parse(JobReference, raw)
        record = self._get_record(request)
        return _bounded(
            RetrievedJob(
                source_id=request.source_id,
                job=record.job,
                canonical_url=record.job.source.canonical_url,
            )
        )

    def normalize_jobs(self, raw: object) -> SearchResults:
        request = _parse(NormalizeJobsInput, raw)
        self._grant.require(Permission.SEARCH)
        # Fetch by authorized IDs, not model-supplied job bodies.
        for reference in request.references:
            self._source(reference.source_id, Permission.READ_JOB)
        batch = SearchBatch(
            hits=tuple(
                SearchHit(source_id=reference.source_id, record=self._get_record(reference))
                for reference in request.references
            ),
            searched_sources=tuple(
                dict.fromkeys(reference.source_id for reference in request.references)
            ),
            limit=request.limit,
        )
        groups = self.deduplicate_candidates(batch)
        return _bounded(
            SearchResults(
                jobs=tuple(_summary(group) for group in groups.groups),
                duplicates_removed=groups.duplicates_removed,
                more_available=groups.more_available,
                searched_sources=groups.searched_sources,
            )
        )

    def candidate_facts(self, raw: object) -> CandidateFacts:
        consume("tool_calls")
        request = _parse(CandidateFactsInput, raw)
        self._grant.require(Permission.READ_CANDIDATE)
        self._grant.candidate(request.candidate_id)
        try:
            with self._transaction() as repository:
                profile = repository.get(CandidateProfile, request.candidate_id)
        except NotFound:
            raise ResourceNotFound() from None
        except ExecutionFailure:
            raise
        except Exception:
            raise PersistenceFailure() from None
        fields: dict[str, object] = {}
        omitted: list[FieldOmission] = []
        for name in request.fields:
            value = getattr(profile, name.value)
            if name == FactField.WORK_AUTHORIZED:
                fields[name.value] = value
            else:
                fields[name.value] = value[: request.per_field_limit]
                if len(value) > request.per_field_limit:
                    omitted.append(
                        FieldOmission(field=name, count=len(value) - request.per_field_limit)
                    )
        return _bounded(
            CandidateFacts.model_validate(
                {
                    **fields,
                    "candidate_id": profile.id,
                    "requested_fields": request.fields,
                    "omitted": tuple(omitted),
                }
            )
        )

    def company_info(self, raw: object) -> CompanyInfo:
        request = _parse(CompanyInput, raw)
        self._grant.require(Permission.READ_COMPANY)
        if request.company_id not in self._grant.company_ids:
            raise AccessDenied()
        info = _parse(
            CompanyInfo,
            _source_call(
                lambda: self._companies.get(
                    request.company_id, timeout_seconds=remaining_timeout(self._timeout)
                ),
                self._retry,
            ),
            source=True,
        )
        if info.company_id != request.company_id:
            raise SourceDataError()
        return _bounded(info)

    def assess(self, raw_request: object, raw_jobs: object) -> DiscoverySuccess:
        request = self.authorize_discovery(raw_request)
        jobs = _parse(DeduplicatedJobs, raw_jobs, source=True)
        for group in jobs.groups:
            selected = group.selected
            record = self._get_record(
                JobReference(source_id=selected.source_id, job_id=selected.record.job.id)
            )
            if record != selected.record:
                raise SourceDataError()
        reports: list[AssessedJob] = []
        try:
            # Whole bounded persistence+assessment operation commits or rolls back together.
            with self._transaction() as repository:
                for group in jobs.groups:
                    record = group.selected.record
                    matches = repository.find_duplicate_jobs(record.job.source)
                    if any(existing != record.job for existing in matches):
                        raise DiscoveryConflict()
                    try:
                        existing = repository.get(JobPosting, record.job.id)
                    except NotFound:
                        repository.add(record.job)
                    else:
                        if existing != record.job:
                            raise DiscoveryConflict()
                    service = AnalysisService(
                        lambda: nullcontext(repository),
                        {record.job.id: record.definitions},
                        self._rules,
                    )
                    analysis_request = AnalysisRequest(
                        id=uuid5(request.id, str(record.job.id)),
                        created_at=request.created_at,
                        job_id=record.job.id,
                        candidate_id=request.candidate_id,
                    )
                    criteria = service.normalize(record.job.id)
                    baseline = service.calculate(analysis_request, criteria)
                    assessment_id = service.validate_and_store(analysis_request, criteria, baseline)
                    report = service.result(analysis_request, criteria, baseline, assessment_id)
                    reports.append(AssessedJob(job=_summary(group), assessment=report))
                result = _bounded(
                    DiscoverySuccess(
                        status="completed" if reports else "empty",
                        jobs=tuple(reports),
                        duplicates_removed=jobs.duplicates_removed,
                        more_available=jobs.more_available,
                        searched_sources=jobs.searched_sources,
                    )
                )
            return result
        except DiscoveryError:
            raise
        except IntegrityViolation:
            raise DiscoveryConflict() from None
        except (CriteriaError, ValidationError):
            raise SourceDataError() from None
        except ExecutionFailure:
            raise
        except Exception:
            raise PersistenceFailure() from None
