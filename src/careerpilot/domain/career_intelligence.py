"Pure evidence catalogs, targeted questions and explainable rule results."

from uuid import UUID, uuid5

from careerpilot.domain.baseline import (
    BaselineAssessment,
    CriterionKind,
    FindingReason,
    NormalizedCriterion,
)
from careerpilot.domain.common import Evidence, EvidenceOrigin
from careerpilot.domain.jobs import JobPosting, RequirementOutcome
from careerpilot.domain.preparation import (
    CareerIntelligence,
    Clarification,
    ExplainedRequirement,
    FactKind,
    InterviewQuestion,
    PracticePlan,
    PracticeSession,
    ProfileFact,
)
from careerpilot.domain.profile import CandidateProfile


def _user(evidence: tuple[Evidence, ...]) -> bool:
    return any(item.origin == EvidenceOrigin.USER_STATEMENT for item in evidence)


def profile_catalog(
    profile: CandidateProfile,
) -> tuple[tuple[ProfileFact, ...], tuple[Clarification, ...]]:
    facts: list[ProfileFact] = []
    questions: list[Clarification] = []

    def add(
        kind: FactKind,
        record_id: UUID | None,
        path: str,
        text: str,
        evidence: tuple[Evidence, ...],
        *,
        confirmed: bool | None = None,
    ) -> None:
        if not evidence:
            return
        trusted = _user(evidence) if confirmed is None else confirmed
        fact = ProfileFact(
            id=uuid5(profile.id, path),
            kind=kind,
            record_id=record_id,
            field_path=path,
            text=text,
            evidence=evidence,
            verification="user_confirmed" if trusted else "unverified",
        )
        facts.append(fact)
        if not trusted:
            questions.append(
                Clarification(
                    id=uuid5(fact.id, "confirm"),
                    reason="unverified_fact",
                    question=(
                        f"Can you confirm the profile statement at {path} "
                        "and supply user-confirmed supporting evidence?"
                    ),
                    fact_ids=(fact.id,),
                )
            )

    if profile.display_name.value is not None:
        add(
            FactKind.NAME,
            None,
            "display_name",
            profile.display_name.value,
            profile.display_name.evidence,
        )
    for skill in profile.skills:
        add(
            FactKind.SKILL,
            skill.id,
            f"skills/{skill.id}/name",
            f"Skill: {skill.name}",
            skill.evidence,
        )
        if skill.proficiency.value is not None:
            add(
                FactKind.SKILL,
                skill.id,
                f"skills/{skill.id}/proficiency",
                f"{skill.name} proficiency: {skill.proficiency.value.value}",
                tuple(
                    {
                        item.id: item for item in (*skill.evidence, *skill.proficiency.evidence)
                    }.values()
                ),
                confirmed=_user(skill.evidence) and _user(skill.proficiency.evidence),
            )
    for language in profile.languages:
        add(
            FactKind.LANGUAGE,
            language.id,
            f"languages/{language.id}/name",
            f"Language: {language.language}",
            language.evidence,
        )
        if language.proficiency.value is not None:
            add(
                FactKind.LANGUAGE,
                language.id,
                f"languages/{language.id}/proficiency",
                f"{language.language} level: {language.proficiency.value.value}",
                tuple(
                    {
                        item.id: item
                        for item in (*language.evidence, *language.proficiency.evidence)
                    }.values()
                ),
                confirmed=_user(language.evidence) and _user(language.proficiency.evidence),
            )
    for entry in profile.experience:
        add(
            FactKind.EXPERIENCE,
            entry.id,
            f"experience/{entry.id}",
            f"{entry.title} | {entry.employer} | {entry.started_on.isoformat()} to "
            + (entry.ended_on.isoformat() if entry.ended_on else "end date not provided"),
            entry.evidence,
        )
    for entry_education in profile.education:
        add(
            FactKind.EDUCATION,
            entry_education.id,
            f"education/{entry_education.id}",
            f"{entry_education.qualification} | {entry_education.institution} | "
            f"{entry_education.started_on.isoformat()} to "
            + (
                entry_education.ended_on.isoformat()
                if entry_education.ended_on
                else "end date not provided"
            ),
            entry_education.evidence,
        )
    for certificate in profile.certifications:
        add(
            FactKind.CERTIFICATION,
            certificate.id,
            f"certifications/{certificate.id}",
            f"{certificate.name} | {certificate.issuer} | issued "
            f"{certificate.issued_on.isoformat()}"
            + (
                f" | expires {certificate.expires_on.isoformat()}"
                if certificate.expires_on
                else " | expiry not provided"
            ),
            certificate.evidence,
        )
    if profile.work_authorized.value is not None:
        add(
            FactKind.AUTHORIZATION,
            None,
            "work_authorized",
            f"Declared work authorization: {str(profile.work_authorized.value).lower()}",
            profile.work_authorized.evidence,
        )
    # Same role/institution and start with different ends is ambiguous.
    # It is not proof of dishonesty.
    conflicts: set[UUID] = set()
    periods: dict[tuple[str, ...], list[tuple[UUID, str]]] = {}
    for work in profile.experience:
        periods.setdefault(
            ("work", work.employer.casefold(), work.title.casefold(), str(work.started_on)), []
        ).append((work.id, str(work.ended_on)))
    for education in profile.education:
        periods.setdefault(
            (
                "education",
                education.institution.casefold(),
                education.qualification.casefold(),
                str(education.started_on),
            ),
            [],
        ).append((education.id, str(education.ended_on)))
    for entries in periods.values():
        if len({end for _, end in entries if end != "None"}) > 1:
            conflicts.update(identifier for identifier, _ in entries)
    if conflicts:
        facts = [
            ProfileFact.model_validate({**fact.model_dump(), "verification": "conflicting"})
            if fact.record_id in conflicts
            else fact
            for fact in facts
        ]
        linked = tuple(fact.id for fact in facts if fact.record_id in conflicts)
        questions.append(
            Clarification(
                id=uuid5(profile.id, "conflicting-dates"),
                reason="conflicting_dates",
                question=(
                    "The same role or qualification/start date has different "
                    "end dates. Which dates are correct, or are these separate "
                    "engagements? Supply evidence; overlapping work alone is "
                    "not an error."
                ),
                fact_ids=linked,
                blocking=True,
            )
        )
    if len(facts) > 100:
        raise ValueError("Preparation supports at most 100 scoped profile facts")
    return tuple(facts), tuple(questions)


