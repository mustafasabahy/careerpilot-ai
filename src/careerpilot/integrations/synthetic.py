"""Bundled, stable synthetic data; no network, credentials, or personal records."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import StrictBool

from careerpilot.application.analysis import AnalysisRequest
from careerpilot.application.storage import CareerRepository, IntegrityViolation, NotFound
from careerpilot.domain.baseline import (
    BaselineRules,
    CriterionDefinition,
    CriterionKind,
    NameAlias,
)
from careerpilot.domain.common import Claim, Evidence, EvidenceOrigin, KnowledgeState
from careerpilot.domain.jobs import JobPosting, JobRequirement, JobSource, RequirementCategory
from careerpilot.domain.profile import (
    CandidateProfile,
    LanguageLevel,
    LanguageProficiency,
    Skill,
    SkillProficiency,
)

FIXTURE_TIME = datetime(2026, 1, 1, tzinfo=UTC)
Scenario = Literal["complete", "missing-information", "hard-gap"]


def fixture_id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"careerpilot:synthetic:{name}")


@dataclass(frozen=True)
class SyntheticBundle:
    jobs: tuple[JobPosting, ...]
    profiles: tuple[CandidateProfile, ...]
    definitions: dict[UUID, tuple[CriterionDefinition, ...]]
    rules: BaselineRules

    def request(self, scenario: Scenario = "complete") -> AnalysisRequest:
        return AnalysisRequest(
            id=fixture_id(f"request:{scenario}"),
            created_at=FIXTURE_TIME,
            job_id=self.jobs[1 if scenario == "missing-information" else 0].id,
            candidate_id=self.profiles[1 if scenario == "hard-gap" else 0].id,
        )


def synthetic_bundle() -> SyntheticBundle:
    user = Evidence(
        id=fixture_id("user-evidence"),
        created_at=FIXTURE_TIME,
        origin=EvidenceOrigin.USER_STATEMENT,
        reference="fixture:synthetic-user-statement",
    )
    ai = Evidence(
        id=fixture_id("ai-evidence"),
        created_at=FIXTURE_TIME,
        origin=EvidenceOrigin.AI_INFERENCE,
        reference="fixture:unreviewed-inference",
    )
    source = Evidence(
        id=fixture_id("job-evidence"),
        created_at=FIXTURE_TIME,
        origin=EvidenceOrigin.SOURCE_DOCUMENT,
        reference="fixture:synthetic-job-source",
    )
    skills = (
        Skill(
            id=fixture_id("python-skill"),
            created_at=FIXTURE_TIME,
            name="Python programming",
            evidence=(user,),
            proficiency=Claim[SkillProficiency](
                state=KnowledgeState.KNOWN, value=SkillProficiency.ADVANCED, evidence=(user,)
            ),
        ),
        Skill(
            id=fixture_id("sql-skill"),
            created_at=FIXTURE_TIME,
            name="SQL",
            evidence=(ai,),
            proficiency=Claim[SkillProficiency](
                state=KnowledgeState.KNOWN, value=SkillProficiency.EXPERT, evidence=(ai,)
            ),
        ),
    )
    languages = tuple(
        LanguageProficiency(
            id=fixture_id(f"language:{name}"),
            created_at=FIXTURE_TIME,
            language=name,
            evidence=(user,),
            proficiency=Claim[LanguageLevel](
                state=KnowledgeState.KNOWN, value=level, evidence=(user,)
            ),
        )
        for name, level in (("English", LanguageLevel.C1), ("German", LanguageLevel.A2))
    )
    profiles = tuple(
        CandidateProfile(
            id=fixture_id(f"candidate:{authorized}"),
            created_at=FIXTURE_TIME,
            display_name=Claim[str](
                state=KnowledgeState.KNOWN, value="Synthetic Candidate", evidence=(user,)
            ),
            work_authorized=Claim[StrictBool](
                state=KnowledgeState.KNOWN, value=authorized, evidence=(user,)
            ),
            skills=skills,
            languages=languages,
        )
        for authorized in (True, False)
    )
    definitions: dict[UUID, tuple[CriterionDefinition, ...]] = {}
    jobs: list[JobPosting] = []
    for label in ("complete", "mixed"):
        specifications: list[tuple[str, RequirementCategory, bool | None, CriterionDefinition]] = []

        def criterion_id(key: str, job_label: str = label) -> UUID:
            return fixture_id(f"requirement:{job_label}:{key}")

        specifications.extend(
            [
                (
                    "Python at intermediate level",
                    RequirementCategory.SKILL,
                    True,
                    CriterionDefinition(
                        requirement_id=criterion_id("python"),
                        kind=CriterionKind.SKILL,
                        name=" PYTHON ",
                        minimum_skill=SkillProficiency.INTERMEDIATE,
                    ),
                ),
                (
                    "Work authorization explicitly declared",
                    RequirementCategory.ELIGIBILITY,
                    True,
                    CriterionDefinition(
                        requirement_id=criterion_id("authorization"),
                        kind=CriterionKind.WORK_AUTHORIZATION,
                        expected=True,
                    ),
                ),
                (
                    "English B2 preferred",
                    RequirementCategory.LANGUAGE,
                    False,
                    CriterionDefinition(
                        requirement_id=criterion_id("english"),
                        kind=CriterionKind.LANGUAGE,
                        name="English",
                        minimum_language=LanguageLevel.B2,
                    ),
                ),
            ]
        )
        if label == "mixed":
            specifications.extend(
                [
                    (
                        "SQL preferred",
                        RequirementCategory.SKILL,
                        False,
                        CriterionDefinition(
                            requirement_id=criterion_id("sql"), kind=CriterionKind.SKILL, name="SQL"
                        ),
                    ),
                    (
                        "German B2 preferred",
                        RequirementCategory.LANGUAGE,
                        False,
                        CriterionDefinition(
                            requirement_id=criterion_id("german"),
                            kind=CriterionKind.LANGUAGE,
                            name="German",
                            minimum_language=LanguageLevel.B2,
                        ),
                    ),
                    (
                        "Leadership potential (unspecified importance)",
                        RequirementCategory.EXPERIENCE,
                        None,
                        CriterionDefinition(
                            requirement_id=criterion_id("leadership"),
                            kind=CriterionKind.UNSUPPORTED,
                        ),
                    ),
                ]
            )
        job = JobPosting(
            id=fixture_id(f"job:{label}"),
            created_at=FIXTURE_TIME,
            title="Synthetic Python Developer",
            employer="Example Organization",
            source=JobSource(
                name="synthetic-fixtures", external_id=label, retrieved_at=FIXTURE_TIME
            ),
            requirements=tuple(
                JobRequirement(
                    id=definition.requirement_id,
                    created_at=FIXTURE_TIME,
                    description=description,
                    category=category,
                    mandatory=mandatory,
                    evidence=(source,),
                )
                for description, category, mandatory, definition in specifications
            ),
        )
        jobs.append(job)
        definitions[job.id] = tuple(definition for _, _, _, definition in specifications)
    return SyntheticBundle(
        jobs=tuple(jobs),
        profiles=profiles,
        definitions=definitions,
        rules=BaselineRules(aliases=(NameAlias(alias="python programming", canonical="python"),)),
    )


def seed_synthetic(repository: CareerRepository, bundle: SyntheticBundle) -> None:
    """Idempotent only for identical fixtures; never overwrite existing data."""
    for profile in bundle.profiles:
        try:
            existing_profile = repository.get(CandidateProfile, profile.id)
        except NotFound:
            repository.add(profile)
        else:
            if existing_profile != profile:
                raise IntegrityViolation("Synthetic profile ID already contains different data")
    for job in bundle.jobs:
        try:
            existing_job = repository.get(JobPosting, job.id)
        except NotFound:
            repository.add(job)
        else:
            if existing_job != job:
                raise IntegrityViolation("Synthetic job ID already contains different data")
