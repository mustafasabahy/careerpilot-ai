"""Local composition facade; HTTP/UI never calculate fit or bypass approval rules."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from careerpilot.application.action_contracts import ActionPrincipal
from careerpilot.application.actions import ActionService
from careerpilot.application.analysis import AnalysisRequest, AnalysisResult, AnalysisService
from careerpilot.application.discovery_contracts import AccessGrant, JobRecord, Permission
from careerpilot.application.memory import CareerMemory, MemoryPrincipal
from careerpilot.application.preparation import PreparationService
from careerpilot.application.preparation_contracts import (
    PreparationAuthorization,
    PreparationRequest,
)
from careerpilot.application.security import ResourceKind
from careerpilot.application.storage import IntegrityViolation, NotFound, StoredT
from careerpilot.domain.actions import ActionContent, ConfiguredTarget, ProposalRequest
from careerpilot.domain.applications import (
    ApplicationDocument,
    DocumentKind,
    DocumentVersion,
    JobApplication,
)
from careerpilot.domain.baseline import normalize_requirements
from careerpilot.domain.common import utc_now
from careerpilot.domain.jobs import JobPosting
from careerpilot.domain.profile import CandidateProfile
from careerpilot.integrations.simulated_actions import SimulatedActionAdapter
from careerpilot.integrations.synthetic import synthetic_bundle
from careerpilot.persistence.actions_sqlite import SQLiteActionDatabase
from careerpilot.persistence.checkpoints import local_checkpointer
from careerpilot.persistence.memory_sqlite import SQLiteMemoryDatabase
from careerpilot.persistence.security import SecurityStore
from careerpilot.persistence.sqlite import SQLiteDatabase
from careerpilot.providers.draft_planner import DeterministicDraftPlanner
from careerpilot.workflows.action_approval import ActionApprovalWorkflow
from careerpilot.workflows.job_analysis import JobAnalysisWorkflow
from careerpilot.workflows.preparation import PreparationResult, PreparationWorkflow


class Portal:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.database = SQLiteDatabase(root / "business.sqlite3")
        if not self.database.path.exists():
            raise ValueError("Initialize local storage with careerpilot-server --init first")
        with self.database.transaction():
            pass
        self.security = SecurityStore(root / "security.sqlite3")
        self.memory_database = SQLiteMemoryDatabase(root / "memory.sqlite3")
        self.memory_database.initialize()
        self.jobs_path = root / "jobs"
        self.jobs_path.mkdir(exist_ok=True)
        self.target = ConfiguredTarget(
            id=uuid5(UUID(int=1), "portal-recruiter"),
            kind="recruiter_message",
            label="Simulated recruiter",
            route_key="recruiter@example.invalid",
            revision=1,
        )
        self.destination = ConfiguredTarget(
            id=uuid5(UUID(int=1), "portal-destination"),
            kind="submit_application",
            label="Simulated application inbox",
            route_key="fixture-inbox",
            revision=1,
        )

    @staticmethod
    def initialize(root: Path) -> None:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        SQLiteDatabase(root / "business.sqlite3").initialize()
        from careerpilot.persistence.conversations import ConversationDatabase

        ConversationDatabase(root / "conversations.sqlite3").initialize()

    def get(
        self, owner: UUID, kind: ResourceKind, model: type[StoredT], identifier: UUID
    ) -> StoredT:
        self.security.require(owner, kind, identifier)
        with self.database.transaction() as repository:
            return repository.get(model, identifier)

    def profile(self, owner: UUID, profile: CandidateProfile) -> CandidateProfile:
        # A new immutable snapshot; existing application/profile references are preserved.
        profile = CandidateProfile.model_validate({**profile.model_dump(), "id": uuid4()})
        self.security.bind(owner, "candidate", profile.id, profile.id)
        with self.database.transaction() as repository:
            repository.add(profile)
        return profile

    def import_job(self, owner: UUID, record: JobRecord) -> JobPosting:
        record = JobRecord.model_validate(record.model_dump())
        self.security.bind(owner, "job", record.job.id)
        normalize_requirements(record.job, record.definitions, synthetic_bundle().rules)
        # A definition file is published before the SQL transaction commits. A failed
        # file write rolls the job back; an orphan file alone grants no business access.
        with self.database.transaction() as repository:
            try:
                existing = repository.get(JobPosting, record.job.id)
            except NotFound:
                repository.add(record.job)
                temporary = self.jobs_path / f"{record.job.id}.{uuid4()}.tmp"
                temporary.write_text(record.model_dump_json())
                temporary.replace(self.jobs_path / f"{record.job.id}.json")
            else:
                path = self.jobs_path / f"{record.job.id}.json"
                if (
                    existing != record.job
                    or not path.exists()
                    or JobRecord.model_validate_json(path.read_bytes()) != record
                ):
                    raise IntegrityViolation("Imported identity or definition snapshot conflicts")
        return record.job

    def get_many(
        self, owner: UUID, kind: ResourceKind, model: type[StoredT], identifiers: tuple[UUID, ...]
    ) -> tuple[StoredT, ...]:
        result = []
        for identifier in identifiers:
            try:
                result.append(self.get(owner, kind, model, identifier))
            except NotFound:
                # A cross-file interrupted create can leave a fail-closed ownership
                # reservation. Never let that hide other authorized list entries.
                continue
        return tuple(result)

    def definitions(self, owner: UUID, job_id: UUID) -> JobRecord:
        self.security.require(owner, "job", job_id)
        record = JobRecord.model_validate_json((self.jobs_path / f"{job_id}.json").read_bytes())
        if record.job != self.get(owner, "job", JobPosting, job_id):
            raise ValueError("Registered job snapshot conflicts")
        return record

    def seed(self, owner: UUID) -> dict[str, object]:
        if self.security.list(owner, "candidate"):
            raise ValueError("Seed requires an empty synthetic account")
        bundle = synthetic_bundle()
        profile = self.profile(owner, bundle.profiles[0])
        jobs = []
        for job in bundle.jobs:
            data = job.model_dump()
            data["id"] = uuid5(owner, str(job.id))
            data["source"] = {
                **job.source.model_dump(),
                "name": f"fixture-{owner.hex}",
                "external_id": str(data["id"]),
                "url": None,
            }
            copied = JobPosting.model_validate(data)
            self.import_job(owner, JobRecord(job=copied, definitions=bundle.definitions[job.id]))
            jobs.append(copied.id)
        return {"candidate_id": profile.id, "job_ids": jobs}

    def analyze(self, owner: UUID, request: AnalysisRequest) -> AnalysisResult:
        self.security.require(owner, "candidate", request.candidate_id)
        record = self.definitions(owner, request.job_id)
        bundle = synthetic_bundle()
        result = JobAnalysisWorkflow(
            AnalysisService(
                self.database.transaction,
                {record.job.id: record.definitions},
                bundle.rules,
            )
        ).run(request)
        if hasattr(result, "assessment_id"):
            self.security.bind(owner, "assessment", result.assessment_id, request.candidate_id)
        return result

    def prepare(self, owner: UUID, request: PreparationRequest) -> PreparationResult:
        self.security.require(owner, "candidate", request.candidate_id)
        self.security.require(owner, "assessment", request.assessment_id)
        record = self.definitions(owner, request.job_id)
        for identifier in (request.resume_document_id, request.cover_document_id):
            if identifier:
                self.security.require(owner, "document", identifier)
        if request.original_resume_version_id:
            self.security.require(owner, "version", request.original_resume_version_id)
        grant = AccessGrant(
            owner,
            frozenset({Permission.READ_CANDIDATE, Permission.READ_JOB}),
            frozenset(),
            frozenset({request.candidate_id}),
            frozenset(),
        )
        authorization = PreparationAuthorization(
            grant, frozenset({request.job_id}), allow_drafts=True
        )
        result = PreparationWorkflow(
            PreparationService(
                authorization,
                self.database.transaction,
                {record.job.id: record.definitions},
                synthetic_bundle().rules,
                DeterministicDraftPlanner(),
            )
        ).run(request)
        if result.status == "drafts_created":
            self.security.bind(owner, "preparation", result.record.id, request.candidate_id)
            for document in result.record.documents:
                self.security.bind(owner, "document", document.document_id, request.candidate_id)
                self.security.bind(owner, "version", document.version_id, request.candidate_id)
        return result

    def application(
        self, owner: UUID, candidate: UUID, job: UUID, version: UUID | None
    ) -> JobApplication:
        self.security.require(owner, "candidate", candidate)
        self.security.require(owner, "job", job)
        if version:
            item = self.get(owner, "version", DocumentVersion, version)
            document = self.get(owner, "document", ApplicationDocument, item.document_id)
            if document.candidate_id != candidate or document.kind != DocumentKind.RESUME:
                raise ValueError("Select this candidate's resume")
        result = JobApplication(candidate_id=candidate, job_id=job, document_version_id=version)
        self.security.bind(owner, "application", result.id, candidate)
        with self.database.transaction() as repository:
            repository.add(result)
        return result

    def actions(self, owner: UUID, application_id: UUID) -> ActionService:
        application = self.get(owner, "application", JobApplication, application_id)
        self.security.require(owner, "candidate", application.candidate_id)
        return ActionService(
            ActionPrincipal(
                owner,
                owner,
                frozenset({application.candidate_id}),
                frozenset({"recruiter_message", "submit_application", "change_application"}),
                frozenset({self.target.id, self.destination.id}),
            ),
            SQLiteActionDatabase(self.database),
            (self.target, self.destination),
            SimulatedActionAdapter(self.root / "simulation.sqlite3"),
        )

    def thread_service(self, owner: UUID, thread_id: UUID) -> ActionService:
        self.security.require(owner, "thread", thread_id)
        with SQLiteActionDatabase(self.database).transaction() as repository:
            proposal = repository.thread_proposal(owner, thread_id)
        return self.actions(owner, proposal.application_id)

    @contextmanager
    def approval(self, service: ActionService) -> Iterator[ActionApprovalWorkflow]:
        with local_checkpointer(
            self.root / "checkpoints.sqlite3", separate_from=(self.database.path,)
        ) as saver:
            yield ActionApprovalWorkflow(service, saver)

    def propose(
        self, owner: UUID, application: UUID, content: ActionContent, previous: UUID | None = None
    ) -> object:
        service = self.actions(owner, application)
        app = self.get(owner, "application", JobApplication, application)
        request = ProposalRequest(
            id=uuid4(),
            thread_id=uuid4(),
            application_id=application,
            content=content,
            expires_at=utc_now() + timedelta(hours=1),
            previous_proposal_id=previous,
        )
        self.security.bind(owner, "thread", request.thread_id, app.candidate_id)
        with self.approval(service) as workflow:
            return workflow.prepare(request)

    def memory(self, owner: UUID) -> CareerMemory:
        return CareerMemory(MemoryPrincipal(owner), self.memory_database)
