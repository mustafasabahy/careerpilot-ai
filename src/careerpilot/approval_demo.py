"""Synthetic staged approvals with explicit operator decisions and simulated effects."""

import argparse
import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from careerpilot.application.action_contracts import ActionPrincipal
from careerpilot.application.actions import ActionService, document_digest
from careerpilot.application.storage import NotFound
from careerpilot.config import configured_execution
from careerpilot.domain.actions import (
    ChangeApplication,
    ConfiguredTarget,
    ProposalRequest,
    RecruiterMessage,
    ReviewDecision,
    SubmitApplication,
)
from careerpilot.domain.applications import (
    ApplicationDocument,
    ApplicationStatus,
    DocumentKind,
    DocumentVersion,
    JobApplication,
)
from careerpilot.domain.common import utc_now
from careerpilot.domain.profile import CandidateProfile
from careerpilot.integrations.simulated_actions import SimulatedActionAdapter
from careerpilot.integrations.synthetic import fixture_id, synthetic_bundle
from careerpilot.persistence.actions_sqlite import SQLiteActionDatabase
from careerpilot.persistence.checkpoints import local_checkpointer
from careerpilot.persistence.sqlite import SQLiteDatabase
from careerpilot.workflows.action_approval import ActionApprovalWorkflow

Scenario = Literal["message", "submission", "record-change"]


def compose(directory: Path, scenario: Scenario) -> tuple[ActionService, ProposalRequest]:
    directory.mkdir(parents=True, exist_ok=True)
    database = SQLiteDatabase(directory / "business.sqlite3")
    database.initialize()
    application_id = fixture_id(f"approval-demo:{scenario}:application")
    with database.transaction() as repository:
        try:
            application = repository.get(JobApplication, application_id)
        except NotFound:
            profile = CandidateProfile(id=fixture_id(f"approval-demo:{scenario}:candidate"))
            job = synthetic_bundle().jobs[0]
            document = ApplicationDocument(
                id=fixture_id(f"approval-demo:{scenario}:document"),
                candidate_id=profile.id,
                kind=DocumentKind.RESUME,
            )
            version = DocumentVersion(
                id=fixture_id(f"approval-demo:{scenario}:version"),
                document_id=document.id,
                number=1,
                content="Synthetic user-reviewed CV for the approval demonstration.",
                generated_by_ai=False,
            )
            application = JobApplication(
                id=application_id,
                candidate_id=profile.id,
                job_id=job.id,
                document_version_id=version.id,
            )
            for entity in (profile, job, document, version, application):
                repository.add(entity)
            if scenario == "submission":
                application = repository.transition_application(
                    application.id, ApplicationStatus.READY
                )
    recruiter = ConfiguredTarget(
        id=fixture_id("approval-demo:recruiter"),
        kind="recruiter_message",
        label="Synthetic recruiter",
        route_key="recruiter@example.invalid",
        revision=1,
    )
    destination = ConfiguredTarget(
        id=fixture_id("approval-demo:destination"),
        kind="submit_application",
        label="Synthetic application destination",
        route_key="fixture-inbox",
        revision=1,
    )
    owner = fixture_id("approval-demo:owner")
    service = ActionService(
        ActionPrincipal(
            owner,
            owner,
            frozenset({application.candidate_id}),
            frozenset({"recruiter_message", "submit_application", "change_application"}),
            frozenset({recruiter.id, destination.id}),
        ),
        SQLiteActionDatabase(database),
        (recruiter, destination),
        # Explicitly configured simulation only; ActionService itself defaults to no adapter.
        SimulatedActionAdapter(directory / "simulation.sqlite3"),
    )
    content: RecruiterMessage | SubmitApplication | ChangeApplication
    if scenario == "message":
        content = RecruiterMessage(
            target_id=recruiter.id,
            subject="Synthetic role inquiry",
            body="Hello, I would welcome a discussion of this synthetic role.",
        )
    elif scenario == "submission":
        with database.transaction() as repository:
            version = repository.get(
                DocumentVersion, fixture_id(f"approval-demo:{scenario}:version")
            )
        content = SubmitApplication(
            target_id=destination.id,
            document_version_id=version.id,
            document_sha256=document_digest(version.content),
        )
    else:
        content = ChangeApplication(
            target_status=ApplicationStatus.WITHDRAWN,
            reason="User chose to stop this synthetic application",
        )
    request = ProposalRequest(
        id=fixture_id(f"approval-demo:{scenario}:proposal"),
        thread_id=fixture_id(f"approval-demo:{scenario}:thread"),
        application_id=application.id,
        content=content,
        expires_at=utc_now() + timedelta(hours=1),
    )
    return service, request


def action(directory: Path, scenario: Scenario, operation: str) -> object:
    service, request = compose(directory, scenario)
    with local_checkpointer(
        directory / "checkpoints.sqlite3", separate_from=(directory / "business.sqlite3",)
    ) as saver:
        workflow = ActionApprovalWorkflow(service, saver)
        if operation == "prepare":
            return workflow.prepare(request).model_dump(mode="json")
        if operation == "state":
            return workflow.current(request.thread_id).model_dump(mode="json")
        if operation == "recover":
            return workflow.recover(request.thread_id).model_dump(mode="json")
        if operation == "demo":
            waiting = workflow.prepare(request)
        else:
            waiting = workflow.current(request.thread_id)
        choice = "approve" if operation == "demo" else operation
        decision = ReviewDecision.model_validate(
            {
                "id": fixture_id(f"approval-demo:{scenario}:decision:{choice}"),
                "proposal_id": waiting.review.proposal.id,
                "thread_id": request.thread_id,
                "proposal_version": waiting.review.proposal.version,
                "content_sha256": waiting.review.proposal.content_sha256,
                "decision": choice,
                "reason": "Synthetic explicit operator review",
            }
        )
        result = workflow.resume(decision)
        if operation != "demo":
            return result.model_dump(mode="json")
        return {
            "waiting": waiting.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
            "audit": [item.model_dump(mode="json") for item in service.audit(request.thread_id)],
        }


@configured_execution
def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic approval workflow; no external sending")
    parser.add_argument("--directory", type=Path)
    parser.add_argument(
        "--scenario", choices=("message", "submission", "record-change"), default="message"
    )
    parser.add_argument(
        "--operation",
        choices=("demo", "prepare", "approve", "reject", "revise", "state", "recover"),
        default="demo",
    )
    arguments = parser.parse_args()
    if arguments.operation != "demo" and arguments.directory is None:
        parser.error("Durable operations require --directory")
    if arguments.directory is None:
        with TemporaryDirectory(prefix="careerpilot-approval-") as directory:
            result = action(Path(directory), arguments.scenario, arguments.operation)
    else:
        result = action(arguments.directory, arguments.scenario, arguments.operation)
    print(json.dumps(result, ensure_ascii=False, indent=2))
