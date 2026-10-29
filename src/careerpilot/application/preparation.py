"""Evidence-first preparation: validate, plan outside transactions, then persist atomically."""

import hashlib
import json
from collections.abc import Mapping
from difflib import unified_diff
from uuid import UUID, uuid5

from pydantic import ValidationError

from careerpilot.application.analysis import TransactionFactory
from careerpilot.application.discovery_contracts import AccessDenied, Permission
from careerpilot.application.draft_generation import planning_input, render_drafts, validate_plan
from careerpilot.application.preparation_contracts import (
    DraftPlanner,
    InvalidPreparationRequest,
    PreparationAccessDenied,
    PreparationAuthorization,
    PreparationConflict,
    PreparationRequest,
    PreparationSuccess,
    PreparationValidationError,
    ReviewableDraft,
)
from careerpilot.application.storage import CareerRepository, NotFound
from careerpilot.domain.applications import ApplicationDocument, DocumentKind, DocumentVersion
from careerpilot.domain.baseline import (
    BaselineRules,
    CriterionDefinition,
    calculate_baseline,
    normalize_requirements,
)
from careerpilot.domain.career_intelligence import build_intelligence
from careerpilot.domain.common import Model
from careerpilot.domain.jobs import JobFitAssessment, JobPosting
from careerpilot.domain.preparation import (
    CareerIntelligence,
    DraftMetadata,
    DraftPlan,
    FactKind,
    PreparationRecord,
)
from careerpilot.domain.profile import CandidateProfile


class PreparationContext(Model):
    request: PreparationRequest
    intelligence: CareerIntelligence
    request_sha256: str
    existing_record_id: UUID | None = None


