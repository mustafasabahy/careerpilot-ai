"""File-backed roundtrips and integrity, using synthetic data only."""

import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_domain import synthetic_job, synthetic_profile

from careerpilot.application.storage import (
    CareerRepository,
    DuplicateJob,
    IntegrityViolation,
    NotFound,
)
from careerpilot.domain.applications import (
    ApplicationDocument,
    ApplicationStatus,
    DocumentKind,
    DocumentVersion,
    JobApplication,
)
from careerpilot.domain.common import utc_now
from careerpilot.domain.governance import ApprovalRequest, ApprovalStatus, AuditEvent
from careerpilot.domain.jobs import (
    JobFitAssessment,
    JobPosting,
    JobSource,
    RequirementOutcome,
    RequirementResult,
)
from careerpilot.domain.profile import CandidateProfile
from careerpilot.persistence.sqlite import SQLiteDatabase


@pytest.fixture
def database(tmp_path: Path) -> SQLiteDatabase:
    db = SQLiteDatabase(tmp_path / "synthetic.sqlite3")
    db.initialize()
    return db


@pytest.fixture
def repository(database: SQLiteDatabase) -> Iterator[CareerRepository]:
    with database.transaction() as repository:
        yield repository


def seed(
    repository: CareerRepository,
) -> tuple[CandidateProfile, JobPosting, DocumentVersion, JobApplication]:
    profile, job = synthetic_profile(), synthetic_job()
    document = ApplicationDocument(candidate_id=profile.id, kind=DocumentKind.RESUME)
    version = DocumentVersion(document_id=document.id, number=1, content="Synthetic resume")
    application = JobApplication(
        candidate_id=profile.id, job_id=job.id, document_version_id=version.id
    )
    for entity in (profile, job, document, version, application):
        repository.add(entity)
    return profile, job, version, application


def test_file_roundtrips_all_aggregates_across_connections(database: SQLiteDatabase) -> None:
    with database.transaction() as repository:
        profile, job, version, application = seed(repository)
        assessment = JobFitAssessment(candidate_id=profile.id, job_id=job.id)
        approval = ApprovalRequest(
            application_id=application.id,
            document_version_id=version.id,
            expires_at=utc_now() + timedelta(days=1),
        )
        repository.add(assessment)
        repository.add(approval)
        audit = repository.audit_history(application.id)[0]
    # A new database instance/connection proves persistence beyond in-memory object identity.
    reopened = SQLiteDatabase(database.path)
    reopened.initialize()
    with reopened.transaction() as repository:
        assert repository.get(CandidateProfile, profile.id) == profile
        assert repository.get(JobPosting, job.id) == job
        assert repository.get(DocumentVersion, version.id) == version
        assert repository.get(ApplicationDocument, version.document_id).candidate_id == profile.id
        assert repository.get(JobApplication, application.id) == application
        assert repository.get(JobFitAssessment, assessment.id) == assessment
        assert repository.get(ApprovalRequest, approval.id) == approval
        assert repository.get(AuditEvent, audit.id) == audit


def test_transaction_rolls_back_successful_prior_inserts(database: SQLiteDatabase) -> None:
    profile = synthetic_profile()
    with pytest.raises(IntegrityViolation):
        with database.transaction() as repository:
            repository.add(profile)
            repository.add(ApplicationDocument(candidate_id=uuid4(), kind=DocumentKind.RESUME))
    with database.transaction() as repository:
        with pytest.raises(NotFound):
            repository.get(CandidateProfile, profile.id)


def test_non_database_error_rolls_back(database: SQLiteDatabase) -> None:
    profile = synthetic_profile()
    with pytest.raises(RuntimeError):
        with database.transaction() as repository:
            repository.add(profile)
            raise RuntimeError("Synthetic failure")
    with database.transaction() as repository:
        with pytest.raises(NotFound):
            repository.get(CandidateProfile, profile.id)


@pytest.mark.parametrize(
    "source",
    [
        JobSource(name="EXAMPLE BOARD", external_id="job-001"),
        JobSource(name="Other Board", url="https://EXAMPLE.test/jobs/1?utm_medium=fixture#other"),
    ],
)
def test_duplicate_job_detection(repository: CareerRepository, source: JobSource) -> None:
    job = synthetic_job()
    repository.add(job)
    assert repository.find_duplicate_jobs(source) == (job,)
    with pytest.raises(DuplicateJob):
        repository.add(JobPosting(title="Duplicate", employer="Example", source=source))


