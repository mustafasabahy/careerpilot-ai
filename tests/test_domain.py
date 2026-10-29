"""Synthetic domain examples and exhaustive application lifecycle edges."""

from datetime import UTC, date, datetime, timedelta, timezone
from itertools import product
from uuid import uuid4

import pytest
from pydantic import BaseModel, StrictBool, ValidationError

from careerpilot.domain.applications import (
    TRANSITIONS,
    ApplicationDocument,
    ApplicationStatus,
    DocumentKind,
    DocumentVersion,
    JobApplication,
)
from careerpilot.domain.common import Claim, Evidence, EvidenceOrigin, KnowledgeState
from careerpilot.domain.governance import ApprovalRequest, ApprovalStatus, AuditEvent
from careerpilot.domain.jobs import (
    FitDecision,
    JobFitAssessment,
    JobPosting,
    JobRequirement,
    JobSource,
    RequirementCategory,
    RequirementOutcome,
    RequirementResult,
)
from careerpilot.domain.profile import (
    CandidateProfile,
    Certification,
    Education,
    LanguageLevel,
    LanguageProficiency,
    Skill,
    SkillProficiency,
    WorkExperience,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
USER = Evidence(origin=EvidenceOrigin.USER_STATEMENT, reference="fixture:user:1", created_at=NOW)
AI = Evidence(origin=EvidenceOrigin.AI_INFERENCE, reference="fixture:inference:1", created_at=NOW)
SOURCE = Evidence(origin=EvidenceOrigin.SOURCE_DOCUMENT, reference="fixture:job:1", created_at=NOW)


def synthetic_profile() -> CandidateProfile:
    return CandidateProfile(
        display_name=Claim[str](
            state=KnowledgeState.KNOWN, value="Synthetic Candidate", evidence=(USER,)
        ),
        work_authorized=Claim[StrictBool](
            state=KnowledgeState.KNOWN, value=False, evidence=(USER,)
        ),
        experience=(
            WorkExperience(
                employer="Example Org",
                title="Developer",
                started_on=date(2020, 1, 1),
                evidence=(USER,),
            ),
        ),
        skills=(
            Skill(
                name="Python",
                evidence=(AI,),
                proficiency=Claim[SkillProficiency](
                    state=KnowledgeState.KNOWN, value=SkillProficiency.ADVANCED, evidence=(AI,)
                ),
            ),
        ),
        education=(
            Education(
                institution="Example Institute",
                qualification="Computing",
                started_on=date(2016, 1, 1),
                ended_on=date(2019, 1, 1),
                evidence=(USER,),
            ),
        ),
        certifications=(
            Certification(
                name="Example Certificate",
                issuer="Example Issuer",
                issued_on=date(2021, 1, 1),
                evidence=(USER,),
            ),
        ),
        languages=(
            LanguageProficiency(
                language="English",
                evidence=(USER,),
                proficiency=Claim[LanguageLevel](
                    state=KnowledgeState.KNOWN, value=LanguageLevel.C1, evidence=(USER,)
                ),
            ),
        ),
    )


def synthetic_job() -> JobPosting:
    return JobPosting(
        title="Python Developer",
        employer="Example Org",
        source=JobSource(
            name="Example Board",
            external_id="job-001",
            url="https://example.test/jobs/1?utm_source=fixture#details",
        ),
        requirements=(
            JobRequirement(
                description="Python experience",
                category=RequirementCategory.SKILL,
                mandatory=True,
                evidence=(SOURCE,),
            ),
        ),
    )


def test_profile_records_roundtrip_and_evidence_survives() -> None:
    profile = synthetic_profile()
    restored = CandidateProfile.model_validate_json(profile.model_dump_json())
    assert restored == profile
    assert restored.id == profile.id
    assert restored.work_authorized.value is False
    assert restored.work_authorized.user_confirmed
    assert not restored.skills[0].proficiency.user_confirmed
    assert restored.skills[0].evidence[0].origin == EvidenceOrigin.AI_INFERENCE
    assert restored.experience[0].is_current.state == KnowledgeState.MISSING


def test_missing_unknown_and_false_are_distinct_and_serializable() -> None:
    missing = CandidateProfile()
    unknown = CandidateProfile(work_authorized=Claim[StrictBool](state=KnowledgeState.UNKNOWN))
    negative = synthetic_profile()
    assert missing.work_authorized.state == KnowledgeState.MISSING
    assert unknown.work_authorized.state == KnowledgeState.UNKNOWN
    assert negative.work_authorized.state == KnowledgeState.KNOWN
    assert negative.work_authorized.value is False
    for profile in (missing, unknown, negative):
        assert CandidateProfile.model_validate_json(profile.model_dump_json()) == profile


@pytest.mark.parametrize(
    "data",
    [
        {"state": "known", "value": False},
        {"state": "known", "evidence": [USER]},
        {"state": "unknown", "value": False},
        {"state": "missing", "evidence": [USER]},
        {"state": "known", "value": "false", "evidence": [USER]},
        {"state": "known", "value": 0, "evidence": [USER]},
    ],
)
def test_invalid_claims(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Claim[StrictBool].model_validate(data)


def test_ai_origin_is_never_user_confirmation() -> None:
    inferred = Claim[str](state=KnowledgeState.KNOWN, value="Python", evidence=(AI,))
    assert not inferred.user_confirmed
    with pytest.raises(ValidationError):
        Claim[str].model_validate({**inferred.model_dump(), "user_confirmed": True})


@pytest.mark.parametrize(
    ("model", "data"),
    [
        (
            WorkExperience,
            {
                "employer": "Example",
                "title": "Developer",
                "started_on": "2022-01-01",
                "ended_on": "2021-01-01",
                "evidence": [USER],
            },
        ),
        (
            Education,
            {
                "institution": "Example",
                "qualification": "Degree",
                "started_on": "2022-01-01",
                "ended_on": "2021-01-01",
                "evidence": [USER],
            },
        ),
        (
            Certification,
            {
                "name": "Example",
                "issuer": "Example",
                "issued_on": "2022-01-01",
                "expires_on": "2021-01-01",
                "evidence": [USER],
            },
        ),
        (Skill, {"name": " ", "evidence": [USER]}),
        (Skill, {"name": "Python", "evidence": []}),
        (
            Skill,
            {
                "name": "Python",
                "evidence": [USER],
                "proficiency": {"state": "known", "value": "guru", "evidence": [USER]},
            },
        ),
        (
            LanguageProficiency,
            {
                "language": "English",
                "evidence": [USER],
                "proficiency": {"state": "known", "value": "C3", "evidence": [USER]},
            },
        ),
        (CandidateProfile, {"password": "SYNTHETIC"}),
        (CandidateProfile, {"id": "invalid"}),
        (JobSource, {"name": "board", "url": "file:///tmp/jobs"}),
        (JobSource, {"name": "board", "url": "https://user:synthetic@example.test/"}),
        (JobSource, {"name": "board", "external_id": " "}),
        (
            JobRequirement,
            {
                "description": "Python",
                "category": "skill",
                "mandatory": "false",
                "evidence": [SOURCE],
            },
        ),
        (DocumentVersion, {"document_id": uuid4(), "number": 0, "content": "Synthetic"}),
        (DocumentVersion, {"document_id": uuid4(), "number": 2, "content": "Synthetic"}),
        (DocumentVersion, {"document_id": uuid4(), "number": 1, "content": ""}),
        (JobFitAssessment, {"candidate_id": uuid4(), "job_id": uuid4(), "ai_score": float("nan")}),
        (JobFitAssessment, {"candidate_id": uuid4(), "job_id": uuid4(), "ai_score": 101}),
        (RequirementResult, {"requirement_id": uuid4(), "outcome": "met"}),
        (AuditEvent, {"application_id": uuid4(), "actor": "user", "action": "status_changed"}),
        (
            AuditEvent,
            {
                "application_id": uuid4(),
                "actor": "user",
                "action": "application_created",
                "to_status": "draft",
            },
        ),
    ],
)
def test_invalid_models(model: type[BaseModel], data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(data)


def test_current_experience_cannot_have_end_date() -> None:
    with pytest.raises(ValidationError):
        WorkExperience(
            employer="Example",
            title="Developer",
            started_on=date(2020, 1, 1),
            ended_on=date(2021, 1, 1),
            evidence=(USER,),
            is_current=Claim[StrictBool](state=KnowledgeState.KNOWN, value=True, evidence=(USER,)),
        )


def test_timestamp_normalization_and_naive_rejection() -> None:
    profile = CandidateProfile(
        created_at=datetime(2026, 1, 1, 2, tzinfo=timezone(timedelta(hours=2)))
    )
    assert profile.created_at == NOW
    assert profile.created_at.utcoffset() == timedelta(0)
    with pytest.raises(ValidationError):
        CandidateProfile(created_at=datetime(2026, 1, 1))
    assert CandidateProfile().id != CandidateProfile().id


def test_duplicate_nested_ids_are_rejected() -> None:
    skill = synthetic_profile().skills[0]
    with pytest.raises(ValidationError):
        CandidateProfile(skills=(skill, skill))
    job = synthetic_job()
    with pytest.raises(ValidationError):
        JobPosting.model_validate({**job.model_dump(), "requirements": [job.requirements[0]] * 2})


@pytest.mark.parametrize(("origin", "target"), list(product(ApplicationStatus, repeat=2)))
def test_entire_application_transition_matrix(
    origin: ApplicationStatus, target: ApplicationStatus
) -> None:
    app = JobApplication(
        candidate_id=uuid4(),
        job_id=uuid4(),
        document_version_id=uuid4(),
        status=origin,
        created_at=NOW,
        updated_at=NOW,
    )
    if target in TRANSITIONS[origin]:
        updated = app.transition(target, at=NOW + timedelta(hours=1))
        assert updated.status == target
        assert updated.id == app.id
        assert app.status == origin
    else:
        with pytest.raises(ValueError, match="Invalid application transition"):
            app.transition(target, at=NOW + timedelta(hours=1))


def test_application_document_and_time_rules() -> None:
    app = JobApplication(candidate_id=uuid4(), job_id=uuid4(), created_at=NOW, updated_at=NOW)
    with pytest.raises(ValidationError):
        app.transition(ApplicationStatus.READY)
    attached = app.attach_document(uuid4())
    ready = attached.transition(ApplicationStatus.READY)
    with pytest.raises(ValueError):
        ready.attach_document(uuid4())
    with pytest.raises(ValueError):
        ready.transition(ApplicationStatus.DRAFT, at=NOW - timedelta(days=1))
    with pytest.raises(ValidationError):
        app.transition(ApplicationStatus.WITHDRAWN, at=datetime(2026, 1, 2))


@pytest.mark.parametrize("approved", [True, False])
def test_approval_decisions_and_expiry(approved: bool) -> None:
    request = ApprovalRequest(
        application_id=uuid4(),
        document_version_id=uuid4(),
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    decided = request.decide(approved, at=NOW + timedelta(minutes=1))
    assert decided.status == (ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED)
    assert ApprovalRequest.model_validate_json(decided.model_dump_json()) == decided
    with pytest.raises(ValueError):
        decided.decide(approved, at=NOW + timedelta(minutes=2))
    for at in (NOW - timedelta(seconds=1), request.expires_at):
        with pytest.raises(ValidationError):
            request.decide(approved, at=at)


def test_document_version_roundtrip() -> None:
    document = ApplicationDocument(candidate_id=uuid4(), kind=DocumentKind.RESUME)
    first = DocumentVersion(document_id=document.id, number=1, content="Synthetic resume")
    second = DocumentVersion(
        document_id=document.id,
        number=2,
        previous_version_id=first.id,
        content="Synthetic revision",
        generated_by_ai=True,
    )
    assert DocumentVersion.model_validate_json(second.model_dump_json()) == second
    with pytest.raises(ValidationError):
        DocumentVersion(
            document_id=document.id, number=1, previous_version_id=first.id, content="x"
        )


@pytest.mark.parametrize(
    ("outcome", "evidence", "decision"),
    [
        (RequirementOutcome.MET, (USER,), FitDecision.ELIGIBLE),
        (RequirementOutcome.UNMET, (USER,), FitDecision.INELIGIBLE),
        (RequirementOutcome.UNKNOWN, (), FitDecision.NEEDS_REVIEW),
        (RequirementOutcome.MET, (AI,), FitDecision.NEEDS_REVIEW),
        (RequirementOutcome.UNMET, (AI,), FitDecision.NEEDS_REVIEW),
    ],
)
def test_fit_uses_explicit_rules_not_ai_score(
    outcome: RequirementOutcome, evidence: tuple[Evidence, ...], decision: FitDecision
) -> None:
    job = synthetic_job()
    assessment = JobFitAssessment(
        candidate_id=uuid4(),
        job_id=job.id,
        ai_score=100,
        results=(
            RequirementResult(
                requirement_id=job.requirements[0].id, outcome=outcome, evidence=evidence
            ),
        ),
    )
    assert assessment.decision(job) == decision
    assert JobFitAssessment.model_validate_json(assessment.model_dump_json()) == assessment


def test_fit_missing_results_unknown_mandatory_and_invalid_references() -> None:
    job = synthetic_job()
    assessment = JobFitAssessment(candidate_id=uuid4(), job_id=job.id)
    assert assessment.decision(job) == FitDecision.NEEDS_REVIEW
    with pytest.raises(ValueError):
        assessment.decision(synthetic_job())
    invalid = JobFitAssessment(
        candidate_id=uuid4(),
        job_id=job.id,
        results=(RequirementResult(requirement_id=uuid4(), outcome=RequirementOutcome.UNKNOWN),),
    )
    with pytest.raises(ValueError):
        invalid.decision(job)
    unspecified = JobPosting(
        title="Developer",
        employer="Example",
        source=JobSource(name="manual"),
        requirements=(
            JobRequirement(
                description="Python", category=RequirementCategory.SKILL, evidence=(SOURCE,)
            ),
        ),
    )
    assert (
        JobFitAssessment(candidate_id=uuid4(), job_id=unspecified.id).decision(unspecified)
        == FitDecision.NEEDS_REVIEW
    )


def test_predecessor_domain_rule_and_chronology() -> None:
    first = DocumentVersion(document_id=uuid4(), number=1, content="Synthetic", created_at=NOW)
    second = DocumentVersion(
        document_id=first.document_id,
        number=2,
        content="Synthetic revision",
        previous_version_id=first.id,
        created_at=NOW + timedelta(seconds=1),
    )
    second.validate_predecessor(first)
    invalid = DocumentVersion(
        document_id=first.document_id,
        number=2,
        content="Synthetic revision",
        previous_version_id=first.id,
        created_at=NOW - timedelta(seconds=1),
    )
    with pytest.raises(ValueError):
        invalid.validate_predecessor(first)


@pytest.mark.parametrize(
    ("action", "before", "after"),
    [
        ("status_changed", "accepted", "draft"),
        ("status_changed", "draft", "submitted"),
        ("approval_decided", "approved", "rejected"),
        ("approval_decided", "pending", "pending"),
    ],
)
def test_audit_rejects_invalid_state_changes(action: str, before: str, after: str) -> None:
    with pytest.raises(ValidationError):
        AuditEvent.model_validate(
            {
                "application_id": uuid4(),
                "actor": "user",
                "action": action,
                "from_status": before,
                "to_status": after,
            }
        )


def test_document_unknown_origin_is_distinct_from_false() -> None:
    unknown = DocumentVersion(document_id=uuid4(), number=1, content="Synthetic")
    declared = DocumentVersion(
        document_id=uuid4(), number=1, content="Synthetic", generated_by_ai=False
    )
    assert unknown.generated_by_ai is None
    assert declared.generated_by_ai is False
    for version in (unknown, declared):
        assert DocumentVersion.model_validate_json(version.model_dump_json()) == version


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://EXAMPLE.test:443/jobs/1?utm_source=x&gclid=y&id=1#details",
            "https://example.test/jobs/1?id=1",
        ),
        ("https://example.test/jobs/1/", "https://example.test/jobs/1/"),
        ("http://example.test/jobs/1", "http://example.test/jobs/1"),
        ("https://example.test/Jobs/1", "https://example.test/Jobs/1"),
        (
            "https://example.test/jobs?id=2&location=remote",
            "https://example.test/jobs?id=2&location=remote",
        ),
    ],
)
def test_canonical_urls_preserve_meaning(url: str, expected: str) -> None:
    source = JobSource.model_validate({"name": "Example", "url": url})
    assert source.canonical_url == expected
