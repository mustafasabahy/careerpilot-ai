"""Composition adapter: conversation commands delegate to the authorized Portal."""

from typing import Literal
from uuid import UUID, uuid5

from careerpilot.application.analysis import AnalysisRequest
from careerpilot.application.conversations import (
    Card,
    Choice,
    Command,
    Conversation,
    Operation,
    Reference,
    Resource,
    Selection,
    localized,
)
from careerpilot.application.preparation_contracts import PreparationRequest
from careerpilot.domain.baseline import BaselineAssessment
from careerpilot.domain.career_intelligence import build_intelligence
from careerpilot.domain.jobs import JobFitAssessment, JobPosting
from careerpilot.domain.profile import CandidateProfile
from careerpilot.portal import Portal


class PortalChatBackend:
    def __init__(self, portal: Portal) -> None:
        self.portal = portal

    def require(self, owner: UUID, kind: Resource, identifier: UUID) -> None:
        self.portal.security.require(owner, kind, identifier)
        if kind == "job":
            self.portal.get(owner, kind, JobPosting, identifier)
        else:
            self.portal.get(owner, kind, CandidateProfile, identifier)

    def choices(self, owner: UUID, kind: Resource, offset: int, limit: int) -> tuple[Choice, ...]:
        identifiers = self.portal.security.list(owner, kind, offset=offset, limit=limit)
        result = []
        for identifier in identifiers:
            if kind == "job":
                job = self.portal.get(owner, kind, JobPosting, identifier)
                label = f"{job.title} · {job.employer} · {str(job.id)[:8]}"
            else:
                profile = self.portal.get(owner, kind, CandidateProfile, identifier)
                label = f"{profile.display_name.value or 'Profile'} · {str(profile.id)[:8]}"
            result.append(Choice(selection=Selection(kind=kind, id=identifier), label=label))
        return tuple(result)

    def execute(
        self, owner: UUID, command: Command, context: Conversation, operation: Operation
    ) -> tuple[Card, UUID | None]:
        p = self.portal
        if command == "help":
            return Card(kind="help", text="Demo mode"), None
        if command == "jobs":
            choices = self.choices(owner, "job", operation.request.offset, 20)
            return Card(
                kind="jobs",
                text=localized(
                    context,
                    "Stored local jobs. Send “Analyze job” to select one. No external search.",
                    "",
                ),
                choices=choices,
                payload={
                    "offset": operation.request.offset,
                    "next_offset": operation.request.offset + 20 if len(choices) == 20 else None,
                },
            ), None
        if command in {"applications", "approvals"}:
            kind: Literal["application", "thread"] = (
                "application" if command == "applications" else "thread"
            )
            identifiers = p.security.list(owner, kind, offset=operation.request.offset, limit=20)
            refs = tuple(Reference(kind=kind, id=i) for i in identifiers)
            card = Card(
                kind=command,
                text=localized(
                    context,
                    "Tracked applications."
                    if command == "applications"
                    else "Approval workflow states. Decisions require explicit controls.",
                    "" if command == "applications" else "",
                ),
                references=refs,
                payload={
                    "offset": operation.request.offset,
                    "next_offset": operation.request.offset + 20
                    if len(identifiers) == 20
                    else None,
                },
            )
            return self.refresh(owner, card), None
        assert context.job_id is not None and context.candidate_id is not None
        self.require(owner, "job", context.job_id)
        self.require(owner, "candidate", context.candidate_id)
        request = AnalysisRequest(
            id=uuid5(operation.id, "analysis"),
            created_at=operation.created_at,
            job_id=context.job_id,
            candidate_id=context.candidate_id,
        )
        analysis = p.analyze(owner, request)
        if analysis.status == "error":
            return Card(kind="error", text="Analysis failed: " + analysis.code), None
        job = p.get(owner, "job", JobPosting, context.job_id)
        profile = p.get(owner, "candidate", CandidateProfile, context.candidate_id)
        refs = (
            Reference(kind="job", id=job.id),
            Reference(kind="candidate", id=profile.id),
            Reference(kind="assessment", id=analysis.assessment_id),
        )
        if command == "prepare":
            result = p.prepare(
                owner,
                PreparationRequest(
                    id=uuid5(operation.id, "preparation"),
                    created_at=operation.created_at,
                    candidate_id=profile.id,
                    job_id=job.id,
                    assessment_id=analysis.assessment_id,
                ),
            )
            if result.status == "drafts_created":
                refs += (Reference(kind="preparation", id=result.record.id),)
                refs += tuple(
                    Reference(kind="version", id=d.version_id) for d in result.record.documents
                )
            return Card(
                kind="preparation",
                text=localized(
                    context,
                    "Preparation result: "
                    + result.status
                    + ". Review all drafts; nothing was submitted.",
                    "",
                ),
                references=refs,
                payload=result.model_dump(mode="json"),
            ), analysis.assessment_id
        payload: dict[str, object] = {
            "analysis": analysis.model_dump(mode="json"),
            "job": job.model_dump(mode="json"),
            "profile": profile.model_dump(mode="json"),
        }
        if command == "missing":
            stored = p.get(owner, "assessment", JobFitAssessment, analysis.assessment_id)
            intelligence = build_intelligence(
                job,
                profile,
                BaselineAssessment(
                    assessment=stored, rules=analysis.rules, findings=analysis.findings
                ),
                analysis.criteria,
            )
            payload["clarifications"] = [
                q.model_dump(mode="json") for q in intelligence.clarifications
            ]
        return Card(
            kind="missing" if command == "missing" else "analysis",
            text=localized(
                context,
                "Missing/unverified requirements are marked unknown. Gaps are shown separately."
                if command == "missing"
                else "Evidence-based rule comparison; not a hiring prediction.",
                "" if command == "missing" else "",
            ),
            references=refs,
            payload=payload,
        ), analysis.assessment_id

    def refresh(self, owner: UUID, card: Card) -> Card:
        from careerpilot.domain.applications import JobApplication

        if card.kind in {"selection", "jobs"}:
            available = []
            for choice in card.choices:
                try:
                    self.require(owner, choice.selection.kind, choice.selection.id)
                except (PermissionError, LookupError):
                    continue
                available.append(choice)
            return card.model_copy(update={"choices": tuple(available)})

        if card.kind == "applications":
            apps = [
                self.portal.get(owner, "application", JobApplication, ref.id).model_dump(
                    mode="json"
                )
                for ref in card.references
                if ref.kind == "application"
            ]
            return card.model_copy(update={"payload": {**card.payload, "items": apps}})
        if card.kind == "approvals":
            values = []
            for ref in card.references:
                if ref.kind == "thread":
                    with self.portal.approval(
                        self.portal.thread_service(owner, ref.id)
                    ) as workflow:
                        values.append(workflow.current(ref.id).model_dump(mode="json"))
            return card.model_copy(update={"payload": {**card.payload, "items": values}})
        for ref in card.references:
            self.portal.security.require(owner, ref.kind, ref.id)
        return card
