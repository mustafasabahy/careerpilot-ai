"""Sensitive-action storage shares business transactions, not checkpoint storage."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID, uuid5

from careerpilot.application.action_contracts import ActionConflict, ActionDenied, ActionRepository
from careerpilot.domain.actions import (
    ActionAudit,
    ActionDecision,
    ActionLifecycle,
    ActionOutcome,
    ActionProposal,
    ActionStatus,
)
from careerpilot.persistence.sqlite import SQLiteDatabase, SQLiteRepository


class SQLiteActionDatabase:
    def __init__(self, business: SQLiteDatabase) -> None:
        self.business = business

    @contextmanager
    def transaction(self) -> Iterator[ActionRepository]:
        # Reuse the same commit/rollback/schema checks and connection as domain writes.
        with self.business.transaction() as repository:
            if not isinstance(repository, SQLiteRepository):
                raise TypeError("SQLite action adapter requires SQLite business storage")
            yield SQLiteActionRepository(repository._connection)


class SQLiteActionRepository(SQLiteRepository):
    def proposal(self, owner_id: UUID, identifier: UUID) -> ActionProposal:
        row = self._connection.execute(
            "SELECT payload FROM action_proposals WHERE owner_id=? AND id=?",
            (str(owner_id), str(identifier)),
        ).fetchone()
        if row is None:
            raise ActionDenied("Proposal is unavailable within the user's scope")
        return ActionProposal.model_validate_json(row[0])

    def thread_proposal(self, owner_id: UUID, thread_id: UUID) -> ActionProposal:
        row = self._connection.execute(
            "SELECT payload FROM action_proposals WHERE owner_id=? AND thread_id=?",
            (str(owner_id), str(thread_id)),
        ).fetchone()
        if row is None:
            raise ActionDenied("Approval thread is unavailable within the user's scope")
        return ActionProposal.model_validate_json(row[0])

    def _audit_action(self, proposal_id: UUID, actor_id: UUID, action: str) -> None:
        event = ActionAudit.model_validate(
            {
                "id": uuid5(proposal_id, f"action-audit:{action}"),
                "proposal_id": proposal_id,
                "actor_id": actor_id,
                "action": action,
            }
        )
        self._connection.execute(
            "INSERT INTO action_audits(id,proposal_id,payload) VALUES (?,?,?)",
            (str(event.id), str(proposal_id), event.model_dump_json()),
        )

    def add_proposal(self, proposal: ActionProposal, actor_id: UUID) -> None:
        proposal = ActionProposal.model_validate(proposal.model_dump())
        with self._write():
            try:
                if proposal.previous_proposal_id is not None:
                    previous = self.proposal(proposal.owner_id, proposal.previous_proposal_id)
                    if (
                        self.status(previous.id) != "revision_requested"
                        or previous.version + 1 != proposal.version
                        or previous.application_id != proposal.application_id
                        or previous.content.kind != proposal.content.kind
                    ):
                        raise ActionConflict("Invalid proposal revision lineage")
                self._connection.execute(
                    "INSERT INTO action_proposals(id,thread_id,owner_id,application_id,"
                    "candidate_id,"
                    "version,previous_proposal_id,content_hash,payload) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        str(proposal.id),
                        str(proposal.thread_id),
                        str(proposal.owner_id),
                        str(proposal.application_id),
                        str(proposal.candidate_id),
                        proposal.version,
                        str(proposal.previous_proposal_id)
                        if proposal.previous_proposal_id
                        else None,
                        proposal.content_sha256,
                        proposal.model_dump_json(),
                    ),
                )
                self._connection.execute(
                    "INSERT INTO action_states(proposal_id,status) VALUES (?,'pending')",
                    (str(proposal.id),),
                )
                self._audit_action(proposal.id, actor_id, "proposed")
            except sqlite3.IntegrityError:
                raise ActionConflict("Proposal ID, thread or revision already exists") from None

    def status(self, proposal_id: UUID) -> ActionStatus:
        row = self._connection.execute(
            "SELECT status FROM action_states WHERE proposal_id=?", (str(proposal_id),)
        ).fetchone()
        if row is None:
            raise ActionConflict("Missing action lifecycle state")
        return ActionLifecycle.model_validate({"status": row[0]}).status

    def decision(self, proposal_id: UUID) -> ActionDecision | None:
        row = self._connection.execute(
            "SELECT payload FROM action_decisions WHERE proposal_id=?", (str(proposal_id),)
        ).fetchone()
        return ActionDecision.model_validate_json(row[0]) if row else None

    def outcome(self, proposal_id: UUID) -> ActionOutcome | None:
        row = self._connection.execute(
            "SELECT payload FROM action_outcomes WHERE proposal_id=?", (str(proposal_id),)
        ).fetchone()
        return ActionOutcome.model_validate_json(row[0]) if row else None

    def decide(self, event: ActionDecision) -> None:
        event = ActionDecision.model_validate(event.model_dump())
        with self._write():
            if self.status(event.proposal_id) != "pending":
                raise ActionConflict("Action has already been reviewed")
            try:
                self._connection.execute(
                    "INSERT INTO action_decisions(id,proposal_id,thread_id,owner_id,version,"
                    "content_hash,payload) VALUES (?,?,?,?,?,?,?)",
                    (
                        str(event.id),
                        str(event.proposal_id),
                        str(event.thread_id),
                        str(event.owner_id),
                        event.proposal_version,
                        event.content_sha256,
                        event.model_dump_json(),
                    ),
                )
                target = {
                    "approve": "approved",
                    "reject": "rejected",
                    "revise": "revision_requested",
                }[event.decision]
                self._connection.execute(
                    "UPDATE action_states SET status=? WHERE proposal_id=?",
                    (target, str(event.proposal_id)),
                )
                self._audit_action(event.proposal_id, event.actor_id, target)
            except sqlite3.IntegrityError:
                raise ActionConflict("Decision ID or exact-version binding conflicts") from None

    def claim(self, proposal_id: UUID, actor_id: UUID) -> bool:
        with self._write():
            if self.status(proposal_id) != "approved":
                return False
            row = self._connection.execute(
                "SELECT content_hash FROM action_proposals WHERE id=?", (str(proposal_id),)
            ).fetchone()
            self._connection.execute(
                "INSERT INTO action_attempts(proposal_id,idempotency_key) VALUES (?,?)",
                (str(proposal_id), f"action:{proposal_id}:{row[0]}"),
            )
            self._connection.execute(
                "UPDATE action_states SET status='executing' WHERE proposal_id=?",
                (str(proposal_id),),
            )
            self._audit_action(proposal_id, actor_id, "execution_claimed")
            return True

    def finish(self, outcome: ActionOutcome, actor_id: UUID) -> None:
        outcome = ActionOutcome.model_validate(outcome.model_dump())
        with self._write():
            current = self.status(outcome.proposal_id)
            if current != "executing" and not (
                current == "approved" and outcome.status == "invalidated"
            ):
                raise ActionConflict("Outcome does not follow an authorized execution")
            self._connection.execute(
                "INSERT INTO action_outcomes(id,proposal_id,payload) VALUES (?,?,?)",
                (str(outcome.id), str(outcome.proposal_id), outcome.model_dump_json()),
            )
            self._connection.execute(
                "UPDATE action_states SET status=? WHERE proposal_id=?",
                (outcome.status, str(outcome.proposal_id)),
            )
            self._audit_action(outcome.proposal_id, actor_id, outcome.status)

    def audit(self, proposal_id: UUID) -> tuple[ActionAudit, ...]:
        return tuple(
            ActionAudit.model_validate_json(row[0])
            for row in self._connection.execute(
                "SELECT payload FROM action_audits WHERE proposal_id=? ORDER BY rowid",
                (str(proposal_id),),
            )
        )
