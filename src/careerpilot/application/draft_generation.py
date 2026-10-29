"""Deterministic validation/rendering around an exchangeable fact-selection planner."""

import json

from pydantic import BaseModel, ValidationError

from careerpilot.application.preparation_contracts import PlanningInput, PreparationValidationError
from careerpilot.domain.jobs import JobPosting
from careerpilot.domain.preparation import CareerIntelligence, DraftPlan, FactKind


def planning_input(intelligence: CareerIntelligence) -> PlanningInput:
    related = {identifier for item in intelligence.explanations for identifier in item.fact_ids}
    allowed = tuple(
        fact
        for fact in intelligence.facts
        if fact.verification == "user_confirmed"
        and fact.kind not in {FactKind.NAME, FactKind.AUTHORIZATION}
    )
    # Unrequested language is not made a job requirement or added by default.
    preferred = tuple(fact.id for fact in allowed if fact.id in related)
    return PlanningInput(facts=allowed, preferred_fact_ids=preferred)


def validate_plan(raw: object, request: PlanningInput) -> DraftPlan:
    try:
        if isinstance(raw, BaseModel):
            raw = raw.model_dump(warnings=False)
        plan = (
            DraftPlan.model_validate_json(raw)
            if isinstance(raw, (str, bytes))
            else DraftPlan.model_validate(raw)
        )
    except ValidationError:
        raise PreparationValidationError(
            "Draft plan must contain only valid existing fact references"
        ) from None
    allowed = {fact.id for fact in request.facts}
    if not set(plan.resume_fact_ids).issubset(allowed) or not set(plan.cover_fact_ids).issubset(
        allowed
    ):
        raise PreparationValidationError(
            "Draft contains an unsupported, unverified or unknown claim reference"
        )
    return plan


def render_drafts(
    job: JobPosting, intelligence: CareerIntelligence, plan: DraftPlan
) -> tuple[str, str]:
    plan = validate_plan(plan, planning_input(intelligence))
    facts = {fact.id: fact for fact in intelligence.facts}
    name = next(
        (
            fact.text
            for fact in intelligence.facts
            if fact.kind == FactKind.NAME and fact.verification == "user_confirmed"
        ),
        None,
    )
    if name is None:
        raise PreparationValidationError("No user-confirmed identity is available")
    if job.id != intelligence.job_id:
        raise PreparationValidationError("Document target differs from the assessed job")
    title = json.dumps(job.title, ensure_ascii=False)
    employer = json.dumps(job.employer, ensure_ascii=False)
    resume = "\n".join(
        [
            "CV DRAFT — review required",
            name,
            f"Target role from job source: {title} | {employer}",
            "",
            "User-confirmed profile statements:",
            *(f"- {facts[identifier].text}" for identifier in plan.resume_fact_ids),
            "",
            "Only cited profile facts are included. Missing/unverified claims were not filled in.",
        ]
    )
    cover = "\n".join(
        [
            "COVER LETTER DRAFT — review required",
            "Dear hiring team,",
            "",
            "I am preparing an application for the role identified below.",
            f"Role title from job source: {title}",
            f"Employer from job source: {employer}",
            "The following statements from my profile are relevant for your review:",
            *(f"- {facts[identifier].text}" for identifier in plan.cover_fact_ids),
            "",
            "I would welcome a discussion of the role and the evidence behind these statements.",
            "Sincerely,",
            name,
        ]
    )
    return resume, cover