def test_no_false_dedup_without_identifiers_and_preserve_semantic_queries(
    repository: CareerRepository,
) -> None:
    for _ in range(2):
        repository.add(
            JobPosting(title="Manual", employer="Example", source=JobSource(name="manual"))
        )
    assert repository.find_duplicate_jobs(JobSource(name="manual")) == ()
    job = JobPosting(
        title="One",
        employer="Example",
        source=JobSource(name="manual", url="https://example.test/job?id=1"),
    )
    repository.add(job)
    assert (
        repository.find_duplicate_jobs(
            JobSource(name="manual", url="https://example.test/job?id=2")
        )
        == ()
    )
    assert repository.find_duplicate_jobs(JobSource(name="other", external_id="job-001")) == ()


def test_dedup_can_report_conflicting_source_and_url_matches(repository: CareerRepository) -> None:
    first = synthetic_job()
    second = JobPosting(
        title="Other",
        employer="Example",
        source=JobSource(name="other", external_id="other", url="https://example.test/jobs/2"),
    )
    repository.add(first)
    repository.add(second)
    source = JobSource(
        name=first.source.name, external_id=first.source.external_id, url=second.source.url
    )
    assert {job.id for job in repository.find_duplicate_jobs(source)} == {first.id, second.id}
    with pytest.raises(DuplicateJob):
        repository.add(JobPosting(title="Conflict", employer="Example", source=source))


def test_valid_version_lineage_and_uniqueness(repository: CareerRepository) -> None:
    _, _, first, _ = seed(repository)
    second = DocumentVersion(
        document_id=first.document_id,
        number=2,
        previous_version_id=first.id,
        content="Synthetic revision",
    )
    repository.add(second)
    assert repository.get(DocumentVersion, second.id) == second
    with pytest.raises(IntegrityViolation):
        repository.add(
            DocumentVersion(
                document_id=first.document_id,
                number=2,
                previous_version_id=first.id,
                content="Synthetic duplicate",
            )
        )
    with pytest.raises(IntegrityViolation):
        repository.add(
            DocumentVersion(
                document_id=first.document_id,
                number=4,
                previous_version_id=second.id,
                content="Skipped version",
            )
        )
    other_doc = ApplicationDocument(candidate_id=uuid4(), kind=DocumentKind.RESUME)
    with pytest.raises(IntegrityViolation):
        repository.add(other_doc)
    profile = synthetic_profile()
    repository.add(profile)
    other_doc = ApplicationDocument(candidate_id=profile.id, kind=DocumentKind.RESUME)
    repository.add(other_doc)
    with pytest.raises(IntegrityViolation):
        repository.add(
            DocumentVersion(
                document_id=other_doc.id,
                number=2,
                previous_version_id=first.id,
                content="Wrong document",
            )
        )


def test_document_ownership_is_enforced(repository: CareerRepository) -> None:
    _, _, version, application = seed(repository)
    other = synthetic_profile()
    repository.add(other)
    other_job = JobPosting(title="Other", employer="Example", source=JobSource(name="manual"))
    repository.add(other_job)
    with pytest.raises(IntegrityViolation):
        repository.add(
            JobApplication(
                candidate_id=other.id, job_id=other_job.id, document_version_id=version.id
            )
        )
    other_doc = ApplicationDocument(candidate_id=other.id, kind=DocumentKind.RESUME)
    repository.add(other_doc)
    other_version = DocumentVersion(document_id=other_doc.id, number=1, content="Synthetic other")
    repository.add(other_version)
    with pytest.raises(IntegrityViolation):
        repository.attach_document(application.id, other_version.id)
    assert repository.get(JobApplication, application.id).document_version_id == version.id


def test_attach_transition_approval_and_audit(repository: CareerRepository) -> None:
    profile, job, version, application = seed(repository)
    with pytest.raises(IntegrityViolation):
        repository.add(JobApplication(candidate_id=profile.id, job_id=job.id))
    second_job = JobPosting(title="Other", employer="Example", source=JobSource(name="manual"))
    repository.add(second_job)
    draft = JobApplication(candidate_id=profile.id, job_id=second_job.id)
    repository.add(draft)
    repository.attach_document(draft.id, version.id)
    repository.transition_application(draft.id, ApplicationStatus.READY)
    request = ApprovalRequest(
        application_id=draft.id,
        document_version_id=version.id,
        expires_at=utc_now() + timedelta(days=1),
    )
    repository.add(request)
    assert repository.decide_approval(request.id, approved=True).status == ApprovalStatus.APPROVED
    repository.transition_application(draft.id, ApplicationStatus.SUBMITTED)
    history = repository.audit_history(draft.id)
    assert [event.action for event in history] == [
        "application_created",
        "document_attached",
        "status_changed",
        "approval_requested",
        "approval_decided",
        "status_changed",
    ]
    assert history[-1].from_status == ApplicationStatus.READY
    assert history[-1].to_status == ApplicationStatus.SUBMITTED
    with pytest.raises(ValueError):
        repository.transition_application(draft.id, ApplicationStatus.DRAFT)
    with pytest.raises(ValueError):
        repository.decide_approval(request.id, approved=False)