def _related(
    profile: CandidateProfile,
    facts: tuple[ProfileFact, ...],
    criterion: NormalizedCriterion,
    baseline: BaselineAssessment,
) -> tuple[UUID, ...]:
    definition = criterion.definition
    if definition is None:
        return ()
    records: set[UUID] = set()
    if definition.kind == CriterionKind.SKILL:
        records = {
            item.id for item in profile.skills if baseline.rules.name(item.name) == definition.name
        }
    elif definition.kind == CriterionKind.LANGUAGE:
        records = {
            item.id
            for item in profile.languages
            if baseline.rules.name(item.language) == definition.name
        }
    if definition.kind == CriterionKind.WORK_AUTHORIZATION:
        return tuple(item.id for item in facts if item.kind == FactKind.AUTHORIZATION)
    return tuple(item.id for item in facts if item.record_id in records)


_EXPLANATIONS = {
    FindingReason.SUPPORTED: (
        "The declared profile evidence supports the configured requirement rule."
    ),
    FindingReason.BELOW_MINIMUM: (
        "The evidenced declared level is below the configured "
        "minimum; this is a stated-data gap, not a competence "
        "judgment."
    ),
    FindingReason.EXPLICIT_NEGATIVE: (
        "The evidenced declared value differs from the stated "
        "requirement; no legal or hiring conclusion is inferred."
    ),
    FindingReason.NOT_RECORDED: (
        "No matching profile fact is recorded. Absence is unknown, "
        "not proof the candidate lacks the skill."
    ),
    FindingReason.UNVERIFIED: (
        "Available claims are unverified under the assessment evidence policy."
    ),
    FindingReason.UNKNOWN_VALUE: "The relevant value is missing or explicitly unknown.",
    FindingReason.CONFLICTING: (
        "The declared evidence conflicts; clarification is "
        "required without choosing a favorable version."
    ),
    FindingReason.UNSUPPORTED: (
        "This criterion is outside supported business rules. No candidate judgment is made."
    ),
    FindingReason.MISSING_DEFINITION: (
        "No deterministic rule definition is available; the requirement remains unknown."
    ),
    FindingReason.NOT_COMPARABLE: (
        "These labels cannot be compared by the configured rule; do not assume equivalence."
    ),
}