class PreparationService:
    def __init__(
        self,
        authorization: PreparationAuthorization,
        transaction: TransactionFactory,
        definitions: Mapping[UUID, tuple[CriterionDefinition, ...]],
        rules: BaselineRules,
        planner: DraftPlanner,
    ) -> None:
        self._authorization = authorization
        self._transaction = transaction
        self._definitions = dict(definitions)
        self._rules = rules
        self._planner = planner

    def _authorize(self, request: PreparationRequest) -> None:
        try:
            self._authorization.access.require(Permission.READ_CANDIDATE)
            self._authorization.access.require(Permission.READ_JOB)
            self._authorization.access.candidate(request.candidate_id)
            if request.job_id not in self._authorization.job_ids:
                raise AccessDenied()
        except AccessDenied:
            raise PreparationAccessDenied(
                "Preparation is outside the bound candidate/job scope"
            ) from None

    def inspect(self, raw: object) -> PreparationContext:
        try:
            if isinstance(raw, PreparationRequest):
                raw = raw.model_dump(warnings=False)
            request = PreparationRequest.model_validate(raw)
        except ValidationError:
            raise InvalidPreparationRequest("Invalid preparation request") from None
        self._authorize(request)
        with self._transaction() as repository:
            profile = repository.get(CandidateProfile, request.candidate_id)
            job = repository.get(JobPosting, request.job_id)
            stored = repository.get(JobFitAssessment, request.assessment_id)
            if len(job.requirements) > 20:
                raise PreparationValidationError("Preparation supports at most 20 requirements")
            if (stored.candidate_id, stored.job_id) != (profile.id, job.id):
                raise PreparationValidationError("Assessment belongs to a different candidate/job")
            criteria = normalize_requirements(job, self._definitions.get(job.id, ()), self._rules)
            recomputed = calculate_baseline(job, profile, criteria, self._rules, stored)
            if recomputed.assessment != stored:
                raise PreparationValidationError(
                    "Assessment cannot be reproduced with the supplied rule context"
                )
            for document_id, kind in (
                (request.resume_document_id, DocumentKind.RESUME),
                (request.cover_document_id, DocumentKind.COVER_LETTER),
            ):
                if document_id is not None:
                    document = repository.get(ApplicationDocument, document_id)
                    if document.candidate_id != profile.id or document.kind != kind:
                        raise PreparationAccessDenied(
                            "Document ownership/kind does not match this preparation"
                        )
                    latest = repository.latest_document_version(document_id)
                    if latest is not None and latest.created_at > request.created_at:
                        raise PreparationValidationError(
                            "Preparation time precedes the latest document version"
                        )
            if request.original_resume_version_id is not None:
                original = repository.get(DocumentVersion, request.original_resume_version_id)
                if original.document_id != request.resume_document_id:
                    raise PreparationValidationError(
                        "Original CV must belong to the selected resume document"
                    )
            intelligence = build_intelligence(job, profile, recomputed, criteria)
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "request": request.model_dump(mode="json"),
                        "intelligence": intelligence.model_dump(mode="json"),
                        "planner": [self._planner.provider, self._planner.model],
                        "renderer": "fact-render-v1",
                        "planner_prompt": "fact-selection-v1",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            try:
                existing = repository.get(PreparationRecord, request.id)
            except NotFound:
                existing = None
            if existing is not None and existing.request_sha256 != fingerprint:
                raise PreparationConflict(
                    "Preparation ID already refers to different inputs or policy"
                )
        return PreparationContext(
            request=request,
            intelligence=intelligence,
            request_sha256=fingerprint,
            existing_record_id=existing.id if existing else None,
        )

    def generate(self, context: PreparationContext) -> object:
        if not self._authorization.allow_drafts:
            raise PreparationAccessDenied("Document generation is not authorized")
        fresh = self.inspect(context.request)
        if fresh != context:
            raise PreparationConflict("Source context changed before generation")
        if context.intelligence.blocking_reasons:
            raise PreparationValidationError("Resolve blocking clarification questions first")
        if self._planner.provider != "mock" and not self._authorization.allow_profile_model_export:
            raise PreparationAccessDenied(
                "Professional fact export to a real planner requires explicit authorization"
            )
        return self._planner.plan(planning_input(context.intelligence))

    def validate(self, context: PreparationContext, raw: object) -> DraftPlan:
        fresh = self.inspect(context.request)
        if (
            fresh.request_sha256 != context.request_sha256
            or fresh.intelligence != context.intelligence
        ):
            raise PreparationConflict("Source data changed before draft validation")
        return validate_plan(raw, planning_input(context.intelligence))

    def _version(
        self,
        repository: CareerRepository,
        request: PreparationRequest,
        kind: DocumentKind,
        document_id: UUID | None,
        content: str,
    ) -> DocumentVersion:
        if document_id is None:
            document_id = uuid5(request.id, f"document:{kind.value}")
            repository.add(
                ApplicationDocument(
                    id=document_id,
                    created_at=request.created_at,
                    candidate_id=request.candidate_id,
                    kind=kind,
                )
            )
        document = repository.get(ApplicationDocument, document_id)
        if document.candidate_id != request.candidate_id or document.kind != kind:
            raise PreparationAccessDenied("Document ownership/kind changed")
        previous = repository.latest_document_version(document_id)
        version = DocumentVersion(
            id=uuid5(request.id, f"version:{kind.value}"),
            created_at=request.created_at,
            document_id=document_id,
            number=previous.number + 1 if previous else 1,
            previous_version_id=previous.id if previous else None,
            content=content,
            generated_by_ai=self._planner.provider != "mock",
        )
        repository.add(version)
        return version

    def persist(self, context: PreparationContext, raw_plan: object) -> PreparationSuccess:
        if not self._authorization.allow_drafts:
            raise PreparationAccessDenied("Document generation is not authorized")
        if context.intelligence.blocking_reasons:
            raise PreparationValidationError("Resolve blocking questions before persisting drafts")
        plan = self.validate(context, raw_plan)
        request = context.request
        with self._transaction() as repository:
            try:
                existing = repository.get(PreparationRecord, request.id)
            except NotFound:
                existing = None
            if existing is not None:
                if existing.request_sha256 != context.request_sha256:
                    raise PreparationConflict("Preparation ID conflicts with persisted inputs")
                return self._read_result(repository, existing)
            job = repository.get(JobPosting, request.job_id)
            resume, cover = render_drafts(job, context.intelligence, plan)
            resume_version = self._version(
                repository, request, DocumentKind.RESUME, request.resume_document_id, resume
            )
            cover_version = self._version(
                repository, request, DocumentKind.COVER_LETTER, request.cover_document_id, cover
            )
            name_id = next(
                item.id
                for item in context.intelligence.facts
                if item.kind == FactKind.NAME and item.verification == "user_confirmed"
            )
            metadata = tuple(
                DraftMetadata(
                    kind=kind,
                    document_id=version.document_id,
                    version_id=version.id,
                    previous_version_id=version.previous_version_id,
                    fact_ids=(*fact_ids, name_id),
                    content_sha256=hashlib.sha256(version.content.encode()).hexdigest(),
                )
                for kind, version, fact_ids in (
                    (DocumentKind.RESUME, resume_version, plan.resume_fact_ids),
                    (DocumentKind.COVER_LETTER, cover_version, plan.cover_fact_ids),
                )
            )
            record = PreparationRecord(
                id=request.id,
                created_at=request.created_at,
                candidate_id=request.candidate_id,
                job_id=request.job_id,
                assessment_id=request.assessment_id,
                request_sha256=context.request_sha256,
                original_resume_version_id=request.original_resume_version_id
                or resume_version.previous_version_id,
                planner_provider=self._planner.provider,
                planner_model=self._planner.model,
                intelligence=context.intelligence,
                documents=metadata,
            )
            repository.add(record)
            return self._read_result(repository, record)

    def replay(self, identifier: UUID) -> PreparationSuccess:
        with self._transaction() as repository:
            record = repository.get(PreparationRecord, identifier)
            # Replay still checks caller ownership; a known UUID grants no rights.
            self._authorize(
                PreparationRequest(
                    id=record.id,
                    created_at=record.created_at,
                    candidate_id=record.candidate_id,
                    job_id=record.job_id,
                    assessment_id=record.assessment_id,
                )
            )
            return self._read_result(repository, record)

    @staticmethod
    def _read_result(repository: CareerRepository, record: PreparationRecord) -> PreparationSuccess:
        drafts: list[ReviewableDraft] = []
        for metadata in record.documents:
            version = repository.get(DocumentVersion, metadata.version_id)
            previous = (
                repository.get(DocumentVersion, metadata.previous_version_id)
                if metadata.previous_version_id
                else None
            )
            if hashlib.sha256(version.content.encode()).hexdigest() != metadata.content_sha256:
                raise PreparationValidationError(
                    "Stored document hash does not match its reviewed metadata"
                )
            diff = "\n".join(
                unified_diff(
                    previous.content.splitlines() if previous else [],
                    version.content.splitlines(),
                    fromfile=f"version-{previous.number}" if previous else "new-document",
                    tofile=f"version-{version.number}",
                    lineterm="",
                )
            )
            drafts.append(
                ReviewableDraft.model_validate(
                    {
                        "version_id": version.id,
                        "kind": metadata.kind.value,
                        "content": version.content,
                        "changes": diff,
                    }
                )
            )
        return PreparationSuccess(record=record, drafts=tuple(drafts))
