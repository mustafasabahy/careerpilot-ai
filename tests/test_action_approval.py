"""Approval binds a user event to exact immutable content, never to model prose."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from careerpilot.application.action_contracts import (
    ActionConflict,
    ActionDenied,
    ActionPrincipal,
    AdapterUnavailable,
)
from careerpilot.application.actions import ActionService, document_digest
from careerpilot.approval_demo import Scenario, action
from careerpilot.domain.actions import (
    ActionReview,
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
from careerpilot.integrations.simulated_actions import SimulatedActionAdapter
from careerpilot.integrations.synthetic import synthetic_bundle
from careerpilot.persistence.actions_sqlite import SQLiteActionDatabase
from careerpilot.persistence.checkpoints import isolated_checkpointer, local_checkpointer
from careerpilot.persistence.sqlite import SQLiteDatabase
from careerpilot.workflows.action_approval import ActionApprovalWorkflow


@dataclass
class Setup:
    database: SQLiteDatabase
    actions: SQLiteActionDatabase
    adapter: SimulatedActionAdapter
    service: ActionService
    saver: InMemorySaver
    workflow: ActionApprovalWorkflow
    application: JobApplication
    document: DocumentVersion
    target: ConfiguredTarget
    destination: ConfiguredTarget
    now: list[datetime]

    def request(self, content: object | None = None, **extra: object) -> ProposalRequest:
        return ProposalRequest.model_validate(
            {
                "id": uuid4(),
                "thread_id": uuid4(),
                "application_id": self.application.id,
                "content": content
                or RecruiterMessage(
                    target_id=self.target.id,
                    subject="Synthetic role inquiry",
                    body="Hello, I would like to discuss this synthetic role.",
                ),
                "expires_at": self.now[0] + timedelta(hours=1),
                **extra,
            }
        )

    def decision(
        self, review: ActionReview, choice: str = "approve", **extra: object
    ) -> ReviewDecision:
        return ReviewDecision.model_validate(
            {
                "id": uuid4(),
                "proposal_id": review.proposal.id,
                "thread_id": review.proposal.thread_id,
                "proposal_version": review.proposal.version,
                "content_sha256": review.proposal.content_sha256,
                "decision": choice,
                **extra,
            }
        )


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    database = SQLiteDatabase(tmp_path / "business.sqlite3")
    database.initialize()
    profile, job = synthetic_bundle().profiles[0], synthetic_bundle().jobs[0]
    document = ApplicationDocument(candidate_id=profile.id, kind=DocumentKind.RESUME)
    version = DocumentVersion(document_id=document.id, number=1, content="Synthetic confirmed CV")
    application = JobApplication(
        candidate_id=profile.id, job_id=job.id, document_version_id=version.id
    )
    with database.transaction() as repository:
        for entity in (profile, job, document, version, application):
            repository.add(entity)
    target = ConfiguredTarget(
        id=uuid4(),
        kind="recruiter_message",
        label="Synthetic recruiter",
        route_key="recruiter@example.invalid",
        revision=1,
    )
    destination = ConfiguredTarget(
        id=uuid4(),
        kind="submit_application",
        label="Synthetic destination",
        route_key="fixture-application-inbox",
        revision=1,
    )
    owner = uuid4()
    principal = ActionPrincipal(
        owner,
        owner,
        frozenset({profile.id}),
        frozenset({"recruiter_message", "submit_application", "change_application"}),
        frozenset({target.id, destination.id}),
    )
    adapter = SimulatedActionAdapter(tmp_path / "simulated.sqlite3")
    actions = SQLiteActionDatabase(database)
    now = [utc_now()]
    service = ActionService(
        principal, actions, (target, destination), adapter, clock=lambda: now[0]
    )
    saver = isolated_checkpointer()
    return Setup(
        database,
        actions,
        adapter,
        service,
        saver,
        ActionApprovalWorkflow(service, saver),
        application,
        version,
        target,
        destination,
        now,
    )


def test_real_interrupt_approval_executes_once_and_audits(setup: Setup) -> None:
    waiting = setup.workflow.prepare(setup.request())
    assert waiting.paused and waiting.review.status == "pending"
    assert setup.adapter.effect_count() == 0
    result = setup.workflow.resume(setup.decision(waiting.review))
    assert result.review.status == "simulated" and not result.paused
    assert result.review.outcome is not None
    assert setup.adapter.effect_count() == 1
    setup.workflow.recover(waiting.review.proposal.thread_id)
    setup.service.execute(waiting.review.proposal.thread_id)
    assert setup.adapter.effect_count() == 1
    with setup.actions.transaction() as repository:
        assert [event.action for event in repository.audit(waiting.review.proposal.id)] == [
            "proposed",
            "approved",
            "execution_claimed",
            "simulated",
        ]
    assert "Synthetic role inquiry" not in repr(list(setup.saver.list(None)))


@pytest.mark.parametrize("choice", ["reject", "revise"])
def test_rejection_or_revision_never_executes(setup: Setup, choice: str) -> None:
    waiting = setup.workflow.prepare(setup.request())
    result = setup.workflow.resume(
        setup.decision(waiting.review, choice, reason="Please revise wording")
    )
    assert result.review.status == ("rejected" if choice == "reject" else "revision_requested")
    assert setup.adapter.effect_count() == 0
    with pytest.raises(ActionDenied):
        setup.service.execute(waiting.review.proposal.thread_id)


def test_revision_has_new_id_hash_version_thread_and_needs_new_approval(setup: Setup) -> None:
    original = setup.workflow.prepare(setup.request()).review
    setup.workflow.resume(setup.decision(original, "revise", reason="Use a different subject"))
    revised = setup.workflow.prepare(
        setup.request(
            RecruiterMessage(
                target_id=setup.target.id, subject="Revised inquiry", body="Synthetic message"
            ),
            previous_proposal_id=original.proposal.id,
        )
    ).review
    assert (
        revised.proposal.version == 2
        and revised.proposal.previous_proposal_id == original.proposal.id
    )
    assert revised.proposal.id != original.proposal.id
    assert revised.proposal.content_sha256 != original.proposal.content_sha256
    assert revised.status == "pending" and setup.adapter.effect_count() == 0
    with pytest.raises(ActionConflict):
        setup.workflow.resume(
            setup.decision(revised, content_sha256=original.proposal.content_sha256)
        )
    result = setup.workflow.resume(setup.decision(revised))
    assert result.review.status == "simulated" and setup.adapter.effect_count() == 1


@pytest.mark.parametrize("altered", ["hash", "version", "thread", "extra"])
def test_altered_decision_is_rejected(setup: Setup, altered: str) -> None:
    review = setup.workflow.prepare(setup.request()).review
    data = setup.decision(review).model_dump()
    if altered == "hash":
        data["content_sha256"] = "0" * 64
    elif altered == "version":
        data["proposal_version"] = 2
    elif altered == "thread":
        data["thread_id"] = uuid4()
    else:
        data["assistant_says_approved"] = True
    with pytest.raises((ActionConflict, ActionDenied, ValidationError)):
        setup.workflow.resume(data)
    assert setup.adapter.effect_count() == 0
    assert setup.service.review(review.proposal.thread_id).decision is None


def test_expired_approval_and_expiry_after_approval(setup: Setup) -> None:
    review = setup.workflow.prepare(setup.request()).review
    setup.now[0] = review.proposal.expires_at
    with pytest.raises(ActionConflict):
        setup.workflow.resume(setup.decision(review))
    assert setup.adapter.effect_count() == 0
    setup.now[0] = review.proposal.created_at
    setup.service.decide(setup.decision(review))
    setup.now[0] = review.proposal.expires_at
    result = setup.workflow.recover(review.proposal.thread_id)
    assert result.review.status == "invalidated" and setup.adapter.effect_count() == 0


def test_edits_after_approval_invalidate_before_execution(setup: Setup) -> None:
    review = setup.workflow.prepare(setup.request()).review
    setup.service.decide(setup.decision(review))
    with setup.database.transaction() as repository:
        repository.transition_application(setup.application.id, ApplicationStatus.READY)
    result = setup.workflow.recover(review.proposal.thread_id)
    assert result.review.status == "invalidated"
    assert setup.adapter.effect_count() == 0


def test_changed_recipient_configuration_or_permissions_denied(setup: Setup) -> None:
    review = setup.workflow.prepare(setup.request()).review
    setup.service.decide(setup.decision(review))
    setup.service.targets[setup.target.id] = ConfiguredTarget.model_validate(
        {
            **setup.target.model_dump(),
            "route_key": "other@example.invalid",
            "revision": 2,
        }
    )
    assert setup.workflow.recover(review.proposal.thread_id).review.status == "invalidated"
    assert setup.adapter.effect_count() == 0


def test_unauthorized_user_cannot_review_resume_recover_or_execute(setup: Setup) -> None:
    review = setup.workflow.prepare(setup.request()).review
    other = ActionService(
        replace(setup.service.principal, owner_id=uuid4(), actor_id=uuid4()),
        setup.actions,
        (setup.target,),
        setup.adapter,
    )
    workflow = ActionApprovalWorkflow(other, setup.saver)
    for operation in (
        lambda: workflow.current(review.proposal.thread_id),
        lambda: workflow.resume(setup.decision(review)),
        lambda: workflow.recover(review.proposal.thread_id),
        lambda: other.execute(review.proposal.thread_id),
    ):
        with pytest.raises(ActionDenied):
            operation()
    assert setup.adapter.effect_count() == 0


def test_repeated_resume_rejected_and_forged_model_approval_cannot_execute(setup: Setup) -> None:
    review = setup.workflow.prepare(setup.request()).review
    with pytest.raises(ActionDenied):
        setup.service.execute(review.proposal.thread_id)
    decision = setup.decision(review)
    setup.workflow.resume(decision)
    with pytest.raises(ActionConflict):
        setup.workflow.resume(decision)
    assert setup.adapter.effect_count() == 1


def test_restart_with_sqlite_while_waiting(setup: Setup, tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.sqlite3"
    request = setup.request()
    with local_checkpointer(path, separate_from=(setup.database.path,)) as saver:
        waiting = ActionApprovalWorkflow(setup.service, saver).prepare(request)
    # New service/database/compiled graph and saver connection.
    fresh = ActionService(
        setup.service.principal,
        SQLiteActionDatabase(SQLiteDatabase(setup.database.path)),
        (setup.target, setup.destination),
        SimulatedActionAdapter(setup.adapter.path),
    )
    with local_checkpointer(path, separate_from=(setup.database.path,)) as saver:
        workflow = ActionApprovalWorkflow(fresh, saver)
        assert workflow.current(request.thread_id).paused
        result = workflow.resume(setup.decision(waiting.review))
        assert result.review.status == "simulated"
    assert setup.adapter.effect_count() == 1


def test_acknowledgement_lost_is_unknown_and_never_retried(setup: Setup) -> None:
    setup.adapter.behavior = "ack_lost"
    review = setup.workflow.prepare(setup.request()).review
    result = setup.workflow.resume(setup.decision(review))
    assert result.review.status == "unknown" and setup.adapter.effect_count() == 1
    for _ in range(2):
        assert setup.workflow.recover(review.proposal.thread_id).review.status == "unknown"
        assert setup.service.execute(review.proposal.thread_id).status == "unknown"
    assert setup.adapter.effect_count() == 1


def test_definite_failure_does_not_look_successful(setup: Setup) -> None:
    setup.adapter.behavior = "failure"
    review = setup.workflow.prepare(setup.request()).review
    result = setup.workflow.resume(setup.decision(review))
    assert result.review.status == "failed" and setup.adapter.effect_count() == 0


def test_recovery_after_claim_is_conservative_even_if_no_effect_was_sent(setup: Setup) -> None:
    review = setup.workflow.prepare(setup.request()).review
    setup.service.decide(setup.decision(review))
    with setup.actions.transaction() as repository:
        assert repository.claim(review.proposal.id, setup.service.principal.actor_id)
    assert setup.workflow.recover(review.proposal.thread_id).review.status == "unknown"
    assert setup.adapter.effect_count() == 0


def test_submission_requires_selected_ready_document_and_simulation_is_not_submission(
    setup: Setup,
) -> None:
    content = SubmitApplication(
        target_id=setup.destination.id,
        document_version_id=setup.document.id,
        document_sha256=document_digest(setup.document.content),
    )
    with pytest.raises(ActionConflict):
        setup.service.propose(setup.request(content))
    with setup.database.transaction() as repository:
        repository.transition_application(setup.application.id, ApplicationStatus.READY)
    review = setup.workflow.prepare(setup.request(content)).review
    assert review.document_content == setup.document.content
    assert setup.workflow.resume(setup.decision(review)).review.status == "simulated"
    with setup.database.transaction() as repository:
        assert (
            repository.get(JobApplication, setup.application.id).status == ApplicationStatus.READY
        )


def test_local_record_change_is_real_atomic_and_requires_approval(setup: Setup) -> None:
    review = setup.workflow.prepare(
        setup.request(
            ChangeApplication(
                target_status=ApplicationStatus.WITHDRAWN, reason="User stopped this application"
            )
        )
    ).review
    with setup.database.transaction() as repository:
        assert (
            repository.get(JobApplication, setup.application.id).status == ApplicationStatus.DRAFT
        )
    result = setup.workflow.resume(setup.decision(review))
    assert result.review.status == "applied" and setup.adapter.effect_count() == 0
    with setup.actions.transaction() as repository:
        assert (
            repository.get(JobApplication, setup.application.id).status
            == ApplicationStatus.WITHDRAWN
        )
        assert repository.audit(review.proposal.id)[-1].action == "applied"
        assert repository.audit_history(setup.application.id)[-1].action == "status_changed"


def test_immutable_proposal_and_exact_version_decision_sql_constraints(setup: Setup) -> None:
    review = setup.workflow.prepare(setup.request()).review
    for table in ("action_proposals",):
        with (
            sqlite3.connect(setup.database.path) as connection,
            pytest.raises(sqlite3.IntegrityError),
        ):
            connection.execute(f"UPDATE {table} SET payload='{{}}'")
        with (
            sqlite3.connect(setup.database.path) as connection,
            pytest.raises(sqlite3.IntegrityError),
        ):
            connection.execute(f"DELETE FROM {table}")
    assert setup.service.review(review.proposal.thread_id).proposal == review.proposal
    with pytest.raises(ValidationError):
        ChangeApplication(target_status=ApplicationStatus.SUBMITTED, reason="Bypass")


def test_no_adapter_no_registered_target_or_missing_permission_means_no_proposal(
    setup: Setup,
) -> None:
    disabled = ActionService(setup.service.principal, setup.actions, (setup.target,))
    with pytest.raises(AdapterUnavailable):
        disabled.propose(setup.request())
    no_permission = ActionService(
        replace(setup.service.principal, permissions=frozenset()),
        setup.actions,
        (setup.target,),
        setup.adapter,
    )
    with pytest.raises(ActionDenied):
        no_permission.propose(setup.request())
    with pytest.raises(ActionDenied):
        setup.service.propose(
            setup.request(
                RecruiterMessage(
                    target_id=uuid4(),
                    subject="Synthetic",
                    body="Synthetic",
                )
            )
        )


def test_concurrent_execution_claim_prevents_duplicate_effect(setup: Setup) -> None:
    review = setup.workflow.prepare(setup.request()).review
    setup.service.decide(setup.decision(review))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(lambda _: setup.service.execute(review.proposal.thread_id), (1, 2))
        )
    assert all(result.status in {"simulated", "unknown"} for result in results)
    assert setup.adapter.effect_count() == 1


def test_local_change_rolls_back_if_outcome_write_fails(setup: Setup) -> None:
    review = setup.workflow.prepare(
        setup.request(
            ChangeApplication(
                target_status=ApplicationStatus.WITHDRAWN, reason="Synthetic withdrawal"
            )
        )
    ).review
    setup.service.decide(setup.decision(review))
    with sqlite3.connect(setup.database.path) as connection:
        connection.execute(
            "CREATE TRIGGER synthetic_fail BEFORE INSERT ON action_outcomes "
            "BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        setup.service.execute(review.proposal.thread_id)
    with setup.actions.transaction() as repository:
        assert (
            repository.get(JobApplication, setup.application.id).status == ApplicationStatus.DRAFT
        )
        assert repository.status(review.proposal.id) == "approved"
        assert [event.action for event in repository.audit(review.proposal.id)] == [
            "proposed",
            "approved",
        ]


def test_permission_revocation_and_unpersisted_llm_resume_are_not_approval(setup: Setup) -> None:
    review = setup.workflow.prepare(setup.request()).review
    with pytest.raises(ActionDenied):
        setup.service.verify_event(review.proposal.thread_id, uuid4())
    setup.service.decide(setup.decision(review))
    revoked = ActionService(
        replace(setup.service.principal, permissions=frozenset()),
        setup.actions,
        (setup.target,),
        setup.adapter,
    )
    with pytest.raises(ActionDenied):
        ActionApprovalWorkflow(revoked, setup.saver).recover(review.proposal.thread_id)
    assert setup.adapter.effect_count() == 0


def test_selected_document_tamper_is_detected_after_approval(setup: Setup) -> None:
    with setup.database.transaction() as repository:
        repository.transition_application(setup.application.id, ApplicationStatus.READY)
    review = setup.workflow.prepare(
        setup.request(
            SubmitApplication(
                target_id=setup.destination.id,
                document_version_id=setup.document.id,
                document_sha256=document_digest(setup.document.content),
            )
        )
    ).review
    setup.service.decide(setup.decision(review))
    altered = DocumentVersion.model_validate(
        {**setup.document.model_dump(), "content": "Altered CV"}
    )
    with sqlite3.connect(setup.database.path) as connection:
        connection.execute(
            "UPDATE versions SET payload=? WHERE id=?", (altered.model_dump_json(), str(altered.id))
        )
    assert setup.workflow.recover(review.proposal.thread_id).review.status == "invalidated"
    assert setup.adapter.effect_count() == 0


@pytest.mark.parametrize("subject,body", [("Hello\r\nBcc: other", "Text"), ("Hello", "Text\x00")])
def test_malformed_content_is_rejected(subject: str, body: str) -> None:
    with pytest.raises(ValidationError):
        RecruiterMessage(target_id=uuid4(), subject=subject, body=body)


def test_only_one_revision_can_follow_a_proposal(setup: Setup) -> None:
    original = setup.workflow.prepare(setup.request()).review
    setup.workflow.resume(setup.decision(original, "revise", reason="Revise"))
    setup.workflow.prepare(setup.request(previous_proposal_id=original.proposal.id))
    with pytest.raises(ActionConflict):
        setup.workflow.prepare(setup.request(previous_proposal_id=original.proposal.id))


def test_crash_after_authenticated_decision_before_command_recovers(setup: Setup) -> None:
    review = setup.workflow.prepare(setup.request()).review
    event = setup.service.decide(setup.decision(review))
    assert setup.adapter.effect_count() == 0
    # No Command was issued; a new workflow reads the already committed user event.
    recovered = ActionApprovalWorkflow(setup.service, setup.saver).recover(
        review.proposal.thread_id
    )
    assert recovered.review.status == "simulated"
    assert recovered.review.decision == event
    assert [item.action for item in setup.service.audit(review.proposal.thread_id)] == [
        "proposed",
        "approved",
        "execution_claimed",
        "simulated",
    ]


def test_simulator_idempotency_ledger_survives_adapter_restart(setup: Setup) -> None:
    review = setup.workflow.prepare(setup.request()).review
    setup.workflow.resume(setup.decision(review))
    proposal = review.proposal
    key = f"action:{proposal.id}:{proposal.content_sha256}"
    restarted = SimulatedActionAdapter(setup.adapter.path)
    assert restarted.execute(proposal, key) == setup.adapter.execute(proposal, key)
    assert restarted.effect_count() == 1


def test_concurrent_authenticated_decisions_one_wins(setup: Setup) -> None:
    review = setup.workflow.prepare(setup.request()).review

    def decide(_: int) -> str:
        try:
            setup.service.decide(setup.decision(review))
            return "approved"
        except ActionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(decide, (1, 2))) == ["approved", "conflict"]
    assert setup.adapter.effect_count() == 0
    assert setup.workflow.recover(review.proposal.thread_id).review.status == "simulated"


def test_expired_proposal_can_be_revised_but_never_executed_without_fresh_approval(
    setup: Setup,
) -> None:
    original = setup.workflow.prepare(setup.request()).review
    setup.now[0] = original.proposal.expires_at
    result = setup.workflow.resume(
        setup.decision(original, "revise", reason="Refresh expired proposal")
    )
    assert result.review.status == "revision_requested"
    fresh = setup.workflow.prepare(setup.request(previous_proposal_id=original.proposal.id))
    assert fresh.review.proposal.version == 2 and fresh.paused
    assert setup.adapter.effect_count() == 0


@pytest.mark.parametrize("scenario", ["message", "submission", "record-change"])
def test_synthetic_demo_supports_all_three_actions(tmp_path: Path, scenario: Scenario) -> None:
    result = action(tmp_path, scenario, "demo")
    assert isinstance(result, dict) and result["waiting"]["paused"]
    assert result["result"]["review"]["status"] == (
        "applied" if scenario == "record-change" else "simulated"
    )
