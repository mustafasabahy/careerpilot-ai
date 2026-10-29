"Evidence-linked intelligence, constrained draft plans and immutable preparation records."

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from careerpilot.domain.applications import DocumentKind
from careerpilot.domain.baseline import (
    BaselineCounts,
    BaselineRules,
    NormalizedCriterion,
    RequirementFinding,
)
from careerpilot.domain.common import Entity, Evidence, Model, Text
from careerpilot.domain.jobs import FitDecision


class FactKind(StrEnum):
    NAME = "name"
    SKILL = "skill"
    LANGUAGE = "language"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    CERTIFICATION = "certification"
    AUTHORIZATION = "authorization"


class ProfileFact(Model):
    id: UUID
    kind: FactKind
    record_id: UUID | None
    field_path: Text
    text: str = Field(min_length=1, max_length=6500)
    verification: Literal["user_confirmed", "unverified", "conflicting"]
    evidence: tuple[Evidence, ...] = Field(min_length=1, max_length=8)


class Clarification(Model):
    id: UUID
    reason: Literal[
        "missing_identity",
        "missing_history",
        "unverified_fact",
        "conflicting_dates",
        "requirement_unknown",
        "requirement_gap",
        "unsupported_requirement",
    ]
    question: Text
    requirement_id: UUID | None = None
    fact_ids: tuple[UUID, ...] = ()
    blocking: bool = False


class ExplainedRequirement(Model):
    finding: RequirementFinding
    source_description: Text
    explanation: Text
    fact_ids: tuple[UUID, ...]


class InterviewQuestion(Model):
    id: UUID
    requirement_id: UUID | None
    category: Literal["technical", "language", "evidence_review", "behavioral"]
    prompt: Text


class PracticeSession(Model):
    order: int = Field(ge=1, le=10)
    duration_minutes: int = Field(ge=5, le=120)
    focus: Text
    question_ids: tuple[UUID, ...]
    deliverable: Text


class PracticePlan(Model):
    questions: tuple[InterviewQuestion, ...]
    sessions: tuple[PracticeSession, ...]

    @model_validator(mode="after")
    def references_exist(self) -> Self:
        ids = {item.id for item in self.questions}
        if len(ids) != len(self.questions):
            raise ValueError("Interview question IDs must be unique")
        if any(not set(session.question_ids).issubset(ids) for session in self.sessions):
            raise ValueError("Practice session references an absent question")
        return self


class CareerIntelligence(Model):
    candidate_id: UUID
    job_id: UUID
    assessment_id: UUID
    decision: FitDecision
    counts: BaselineCounts
    rules: BaselineRules
    criteria: tuple[NormalizedCriterion, ...]
    facts: tuple[ProfileFact, ...] = Field(max_length=100)
    explanations: tuple[ExplainedRequirement, ...]
    clarifications: tuple[Clarification, ...]
    practice: PracticePlan
    blocking_reasons: tuple[Text, ...]
    interpretation: Literal[
        "Declared evidence comparison, not a hiring prediction or verified competence judgment"
    ] = "Declared evidence comparison, not a hiring prediction or verified competence judgment"

    @model_validator(mode="after")
    def coherent_references(self) -> Self:
        ids = {fact.id for fact in self.facts}
        if len(ids) != len(self.facts):
            raise ValueError("Profile fact IDs must be unique")
        requirement_ids = tuple(item.requirement_id for item in self.criteria)
        if tuple(item.finding.requirement_id for item in self.explanations) != requirement_ids:
            raise ValueError("Explanations must cover the exact normalized requirements")
        if any(not set(item.fact_ids).issubset(ids) for item in self.explanations):
            raise ValueError("Explanation refers to an absent profile fact")
        for question in self.clarifications:
            if not set(question.fact_ids).issubset(ids):
                raise ValueError("Clarification refers to an absent profile fact")
            if (
                question.requirement_id is not None
                and question.requirement_id not in requirement_ids
            ):
                raise ValueError("Clarification refers to another job's requirement")
        if any(
            question.requirement_id is not None and question.requirement_id not in requirement_ids
            for question in self.practice.questions
        ):
            raise ValueError("Practice refers to another job's requirement")
        return self


class DraftPlan(Model):
    # No free prose/achievements/metrics field exists. A model may select IDs only.
    resume_fact_ids: tuple[UUID, ...] = Field(min_length=1, max_length=30)
    cover_fact_ids: tuple[UUID, ...] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def coherent_selection(self) -> Self:
        if len(set(self.resume_fact_ids)) != len(self.resume_fact_ids):
            raise ValueError("Resume references must be unique")
        if len(set(self.cover_fact_ids)) != len(self.cover_fact_ids):
            raise ValueError("Cover-letter references must be unique")
        if not set(self.cover_fact_ids).issubset(self.resume_fact_ids):
            raise ValueError("Cover-letter claims must also be in the resume selection")
        return self


class DraftMetadata(Model):
    kind: DocumentKind
    document_id: UUID
    version_id: UUID
    previous_version_id: UUID | None
    fact_ids: tuple[UUID, ...]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_required: Literal[True] = True


class PreparationRecord(Entity):
    candidate_id: UUID
    job_id: UUID
    assessment_id: UUID
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_resume_version_id: UUID | None
    planner_provider: Text
    planner_model: Text
    renderer_version: Literal["fact-render-v1"] = "fact-render-v1"
    planner_prompt_version: Literal["fact-selection-v1"] = "fact-selection-v1"
    intelligence: CareerIntelligence
    documents: tuple[DraftMetadata, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def linked_metadata(self) -> Self:
        if (self.candidate_id, self.job_id, self.assessment_id) != (
            self.intelligence.candidate_id,
            self.intelligence.job_id,
            self.intelligence.assessment_id,
        ):
            raise ValueError("Intelligence belongs to another candidate/job/assessment")
        if {item.kind for item in self.documents} != {
            DocumentKind.RESUME,
            DocumentKind.COVER_LETTER,
        }:
            raise ValueError("A preparation requires one resume and one cover letter")
        facts = {item.id: item for item in self.intelligence.facts}
        for document in self.documents:
            if any(
                identifier not in facts or facts[identifier].verification != "user_confirmed"
                for identifier in document.fact_ids
            ):
                raise ValueError("Document metadata includes unsupported/unverified claims")
        return self
