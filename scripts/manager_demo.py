"""Reproducible manager demonstration using only implemented synthetic features."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from careerpilot.application.analysis import AnalysisReport, AnalysisRequest
from careerpilot.application.execution import RunControl, execution_scope, retry_read
from careerpilot.application.extraction import ProviderTransientFailure
from careerpilot.application.preparation_contracts import PreparationRequest, PreparationSuccess
from careerpilot.domain.actions import RecruiterMessage, ReviewDecision
from careerpilot.domain.common import utc_now
from careerpilot.domain.reliability import RetryPolicy
from careerpilot.evaluation import run_dataset
from careerpilot.integrations.simulated_actions import SimulatedActionAdapter
from careerpilot.portal import Portal
from careerpilot.workflows.action_approval import ApprovalThread


def demonstrate(root: Path) -> dict[str, object]:
    if root.exists():
        raise ValueError("Use a fresh demo directory to preserve previous evidence")
    Portal.initialize(root)
    portal = Portal(root)
    owner = uuid4()
    portal.seed(owner)
    candidate = portal.security.list(owner, "candidate")[0]
    job = portal.security.list(owner, "job")[0]
    report = portal.analyze(
        owner,
        AnalysisRequest(
            id=uuid4(),
            created_at=utc_now(),
            candidate_id=candidate,
            job_id=job,
        ),
    )
    assert isinstance(report, AnalysisReport)
    preparation = portal.prepare(
        owner,
        PreparationRequest(
            id=uuid4(),
            created_at=utc_now(),
            candidate_id=candidate,
            job_id=job,
            assessment_id=report.assessment_id,
        ),
    )
    assert isinstance(preparation, PreparationSuccess)
    resume = next(item for item in preparation.record.documents if item.kind == "resume")
    application = portal.application(owner, candidate, job, resume.version_id)
    waiting = portal.propose(
        owner,
        application.id,
        RecruiterMessage(
            target_id=portal.target.id,
            subject="Synthetic manager demo",
            body="A synthetic evidence-supported role inquiry, simulated only.",
        ),
    )
    assert isinstance(waiting, ApprovalThread) and waiting.paused
    # Close/reopen every resource and compiled graph before review/resume.
    reopened = Portal(root)
    proposal = waiting.review.proposal
    service = reopened.thread_service(owner, proposal.thread_id)
    with reopened.approval(service) as workflow:
        before = workflow.current(proposal.thread_id)
        result = workflow.resume(
            ReviewDecision(
                id=uuid4(),
                proposal_id=proposal.id,
                thread_id=proposal.thread_id,
                proposal_version=proposal.version,
                content_sha256=proposal.content_sha256,
                decision="approve",
            )
        )
        workflow.recover(proposal.thread_id)
        workflow.recover(proposal.thread_id)
    assert isinstance(service.adapter, SimulatedActionAdapter)
    recovered_effects = service.adapter.effect_count()
    assert recovered_effects == 1
    unknown_waiting = reopened.propose(
        owner,
        application.id,
        RecruiterMessage(
            target_id=reopened.target.id,
            subject="Synthetic lost acknowledgement",
            body="A separate approved synthetic action whose acknowledgement is lost.",
        ),
    )
    assert isinstance(unknown_waiting, ApprovalThread)
    unknown_proposal = unknown_waiting.review.proposal
    ambiguous_service = reopened.thread_service(owner, unknown_proposal.thread_id)
    assert isinstance(ambiguous_service.adapter, SimulatedActionAdapter)
    ambiguous_service.adapter.behavior = "ack_lost"
    with reopened.approval(ambiguous_service) as workflow:
        unknown = workflow.resume(
            ReviewDecision(
                id=uuid4(),
                proposal_id=unknown_proposal.id,
                thread_id=unknown_proposal.thread_id,
                proposal_version=1,
                content_sha256=unknown_proposal.content_sha256,
                decision="approve",
            )
        )
        workflow.recover(unknown_proposal.thread_id)
    assert ambiguous_service.adapter.effect_count() == 2  # Two distinct approved proposals.
    attempts = []

    def read() -> int:
        attempts.append(1)
        if len(attempts) == 1:
            raise ProviderTransientFailure("Synthetic temporary read failure")
        return 42

    control = RunControl()
    with execution_scope(control):
        answer = retry_read(
            read, RetryPolicy(max_retries=1, initial_delay=0), resource="model_calls"
        )
    evaluation = run_dataset(Path(__file__).resolve().parents[1] / "evals" / "dataset-v1.json")
    return {
        "integration_mode": "synthetic; external effects simulated",
        "analysis": report.model_dump(mode="json"),
        "preparation": preparation.model_dump(mode="json"),
        "waiting_for_approval": before.model_dump(mode="json"),
        "resumed": result.model_dump(mode="json"),
        "effects_after_repeated_recovery": recovered_effects,
        "ambiguous_result": unknown.review.status,
        "total_effects_for_two_distinct_proposals": ambiguous_service.adapter.effect_count(),
        "simulator_receipt_for_unknown": str(
            ambiguous_service.adapter.receipt(
                f"action:{unknown_proposal.id}:{unknown_proposal.content_sha256}"
            )
        ),
        "retry": {
            "attempts": len(attempts),
            "answer": answer,
            "counted_model_calls": control.record.model_calls,
        },
        "evaluation": evaluation,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = demonstrate(args.directory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "analysis": "completed",
                "drafts": "created",
                "approval": "paused_and_resumed",
                "recovery_effects": 1,
                "uncertain_result": result["ambiguous_result"],
                "retry_attempts": 2,
                "evaluation_results": result["evaluation"],
            }
        )
    )


if __name__ == "__main__":
    main()