def build_intelligence(
    job: JobPosting,
    profile: CandidateProfile,
    baseline: BaselineAssessment,
    criteria: tuple[NormalizedCriterion, ...],
) -> CareerIntelligence:
    facts, initial_questions = profile_catalog(profile)
    questions = list(initial_questions)
    blocking: list[str] = []
    if not any(
        item.kind == FactKind.NAME and item.verification == "user_confirmed" for item in facts
    ):
        blocking.append("A user-confirmed display name is required")
        questions.append(
            Clarification(
                id=uuid5(profile.id, "name"),
                reason="missing_identity",
                question="What name should appear on your draft? Confirm it explicitly.",
                blocking=True,
            )
        )
    if not any(
        item.kind not in {FactKind.NAME, FactKind.AUTHORIZATION}
        and item.verification == "user_confirmed"
        for item in facts
    ):
        blocking.append("At least one user-confirmed professional fact is required")
        questions.append(
            Clarification(
                id=uuid5(profile.id, "professional-fact"),
                reason="missing_history",
                blocking=True,
                question=(
                    "Which genuine professional skill or career record can you "
                    "confirm with evidence? No experience, education or "
                    "achievement will be invented."
                ),
            )
        )
    if any(item.reason == "conflicting_dates" for item in questions):
        blocking.append("Resolve the conflicting career dates before generating documents")
    if not profile.experience and not profile.education:
        questions.append(
            Clarification(
                id=uuid5(profile.id, "history"),
                reason="missing_history",
                question=(
                    "No employment or education history was supplied. Would "
                    "you like to add evidence-backed history? It will not be "
                    "invented."
                ),
            )
        )
    explained: list[ExplainedRequirement] = []
    interview: list[InterviewQuestion] = []
    for requirement, finding, criterion in zip(
        job.requirements, baseline.findings, criteria, strict=True
    ):
        linked = _related(profile, facts, criterion, baseline)
        explained.append(
            ExplainedRequirement(
                finding=finding,
                source_description=requirement.description,
                explanation=_EXPLANATIONS[finding.reason],
                fact_ids=linked,
            )
        )
        if finding.outcome != RequirementOutcome.MET:
            unsupported = finding.reason in {
                FindingReason.UNSUPPORTED,
                FindingReason.MISSING_DEFINITION,
            }
            question = (
                (
                    "This requirement is outside supported rules. Review "
                    "whether it is job-related; do not provide protected "
                    "personal details."
                )
                if unsupported
                else (
                    f"For requirement {requirement.id}, which current profile fact or "
                    "user-provided evidence confirms the stated value/level? If unknown, say so."
                )
            )
            questions.append(
                Clarification(
                    id=uuid5(requirement.id, "clarify"),
                    reason="unsupported_requirement"
                    if unsupported
                    else (
                        "requirement_gap"
                        if finding.outcome == RequirementOutcome.UNMET
                        else "requirement_unknown"
                    ),
                    question=question,
                    requirement_id=requirement.id,
                    fact_ids=linked,
                )
            )
        category = "evidence_review"
        prompt = (
            "Review this source requirement for job relevance and "
            "clarify unsupported assumptions without judging personal "
            "traits."
        )
        if criterion.definition is not None and criterion.definition.kind == CriterionKind.SKILL:
            category = "technical"
            prompt = (
                "Work through a small task involving the skill in this "
                "linked requirement. Explain design choices, tests and "
                "limitations; label hypothetical examples clearly."
            )
        elif (
            criterion.definition is not None and criterion.definition.kind == CriterionKind.LANGUAGE
        ):
            category = "language"
            prompt = (
                "Practice a job-related explanation using the language in "
                "this linked requirement. Do not claim a proficiency level "
                "without evidence."
            )
        interview.append(
            InterviewQuestion.model_validate(
                {
                    "id": uuid5(requirement.id, "interview"),
                    "requirement_id": requirement.id,
                    "category": category,
                    "prompt": prompt,
                }
            )
        )
    interview.append(
        InterviewQuestion(
            id=uuid5(job.id, "behavioral"),
            requirement_id=None,
            category="behavioral",
            prompt=(
                "Describe a task you actually completed: context, your "
                "role, choices and evidenced outcome. If none is "
                "available, use a clearly labeled hypothetical exercise; "
                "never invent metrics."
            ),
        )
    )
    ids = tuple(item.id for item in interview)
    practice = PracticePlan(
        questions=tuple(interview),
        sessions=(
            PracticeSession(
                order=1,
                duration_minutes=20,
                focus="Evidence and clarification review",
                question_ids=ids,
                deliverable="List confirmed examples, genuine gaps and unanswered questions.",
            ),
            PracticeSession(
                order=2,
                duration_minutes=30,
                focus=(
                    "Job-relevant technical and language practice"
                    if any(item.category == "language" for item in interview)
                    else "Job-relevant technical practice"
                ),
                question_ids=ids,
                deliverable=(
                    "A small practice solution or explanation with assumptions clearly marked."
                ),
            ),
            PracticeSession(
                order=3,
                duration_minutes=25,
                focus="Mock interview and reflection",
                question_ids=ids,
                deliverable=(
                    "Evidence-backed answers and a revised practice checklist, "
                    "not invented career claims."
                ),
            ),
        ),
    )
    return CareerIntelligence(
        candidate_id=profile.id,
        job_id=job.id,
        assessment_id=baseline.assessment.id,
        decision=baseline.assessment.decision(job),
        counts=baseline.counts,
        rules=baseline.rules,
        criteria=criteria,
        facts=facts,
        explanations=tuple(explained),
        clarifications=tuple(questions),
        practice=practice,
        blocking_reasons=tuple(blocking),
    )
