"""Synthetic evidence-linked preparation demo, with no paid model call by default."""

import argparse
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from careerpilot.application.analysis import AnalysisRequest, AnalysisService
from careerpilot.application.discovery_contracts import AccessGrant, Permission
from careerpilot.application.extraction import ExtractionError
from careerpilot.application.preparation import PreparationService
from careerpilot.application.preparation_contracts import (
    DraftPlanner,
    PreparationAuthorization,
    PreparationFailure,
    PreparationRequest,
)
from careerpilot.config import configured_execution
from careerpilot.domain.applications import ApplicationDocument, DocumentKind, DocumentVersion
from careerpilot.domain.profile import CandidateProfile, Certification, Education, WorkExperience
from careerpilot.integrations.synthetic import FIXTURE_TIME, fixture_id, synthetic_bundle
from careerpilot.persistence.sqlite import SQLiteDatabase
from careerpilot.providers.factory import build_draft_planner
from careerpilot.workflows.preparation import PreparationWorkflow

Scenario = Literal["ready", "missing-profile", "conflicting-dates"]
ORIGINAL_CV = "Original user CV\nSynthetic Candidate\nSoftware Developer at Example Labs\n"


def build_preparation_demo(
    database: SQLiteDatabase,
    planner: DraftPlanner,
    scenario: Scenario = "ready",
    *,
    allow_export: bool = False,
) -> tuple[PreparationService, PreparationRequest]:
    bundle = synthetic_bundle()
    base = bundle.profiles[0]
    evidence = base.skills[0].evidence
    experience = WorkExperience(
        id=fixture_id("prep-experience"),
        created_at=FIXTURE_TIME,
        employer="Example Labs",
        title="Software Developer",
        started_on=date(2021, 1, 1),
        ended_on=date(2024, 1, 1),
        evidence=evidence,
    )
    profile_data = {
        **base.model_dump(),
        "id": fixture_id(f"prep-candidate:{scenario}"),
        "experience": [experience],
        "education": [
            Education(
                id=fixture_id("prep-education"),
                created_at=FIXTURE_TIME,
                institution="Example Institute",
                qualification="Computing degree",
                started_on=date(2017, 1, 1),
                ended_on=date(2020, 1, 1),
                evidence=evidence,
            )
        ],
        "certifications": [
            Certification(
                id=fixture_id("prep-certificate"),
                created_at=FIXTURE_TIME,
                name="Example Systems Certificate",
                issuer="Example Training",
                issued_on=date(2022, 1, 1),
                evidence=evidence,
            )
        ],
    }
    if scenario == "missing-profile":
        profile = CandidateProfile(id=profile_data["id"], created_at=FIXTURE_TIME)
    else:
        if scenario == "conflicting-dates":
            conflict = WorkExperience.model_validate(
                {
                    **experience.model_dump(),
                    "id": fixture_id("conflicting-experience"),
                    "ended_on": date(2023, 1, 1),
                }
            )
            profile_data["experience"] = [experience, conflict]
        profile = CandidateProfile.model_validate(profile_data)
    job = bundle.jobs[1]
    document = ApplicationDocument(
        id=fixture_id(f"original-document:{scenario}"),
        created_at=FIXTURE_TIME,
        candidate_id=profile.id,
        kind=DocumentKind.RESUME,
    )
    original = DocumentVersion(
        id=fixture_id(f"original-version:{scenario}"),
        created_at=FIXTURE_TIME,
        document_id=document.id,
        number=1,
        content=ORIGINAL_CV,
        generated_by_ai=False,
    )
    with database.transaction() as repository:
        for entity in (profile, job, document, original):
            repository.add(entity)
    analysis_service = AnalysisService(database.transaction, bundle.definitions, bundle.rules)
    analysis_request = AnalysisRequest(
        id=fixture_id(f"prep-assessment:{scenario}"),
        created_at=FIXTURE_TIME,
        job_id=job.id,
        candidate_id=profile.id,
    )
    criteria = analysis_service.normalize(job.id)
    report = analysis_service.calculate(analysis_request, criteria)
    assessment_id = analysis_service.validate_and_store(analysis_request, criteria, report)
    access = AccessGrant(
        actor_id=fixture_id("prep-actor"),
        permissions=frozenset(Permission),
        candidate_ids=frozenset({profile.id}),
        source_ids=frozenset({"synthetic"}),
        company_ids=frozenset(),
    )
    authorization = PreparationAuthorization(
        access, frozenset({job.id}), allow_drafts=True, allow_profile_model_export=allow_export
    )
    service = PreparationService(
        authorization, database.transaction, bundle.definitions, bundle.rules, planner
    )
    request = PreparationRequest(
        id=fixture_id(f"prep-request:{scenario}"),
        created_at=FIXTURE_TIME,
        candidate_id=profile.id,
        job_id=job.id,
        assessment_id=assessment_id,
        resume_document_id=document.id,
        original_resume_version_id=original.id,
    )
    return service, request


@configured_execution
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evidence-linked career preparation, mock by default"
    )
    parser.add_argument(
        "--scenario", choices=["ready", "missing-profile", "conflicting-dates"], default="ready"
    )
    parser.add_argument(
        "--allow-profile-model-export",
        action="store_true",
        help="Explicitly permit professional fact text to reach a configured real planner",
    )
    args = parser.parse_args()
    try:
        planner = build_draft_planner()
        with TemporaryDirectory(prefix="careerpilot-prepare-") as directory:
            database = SQLiteDatabase(Path(directory) / "preparation.sqlite3")
            database.initialize()
            service, request = build_preparation_demo(
                database, planner, args.scenario, allow_export=args.allow_profile_model_export
            )
            result = PreparationWorkflow(service).run(request)
            print(result.model_dump_json(indent=2))
            if isinstance(result, PreparationFailure):
                raise SystemExit(2)
    except ExtractionError:
        parser.exit(
            2, "Planner configuration failed; check explicit provider/model/credential settings\n"
        )
