"""Extraction entry point reusing the unchanged deterministic analysis graph."""

from typing import Literal
from uuid import UUID

from pydantic import TypeAdapter

from careerpilot.application.analysis import (
    AnalysisRequest,
    AnalysisResult,
    AnalysisService,
    TransactionFactory,
)
from careerpilot.application.execution import bounded_workflow
from careerpilot.application.extracted_jobs import prepare_job
from careerpilot.application.extraction import (
    ExtractionEnvelope,
    ExtractionRequest,
    ExtractionService,
)
from careerpilot.application.storage import IntegrityViolation, NotFound
from careerpilot.domain.baseline import BaselineRules
from careerpilot.domain.common import Model
from careerpilot.domain.jobs import JobPosting
from careerpilot.domain.profile import CandidateProfile
from careerpilot.workflows.job_analysis import JobAnalysisWorkflow


class ExtractedCareerResult(Model):
    status: Literal["requires_review"] = "requires_review"
    extraction: ExtractionEnvelope
    analysis: AnalysisResult | None
    review_reason: Literal["unreviewed_requirements", "missing_or_conflicting_job_identity"]


class ExtractedJobWorkflow:
    def __init__(
        self, extraction: ExtractionService, transaction: TransactionFactory, rules: BaselineRules
    ) -> None:
        self._extraction = extraction
        self._transaction = transaction
        self._rules = rules

    @bounded_workflow
    def run(
        self, document: ExtractionRequest, *, candidate_id: UUID, analysis_id: UUID
    ) -> ExtractedCareerResult:
        candidate_id = TypeAdapter(UUID).validate_python(candidate_id)
        analysis_id = TypeAdapter(UUID).validate_python(analysis_id)
        # Fail before paying for a model call if the candidate does not exist.
        with self._transaction() as repository:
            repository.get(CandidateProfile, candidate_id)
        envelope = self._extraction.extract(document)
        prepared = prepare_job(envelope)
        if prepared is None:
            return ExtractedCareerResult(
                extraction=envelope,
                analysis=None,
                review_reason="missing_or_conflicting_job_identity",
            )
        with self._transaction() as repository:
            try:
                existing = repository.get(JobPosting, prepared.job.id)
            except NotFound:
                repository.add(prepared.job)
            else:
                if existing != prepared.job:
                    raise IntegrityViolation("Extracted job snapshot conflicts with persisted data")
        service = AnalysisService(
            self._transaction, {prepared.job.id: prepared.definitions}, self._rules
        )
        analysis = JobAnalysisWorkflow(service).run(
            AnalysisRequest(
                id=analysis_id,
                created_at=document.created_at,
                job_id=prepared.job.id,
                candidate_id=candidate_id,
            )
        )
        return ExtractedCareerResult(
            extraction=envelope, analysis=analysis, review_reason="unreviewed_requirements"
        )
