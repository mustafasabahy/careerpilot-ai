"""Typed analysis boundary and use cases, independent of LangGraph and SQLite."""

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import Field, model_validator

from careerpilot.application.storage import CareerRepository, NotFound
from careerpilot.domain.baseline import (
    BaselineAssessment,
    BaselineCounts,
    BaselineRules,
    CriterionDefinition,
    NormalizedCriterion,
    RequirementFinding,
    calculate_baseline,
    normalize_requirements,
)
from careerpilot.domain.common import Entity, Model, UtcTimestamp
from careerpilot.domain.jobs import FitDecision, JobFitAssessment, JobPosting
from careerpilot.domain.profile import CandidateProfile


class AnalysisError(RuntimeError):
    """A workflow invariant failed; never return this as successful analysis."""


class AssessmentValidationError(AnalysisError):
    """Calculated or persisted data did not match deterministic expectations."""


class AssessmentConflict(AnalysisError):
    """An analysis ID was previously used for different inputs/policy."""


class AnalysisRequest(Entity):
    """Caller-supplied run namespace/time ensure reproducible identities and results."""

    id: UUID = Field(...)
    created_at: UtcTimestamp = Field(...)
    job_id: UUID
    candidate_id: UUID


class AnalysisFailure(Model):
    status: Literal["error"] = "error"
    code: Literal["invalid_request", "job_not_found", "profile_not_found"]


class AnalysisReport(Model):
    status: Literal["completed", "needs_information"]
    assessment_id: UUID
    job_id: UUID
    candidate_id: UUID
    decision: FitDecision
    rules: BaselineRules
    criteria: tuple[NormalizedCriterion, ...]
    findings: tuple[RequirementFinding, ...]
    counts: BaselineCounts

    @model_validator(mode="after")
    def coherent_status(self) -> "AnalysisReport":
        needs_information = not self.findings or any(
            item.outcome.value == "unknown" or item.importance.value == "unspecified"
            for item in self.findings
        )
        if (self.status == "needs_information") != needs_information:
            raise ValueError("Result status does not reflect missing information")
        return self


AnalysisResult = Annotated[AnalysisReport | AnalysisFailure, Field(discriminator="status")]
TransactionFactory = Callable[[], AbstractContextManager[CareerRepository]]


class AnalysisService:
    def __init__(
        self,
        transaction: TransactionFactory,
        definitions: Mapping[UUID, tuple[CriterionDefinition, ...]],
        rules: BaselineRules,
    ) -> None:
        self._transaction = transaction
        self._definitions = dict(definitions)
        self.rules = rules

    def _identity(
        self, request: AnalysisRequest, criteria: tuple[NormalizedCriterion, ...]
    ) -> Entity:
        material = json.dumps(
            {
                "request": request.model_dump(mode="json"),
                "rules": self.rules.model_dump(mode="json"),
                "criteria": [item.model_dump(mode="json") for item in criteria],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return Entity(id=uuid5(request.id, material), created_at=request.created_at)

    def load_job(self, identifier: UUID) -> UUID:
        with self._transaction() as repository:
            return repository.get(JobPosting, identifier).id

    def load_profile(self, identifier: UUID) -> UUID:
        with self._transaction() as repository:
            return repository.get(CandidateProfile, identifier).id

    def normalize(self, job_id: UUID) -> tuple[NormalizedCriterion, ...]:
        with self._transaction() as repository:
            return normalize_requirements(
                repository.get(JobPosting, job_id), self._definitions.get(job_id, ()), self.rules
            )

    def calculate(
        self, request: AnalysisRequest, criteria: tuple[NormalizedCriterion, ...]
    ) -> BaselineAssessment:
        with self._transaction() as repository:
            return calculate_baseline(
                repository.get(JobPosting, request.job_id),
                repository.get(CandidateProfile, request.candidate_id),
                criteria,
                self.rules,
                self._identity(request, criteria),
            )

    def validate_and_store(
        self,
        request: AnalysisRequest,
        criteria: tuple[NormalizedCriterion, ...],
        report: BaselineAssessment,
    ) -> UUID:
        """Validate complete output and only then persist, atomically/idempotently."""
        with self._transaction() as repository:
            job = repository.get(JobPosting, request.job_id)
            canonical = normalize_requirements(job, self._definitions.get(job.id, ()), self.rules)
            if criteria != canonical:
                raise AssessmentValidationError(
                    "Criteria differ from configured deterministic definitions"
                )
            expected = calculate_baseline(
                job,
                repository.get(CandidateProfile, request.candidate_id),
                canonical,
                self.rules,
                self._identity(request, criteria),
            )
            if report != expected:
                raise AssessmentValidationError("Assessment differs from deterministic baseline")
            report.assessment.decision(job)
            try:
                persisted = repository.get(JobFitAssessment, report.assessment.id)
            except NotFound:
                repository.add(report.assessment)
            else:
                if persisted != report.assessment:
                    raise AssessmentConflict(
                        "Analysis ID already belongs to a different assessment"
                    )
            return report.assessment.id

    def result(
        self,
        request: AnalysisRequest,
        criteria: tuple[NormalizedCriterion, ...],
        report: BaselineAssessment,
        assessment_id: UUID,
    ) -> AnalysisReport:
        with self._transaction() as repository:
            persisted = repository.get(JobFitAssessment, assessment_id)
            if persisted != report.assessment:
                raise AssessmentValidationError(
                    "Persisted assessment changed before result assembly"
                )
            decision = persisted.decision(repository.get(JobPosting, request.job_id))
        return AnalysisReport(
            status="needs_information" if report.needs_information else "completed",
            assessment_id=assessment_id,
            job_id=request.job_id,
            candidate_id=request.candidate_id,
            decision=decision,
            rules=report.rules,
            criteria=criteria,
            findings=report.findings,
            counts=report.counts,
        )
