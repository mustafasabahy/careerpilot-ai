"""Baseline business rules are tested without importing LangGraph."""

from uuid import uuid4

import pytest
from pydantic import StrictBool, ValidationError

from careerpilot.domain.baseline import (
    BaselineRules,
    CriteriaError,
    CriterionDefinition,
    CriterionKind,
    FindingReason,
    Importance,
    NameAlias,
    calculate_baseline,
    normalize_requirements,
)
from careerpilot.domain.common import Claim, Entity, Evidence, EvidenceOrigin, KnowledgeState
from careerpilot.domain.jobs import FitDecision, RequirementOutcome
from careerpilot.domain.profile import (
    CandidateProfile,
    LanguageLevel,
    LanguageProficiency,
    Skill,
    SkillProficiency,
)
from careerpilot.integrations.synthetic import FIXTURE_TIME, synthetic_bundle


def test_complete_baseline_is_repeatable_without_a_graph() -> None:
    bundle = synthetic_bundle()
    job, profile = bundle.jobs[0], bundle.profiles[0]
    criteria = normalize_requirements(job, bundle.definitions[job.id], bundle.rules)
    identity = Entity(id=uuid4(), created_at=FIXTURE_TIME)
    report = calculate_baseline(job, profile, criteria, bundle.rules, identity)
    assert report == calculate_baseline(job, profile, criteria, bundle.rules, identity)
    assert report.assessment.decision(job) == FitDecision.ELIGIBLE
    assert report.assessment.ai_score is None
    assert report.counts.hard_supported == 2
    assert report.counts.soft_supported == 1
    assert not report.needs_information
    assert criteria[0].definition is not None and criteria[0].definition.name == "python"


def test_explicit_hard_gap_is_not_overridden_by_matches() -> None:
    bundle = synthetic_bundle()
    job = bundle.jobs[0]
    criteria = normalize_requirements(job, bundle.definitions[job.id], bundle.rules)
    report = calculate_baseline(
        job, bundle.profiles[1], criteria, bundle.rules, bundle.request("hard-gap")
    )
    assert report.assessment.decision(job) == FitDecision.INELIGIBLE
    assert report.counts.hard_gaps == 1
    assert report.counts.hard_supported == 1
    assert report.findings[1].reason == FindingReason.EXPLICIT_NEGATIVE
    assert not report.needs_information


def test_soft_gaps_unknowns_and_unspecified_importance_are_distinct() -> None:
    bundle = synthetic_bundle()
    job = bundle.jobs[1]
    criteria = normalize_requirements(job, bundle.definitions[job.id], bundle.rules)
    report = calculate_baseline(job, bundle.profiles[0], criteria, bundle.rules, bundle.request())
    assert report.assessment.decision(job) == FitDecision.NEEDS_REVIEW
    assert report.counts.soft_gaps == 1
    assert report.counts.soft_unknown == 1
    assert report.counts.unspecified == 1
    assert report.findings[3].reason == FindingReason.UNVERIFIED
    assert report.findings[4].reason == FindingReason.BELOW_MINIMUM
    assert report.findings[5].reason == FindingReason.UNSUPPORTED
    assert report.needs_information


def test_omitted_skill_is_unknown_instead_of_an_explicit_gap() -> None:
    bundle = synthetic_bundle()
    job = bundle.jobs[0]
    profile = CandidateProfile.model_validate({**bundle.profiles[0].model_dump(), "skills": []})
    criteria = normalize_requirements(job, bundle.definitions[job.id], bundle.rules)
    report = calculate_baseline(job, profile, criteria, bundle.rules, bundle.request())
    assert report.findings[0].outcome == RequirementOutcome.UNKNOWN
    assert report.findings[0].reason == FindingReason.NOT_RECORDED
    assert report.counts.hard_gaps == 0


def test_configurable_aliases_change_matching_without_graph_changes() -> None:
    bundle = synthetic_bundle()
    job = bundle.jobs[0]
    rules = BaselineRules()
    criteria = normalize_requirements(job, bundle.definitions[job.id], rules)
    report = calculate_baseline(job, bundle.profiles[0], criteria, rules, bundle.request())
    assert report.findings[0].reason == FindingReason.NOT_RECORDED


def test_missing_definition_becomes_unknown_not_a_fabricated_requirement() -> None:
    bundle = synthetic_bundle()
    job = bundle.jobs[0]
    criteria = normalize_requirements(job, (), bundle.rules)
    assert all(item.definition is None for item in criteria)
    report = calculate_baseline(job, bundle.profiles[0], criteria, bundle.rules, bundle.request())
    assert all(item.reason == FindingReason.MISSING_DEFINITION for item in report.findings)
    assert report.assessment.decision(job) == FitDecision.NEEDS_REVIEW