def test_approval_wrong_or_superseded_version(repository: CareerRepository) -> None:
    _, _, first, application = seed(repository)
    second = DocumentVersion(
        document_id=first.document_id,
        number=2,
        previous_version_id=first.id,
        content="Synthetic revision",
    )
    repository.add(second)
    with pytest.raises(IntegrityViolation):
        repository.add(
            ApprovalRequest(
                application_id=application.id,
                document_version_id=second.id,
                expires_at=utc_now() + timedelta(days=1),
            )
        )
    request = ApprovalRequest(
        application_id=application.id,
        document_version_id=first.id,
        expires_at=utc_now() + timedelta(days=1),
    )
    repository.add(request)
    repository.attach_document(application.id, second.id)
    with pytest.raises(IntegrityViolation):
        repository.decide_approval(request.id, approved=True)
    assert repository.get(ApprovalRequest, request.id).document_version_id == first.id
    assert repository.get(ApprovalRequest, request.id).status == ApprovalStatus.PENDING


def test_invalid_references(repository: CareerRepository) -> None:
    profile, job, _, application = seed(repository)
    with pytest.raises(NotFound):
        repository.get(JobPosting, uuid4())
    with pytest.raises(IntegrityViolation):
        repository.add(JobApplication(candidate_id=profile.id, job_id=uuid4()))
    with pytest.raises(IntegrityViolation):
        repository.add(JobApplication(candidate_id=uuid4(), job_id=job.id))
    with pytest.raises(NotFound):
        repository.add(DocumentVersion(document_id=uuid4(), number=1, content="Synthetic"))
    with pytest.raises(NotFound):
        repository.add(
            ApprovalRequest(
                application_id=uuid4(),
                document_version_id=uuid4(),
                expires_at=utc_now() + timedelta(days=1),
            )
        )
    with pytest.raises(IntegrityViolation):
        repository.add(
            AuditEvent(application_id=uuid4(), actor="system", action="application_created")
        )
    with pytest.raises(ValueError):
        repository.add(
            JobFitAssessment(
                candidate_id=profile.id,
                job_id=job.id,
                results=(
                    RequirementResult(requirement_id=uuid4(), outcome=RequirementOutcome.UNKNOWN),
                ),
            )
        )
    assert len(repository.audit_history(application.id)) == 1


def test_cannot_insert_application_or_approval_already_transitioned(
    repository: CareerRepository,
) -> None:
    profile, job, version, application = seed(repository)
    with pytest.raises(IntegrityViolation):
        repository.add(
            JobApplication(
                candidate_id=profile.id,
                job_id=job.id,
                document_version_id=version.id,
                status=ApplicationStatus.SUBMITTED,
            )
        )
    pending = ApprovalRequest(
        application_id=application.id,
        document_version_id=version.id,
        expires_at=utc_now() + timedelta(days=1),
    )
    with pytest.raises(IntegrityViolation):
        repository.add(pending.decide(True))


