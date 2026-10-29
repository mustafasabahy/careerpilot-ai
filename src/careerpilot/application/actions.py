"""Exact-version approval and side-effect control, independent of LangGraph/SQLite."""

import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import timedelta
from uuid import UUID, uuid4

from careerpilot.application.action_contracts import (
    ActionAdapter,
    ActionConflict,
    ActionDatabase,
    ActionDenied,
    ActionPrincipal,
    ActionRepository,
    AdapterReceipt,
    AdapterUnavailable,
    DefiniteAdapterFailure,
)
from careerpilot.application.execution import current_run, remaining_timeout
from careerpilot.domain.actions import (
    ActionAudit,
    ActionDecision,
    ActionOutcome,
    ActionProposal,
    ActionReview,
    ChangeApplication,
    ConfiguredTarget,
    ProposalRequest,
    ReviewDecision,
    SubmitApplication,
)
from careerpilot.domain.applications import (
    ApplicationDocument,
    DocumentKind,
    DocumentVersion,
    JobApplication,
)
from careerpilot.domain.common import Model, UtcTimestamp, utc_now


def digest(value: Model) -> str:
    data = value.model_dump(mode="json")
    if isinstance(value, ActionProposal):
        data.pop("content_sha256")
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def document_digest(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


class ActionService:
    def __init__(
        self,
        principal: ActionPrincipal,
        database: ActionDatabase,
        targets: Iterable[ConfiguredTarget] = (),
        adapter: ActionAdapter | None = None,
        *,
        clock: Callable[[], UtcTimestamp] = utc_now,
    ) -> None:
        self.principal = principal
        self.database = database
        self.targets = {
            item.id: ConfiguredTarget.model_validate(item.model_dump()) for item in targets
        }
        self.adapter = adapter
        self.clock = clock

    def _scope(self, proposal: ActionProposal) -> None:
        if (
            proposal.owner_id != self.principal.owner_id
            or proposal.candidate_id not in self.principal.candidate_ids
            or proposal.content.kind not in self.principal.permissions
            or (proposal.target is not None and proposal.target.id not in self.principal.target_ids)
        ):
            raise ActionDenied("Sensitive action is not authorized")
        if digest(proposal) != proposal.content_sha256:
            raise ActionConflict("Stored proposal content hash is invalid")

    def _current(self, repository: ActionRepository, proposal: ActionProposal) -> None:
        self._scope(proposal)
        if not proposal.created_at <= self.clock() < proposal.expires_at:
            raise ActionConflict("Proposal is expired or not yet valid")
        application = repository.get(JobApplication, proposal.application_id)
        if (
            application.candidate_id != proposal.candidate_id
            or application.job_id != proposal.job_id
            or digest(application) != proposal.application_sha256
        ):
            raise ActionConflict("Application changed; prepare a new proposal")
        if proposal.target is not None:
            if self.targets.get(proposal.target.id) != proposal.target:
                raise ActionConflict("Configured recipient or destination has changed")
            if self.adapter is None or not self.adapter.enabled:
                raise AdapterUnavailable("External actions require an explicit safe adapter")
            if self.adapter.identifier != proposal.adapter_id:
                raise ActionConflict("Execution adapter has changed")
        if isinstance(proposal.content, SubmitApplication):
            document = repository.get(DocumentVersion, proposal.content.document_version_id)
            source_document = repository.get(ApplicationDocument, document.document_id)
            if (
                source_document.candidate_id != proposal.candidate_id
                or source_document.kind != DocumentKind.RESUME
                or application.status.value != "ready"
                or application.document_version_id != document.id
                or document_digest(document.content) != proposal.content.document_sha256
            ):
                raise ActionConflict("Submission requires the exact selected ready document")
        elif isinstance(proposal.content, ChangeApplication):
            application.transition(proposal.content.target_status, at=self.clock())

    def propose(self, raw: object) -> ActionReview:
        if isinstance(raw, ProposalRequest):
            raw = raw.model_dump()
        request = ProposalRequest.model_validate(raw)
        now = self.clock()
        if not now < request.expires_at <= now + timedelta(hours=24):
            raise ActionConflict("Proposal expiry must be within the next 24 hours")
        with self.database.transaction() as repository:
            application = repository.get(JobApplication, request.application_id)
            if (
                application.candidate_id not in self.principal.candidate_ids
                or request.content.kind not in self.principal.permissions
            ):
                raise ActionDenied("Proposal is not authorized")
            version = 1
            if request.previous_proposal_id is not None:
                previous = repository.proposal(
                    self.principal.owner_id, request.previous_proposal_id
                )
                self._scope(previous)
                if (
                    repository.status(previous.id) != "revision_requested"
                    or previous.application_id != request.application_id
                    or previous.content.kind != request.content.kind
                ):
                    raise ActionConflict("Revision requires a matching revision-requested proposal")
                version = previous.version + 1
            target = None
            adapter_id = "local-record-v1"
            if not isinstance(request.content, ChangeApplication):
                target = self.targets.get(request.content.target_id)
                if target is None:
                    raise ActionDenied("Target is not registered")
                if self.adapter is None or not self.adapter.enabled:
                    raise AdapterUnavailable("No external adapter configured")
                adapter_id = self.adapter.identifier
            proposal = ActionProposal(
                id=request.id,
                thread_id=request.thread_id,
                created_at=now,
                owner_id=self.principal.owner_id,
                candidate_id=application.candidate_id,
                job_id=application.job_id,
                application_id=application.id,
                version=version,
                previous_proposal_id=request.previous_proposal_id,
                expires_at=request.expires_at,
                application_sha256=digest(application),
                target=target,
                adapter_id=adapter_id,
                content=request.content,
                content_sha256="0" * 64,
            )
            proposal = ActionProposal.model_validate(
                {**proposal.model_dump(), "content_sha256": digest(proposal)}
            )
            self._current(repository, proposal)
            repository.add_proposal(proposal, self.principal.actor_id)
            return self._review(repository, proposal)

    def _review(self, repository: ActionRepository, proposal: ActionProposal) -> ActionReview:
        self._scope(proposal)
        content = None
        if isinstance(proposal.content, SubmitApplication):
            content = repository.get(DocumentVersion, proposal.content.document_version_id).content
        return ActionReview(
            proposal=proposal,
            status=repository.status(proposal.id),
            decision=repository.decision(proposal.id),
            outcome=repository.outcome(proposal.id),
            document_content=content,
        )

    def review(self, thread_id: UUID) -> ActionReview:
        with self.database.transaction() as repository:
            proposal = repository.thread_proposal(self.principal.owner_id, thread_id)
            return self._review(repository, proposal)

    def audit(self, thread_id: UUID) -> tuple[ActionAudit, ...]:
        with self.database.transaction() as repository:
            proposal = repository.thread_proposal(self.principal.owner_id, thread_id)
            self._scope(proposal)
            return repository.audit(proposal.id)

    def decide(self, raw: object) -> ActionDecision:
        if isinstance(raw, ReviewDecision):
            raw = raw.model_dump()
        request = ReviewDecision.model_validate(raw)
        with self.database.transaction() as repository:
            proposal = repository.proposal(self.principal.owner_id, request.proposal_id)
            self._scope(proposal)
            if request.decision == "approve":
                self._current(repository, proposal)
            if (
                request.thread_id != proposal.thread_id
                or request.proposal_version != proposal.version
                or request.content_sha256 != proposal.content_sha256
            ):
                raise ActionConflict("Decision does not identify the exact proposal version")
            if repository.status(proposal.id) != "pending":
                raise ActionConflict("Proposal has already been reviewed")
            event = ActionDecision(
                id=request.id,
                created_at=self.clock(),
                owner_id=self.principal.owner_id,
                actor_id=self.principal.actor_id,
                proposal_id=proposal.id,
                thread_id=proposal.thread_id,
                proposal_version=proposal.version,
                content_sha256=proposal.content_sha256,
                decision=request.decision,
                reason=request.reason,
            )
            self.verify_exact_event(proposal, event)
            repository.decide(event)
            return event

    def verify_event(self, thread_id: UUID, event_id: UUID) -> ActionDecision:
        with self.database.transaction() as repository:
            proposal = repository.thread_proposal(self.principal.owner_id, thread_id)
            self._scope(proposal)
            event = repository.decision(proposal.id)
            if (
                event is None
                or event.id != event_id
                or (
                    event.owner_id != proposal.owner_id
                    or event.thread_id != thread_id
                    or event.proposal_version != proposal.version
                    or event.content_sha256 != proposal.content_sha256
                )
            ):
                raise ActionDenied("No authenticated exact-version decision exists")
            self.verify_exact_event(proposal, event)
            return event

    def cancel(self, thread_id: UUID) -> ActionReview:
        # Durable action cancellation is distinct from stopping one invocation.
        with self.database.transaction() as repository:
            proposal = repository.thread_proposal(self.principal.owner_id, thread_id)
            self._scope(proposal)
            state = repository.status(proposal.id)
            if state == "pending":
                repository.decide(
                    ActionDecision(
                        id=uuid4(),
                        created_at=self.clock(),
                        owner_id=proposal.owner_id,
                        actor_id=self.principal.actor_id,
                        proposal_id=proposal.id,
                        thread_id=proposal.thread_id,
                        proposal_version=proposal.version,
                        content_sha256=proposal.content_sha256,
                        decision="reject",
                        reason="User cancelled the staged action",
                    )
                )
            elif state in {"approved", "executing"}:
                repository.finish(
                    ActionOutcome(
                        proposal_id=proposal.id,
                        created_at=self.clock(),
                        status="unknown" if state == "executing" else "invalidated",
                        code="ambiguous_execution" if state == "executing" else "cancelled",
                    ),
                    self.principal.actor_id,
                )
            return self._review(repository, proposal)

    def execute(self, thread_id: UUID) -> ActionReview:
        # Claim and record local changes atomically, but never hold a transaction
        # over an adapter call. A claim recovered without a result is ambiguous.
        with self.database.transaction() as repository:
            proposal = repository.thread_proposal(self.principal.owner_id, thread_id)
            self._scope(proposal)
            event = repository.decision(proposal.id)
            if event is None or event.decision != "approve":
                raise ActionDenied("An authenticated approval is required before execution")
            self.verify_exact_event(proposal, event)
            if event.created_at > self.clock():
                raise ActionConflict("Approval decision is in the future")
            existing = repository.outcome(proposal.id)
            if existing is not None:
                return self._review(repository, proposal)
            if repository.status(proposal.id) == "executing":
                repository.finish(
                    ActionOutcome(
                        proposal_id=proposal.id,
                        created_at=self.clock(),
                        status="unknown",
                        code="ambiguous_execution",
                    ),
                    self.principal.actor_id,
                )
                return self._review(repository, proposal)
            try:
                self._current(repository, proposal)
            except (ActionConflict, AdapterUnavailable):
                repository.finish(
                    ActionOutcome(
                        proposal_id=proposal.id,
                        created_at=self.clock(),
                        status="invalidated",
                        code="stale_or_expired",
                    ),
                    self.principal.actor_id,
                )
                return self._review(repository, proposal)
            if control := current_run():
                control.check()
            if not repository.claim(proposal.id, self.principal.actor_id):
                raise ActionConflict("Execution is not approved")
            if isinstance(proposal.content, ChangeApplication):
                repository.transition_application(
                    proposal.application_id, proposal.content.target_status
                )
                repository.finish(
                    ActionOutcome(
                        proposal_id=proposal.id,
                        created_at=self.clock(),
                        status="applied",
                        code="record_updated",
                    ),
                    self.principal.actor_id,
                )
                return self._review(repository, proposal)
        # A simulation receipt is explicit: it must never mark an application submitted.
        status: str
        receipt_id = None
        try:
            if self.adapter is None:
                raise DefiniteAdapterFailure("No adapter configured")
            receipt = AdapterReceipt.model_validate(
                self.adapter.execute(
                    proposal,
                    f"action:{proposal.id}:{proposal.content_sha256}",
                    timeout_seconds=remaining_timeout(30),
                ).model_dump()
            )
            status, code, receipt_id = "simulated", "simulated_effect", receipt.receipt_id
        except DefiniteAdapterFailure:
            status, code = "failed", "adapter_failure"
        except Exception:
            # Timeout, malformed receipt or transport error can occur after an effect.
            # Do not expose raw provider text or retry automatically.
            status, code = "unknown", "ambiguous_execution"
        with self.database.transaction() as repository:
            if repository.outcome(proposal.id) is None:
                repository.finish(
                    ActionOutcome.model_validate(
                        {
                            "proposal_id": proposal.id,
                            "created_at": self.clock(),
                            "status": status,
                            "code": code,
                            "receipt_id": receipt_id,
                        }
                    ),
                    self.principal.actor_id,
                )
            return self._review(repository, proposal)

    @staticmethod
    def verify_exact_event(proposal: ActionProposal, event: ActionDecision) -> None:
        if (
            event.proposal_id != proposal.id
            or event.owner_id != proposal.owner_id
            or event.thread_id != proposal.thread_id
            or event.proposal_version != proposal.version
            or event.content_sha256 != proposal.content_sha256
            or event.created_at < proposal.created_at
            or (event.decision == "approve" and event.created_at >= proposal.expires_at)
        ):
            raise ActionDenied("Approval binding is invalid")