def test_duplicate_foreign_or_wrong_category_definitions_fail() -> None:
    bundle = synthetic_bundle()
    job = bundle.jobs[0]
    definition = bundle.definitions[job.id][0]
    invalid = [
        (definition, definition),
        (CriterionDefinition(requirement_id=uuid4(), kind=CriterionKind.UNSUPPORTED),),
        (
            CriterionDefinition(
                requirement_id=definition.requirement_id,
                kind=CriterionKind.WORK_AUTHORIZATION,
                expected=True,
            ),
        ),
    ]
    for definitions in invalid:
        with pytest.raises(CriteriaError):
            normalize_requirements(job, definitions, bundle.rules)


def test_criteria_cannot_omit_reorder_or_downgrade_hard_constraints() -> None:
    bundle = synthetic_bundle()
    job = bundle.jobs[0]
    criteria = normalize_requirements(job, bundle.definitions[job.id], bundle.rules)
    downgraded = criteria[0].model_copy(update={"importance": Importance.SOFT})
    for altered in (criteria[:-1], tuple(reversed(criteria)), (downgraded, *criteria[1:])):
        with pytest.raises(CriteriaError):
            calculate_baseline(job, bundle.profiles[0], altered, bundle.rules, bundle.request())


@pytest.mark.parametrize("origins", [(EvidenceOrigin.AI_INFERENCE,), ()])
def test_policy_cannot_promote_inference_to_fact(origins: tuple[EvidenceOrigin, ...]) -> None:
    with pytest.raises(ValidationError):
        BaselineRules(accepted_origins=origins)


@pytest.mark.parametrize(
    "aliases",
    [
        (NameAlias(alias="PY", canonical="python"), NameAlias(alias="py", canonical="python")),
        (NameAlias(alias="py", canonical="python"), NameAlias(alias="python", canonical="py")),
    ],
)
def test_conflicting_or_chained_aliases_are_rejected(aliases: tuple[NameAlias, ...]) -> None:
    with pytest.raises(ValidationError):
        BaselineRules(aliases=aliases)


def test_conflicting_supported_skill_levels_require_review() -> None:
    bundle = synthetic_bundle()
    job, base = bundle.jobs[0], bundle.profiles[0]
    evidence = base.skills[0].evidence
    low = Skill(
        name="Python",
        evidence=evidence,
        proficiency=Claim[SkillProficiency](
            state=KnowledgeState.KNOWN, value=SkillProficiency.BEGINNER, evidence=evidence
        ),
    )
    profile = CandidateProfile.model_validate({**base.model_dump(), "skills": [*base.skills, low]})
    criteria = normalize_requirements(job, bundle.definitions[job.id], bundle.rules)
    report = calculate_baseline(job, profile, criteria, bundle.rules, bundle.request())
    assert report.findings[0].reason == FindingReason.CONFLICTING
    assert report.findings[0].outcome == RequirementOutcome.UNKNOWN


@pytest.mark.parametrize("state", [KnowledgeState.MISSING, KnowledgeState.UNKNOWN])
def test_missing_or_unknown_authorization_never_becomes_false(state: KnowledgeState) -> None:
    bundle = synthetic_bundle()
    job = bundle.jobs[0]
    profile = CandidateProfile.model_validate(
        {**bundle.profiles[0].model_dump(), "work_authorized": Claim[StrictBool](state=state)}
    )
    criteria = normalize_requirements(job, bundle.definitions[job.id], bundle.rules)
    report = calculate_baseline(job, profile, criteria, bundle.rules, bundle.request())
    assert report.findings[1].outcome == RequirementOutcome.UNKNOWN
    assert report.counts.hard_gaps == 0


def test_document_evidence_can_be_configured_more_strictly() -> None:
    bundle = synthetic_bundle()
    job, base = bundle.jobs[0], bundle.profiles[0]
    evidence = Evidence(origin=EvidenceOrigin.SOURCE_DOCUMENT, reference="fixture:source")
    profile = CandidateProfile.model_validate(
        {
            **base.model_dump(),
            "work_authorized": Claim[StrictBool](
                state=KnowledgeState.KNOWN, value=True, evidence=(evidence,)
            ),
        }
    )
    rules = BaselineRules(
        aliases=bundle.rules.aliases, accepted_origins=(EvidenceOrigin.USER_STATEMENT,)
    )
    report = calculate_baseline(
        job,
        profile,
        normalize_requirements(job, bundle.definitions[job.id], rules),
        rules,
        bundle.request(),
    )
    assert report.findings[1].reason == FindingReason.UNVERIFIED


def test_native_is_not_silently_equated_to_a_cefr_certificate() -> None:
    bundle = synthetic_bundle()
    job, base = bundle.jobs[0], bundle.profiles[0]
    evidence = base.languages[0].evidence
    language = LanguageProficiency(
        language="English",
        evidence=evidence,
        proficiency=Claim[LanguageLevel](
            state=KnowledgeState.KNOWN, value=LanguageLevel.NATIVE, evidence=evidence
        ),
    )
    profile = CandidateProfile.model_validate({**base.model_dump(), "languages": [language]})
    report = calculate_baseline(
        job,
        profile,
        normalize_requirements(job, bundle.definitions[job.id], bundle.rules),
        bundle.rules,
        bundle.request(),
    )
    assert report.findings[2].reason == FindingReason.NOT_COMPARABLE