def test_database_constraints_apply_even_to_raw_inserts(database: SQLiteDatabase) -> None:
    with database.transaction() as repository:
        profile, job, version, _ = seed(repository)
    with sqlite3.connect(database.path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO documents(id,candidate_id,payload) VALUES (?,?,?)",
                (str(uuid4()), str(uuid4()), "{}"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO jobs(id,source_name,external_id,payload) VALUES (?,?,?,?)",
                (str(uuid4()), job.source.name, job.source.external_id, "{}"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO versions(id,document_id,candidate_id,number,payload) "
                "VALUES (?,?,?,?,?)",
                (str(uuid4()), str(version.document_id), str(profile.id), 1, "{}"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO jobs(id,source_name,canonical_url,payload) VALUES (?,?,?,?)",
                (str(uuid4()), "other", job.source.canonical_url, "{}"),
            )


def test_audit_failure_does_not_commit_partial_operation(database: SQLiteDatabase) -> None:
    with database.transaction() as repository:
        _, _, _, application = seed(repository)
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            "CREATE TRIGGER synthetic_audit_failure BEFORE INSERT ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
    # Deliberately catch inside the transaction; savepoint must still undo the update.
    with database.transaction() as repository:
        with pytest.raises(IntegrityViolation):
            repository.transition_application(application.id, ApplicationStatus.READY)
        assert repository.get(JobApplication, application.id).status == ApplicationStatus.DRAFT
        assert len(repository.audit_history(application.id)) == 1


def test_unsafe_pydantic_copy_is_revalidated(repository: CareerRepository) -> None:
    profile = synthetic_profile().model_copy(update={"created_at": "not-a-date"})
    with pytest.raises(ValidationError):
        repository.add(profile)


def test_unknown_database_versions_and_unversioned_data_are_preserved(tmp_path: Path) -> None:
    path = tmp_path / "existing.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE existing (value TEXT)")
        connection.execute("INSERT INTO existing VALUES ('synthetic')")
    db = SQLiteDatabase(path)
    with pytest.raises(IntegrityViolation):
        db.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM existing").fetchone() == ("synthetic",)
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(IntegrityViolation):
        db.initialize()
    with pytest.raises(IntegrityViolation):
        with db.transaction():
            pass
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
        assert connection.execute("SELECT value FROM existing").fetchone() == ("synthetic",)


def test_session_closes_and_requires_initialization(
    tmp_path: Path, database: SQLiteDatabase
) -> None:
    db = SQLiteDatabase(tmp_path / "not_initialized.sqlite3")
    with pytest.raises(IntegrityViolation):
        with db.transaction():
            pass
    with database.transaction() as repository:
        profile = synthetic_profile()
        repository.add(profile)
    with pytest.raises(sqlite3.ProgrammingError):
        repository.get(CandidateProfile, profile.id)


@pytest.mark.parametrize("approved", [True, False])
def test_approval_and_audit_roundtrip_after_decision(
    database: SQLiteDatabase, approved: bool
) -> None:
    with database.transaction() as repository:
        _, _, version, application = seed(repository)
        request = ApprovalRequest(
            application_id=application.id,
            document_version_id=version.id,
            expires_at=utc_now() + timedelta(days=1),
        )
        repository.add(request)
        decided = repository.decide_approval(request.id, approved=approved)
    with database.transaction() as repository:
        assert repository.get(ApprovalRequest, request.id) == decided
        event = repository.audit_history(application.id)[-1]
        assert event.from_status == ApprovalStatus.PENDING
        assert event.to_status == decided.status
        assert repository.get(AuditEvent, event.id) == event


def test_missing_and_unknown_roundtrip_in_database(database: SQLiteDatabase) -> None:
    from careerpilot.domain.common import Claim, KnowledgeState

    missing = CandidateProfile()
    unknown = CandidateProfile.model_validate(
        {"work_authorized": Claim[bool](state=KnowledgeState.UNKNOWN)}
    )
    with database.transaction() as repository:
        repository.add(missing)
        repository.add(unknown)
    with database.transaction() as repository:
        assert (
            repository.get(CandidateProfile, missing.id).work_authorized.state
            == KnowledgeState.MISSING
        )
        assert (
            repository.get(CandidateProfile, unknown.id).work_authorized.state
            == KnowledgeState.UNKNOWN
        )


def test_failed_audit_rolls_back_application_insert_even_if_caught(
    database: SQLiteDatabase,
) -> None:
    with database.transaction() as repository:
        profile, job = synthetic_profile(), synthetic_job()
        repository.add(profile)
        repository.add(job)
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            "CREATE TRIGGER synthetic_audit_failure BEFORE INSERT ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
    application = JobApplication(candidate_id=profile.id, job_id=job.id)
    with database.transaction() as repository:
        with pytest.raises(IntegrityViolation):
            repository.add(application)
        with pytest.raises(NotFound):
            repository.get(JobApplication, application.id)


def test_assessment_requires_existing_candidate_and_job(repository: CareerRepository) -> None:
    profile, job, _, _ = seed(repository)
    with pytest.raises(IntegrityViolation):
        repository.add(JobFitAssessment(candidate_id=uuid4(), job_id=job.id))
    with pytest.raises(NotFound):
        repository.add(JobFitAssessment(candidate_id=profile.id, job_id=uuid4()))


def test_reopened_adapter_still_enforces_foreign_keys(database: SQLiteDatabase) -> None:
    reopened = SQLiteDatabase(database.path)
    for db in (database, reopened):
        with db.transaction() as repository:
            with pytest.raises(IntegrityViolation):
                repository.add(ApplicationDocument(candidate_id=uuid4(), kind=DocumentKind.RESUME))
