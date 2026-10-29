"""Pure, evidence-aware baseline matching; no workflow or storage dependencies."""

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import Field, StrictBool, model_validator

from careerpilot.domain.common import Entity, Evidence, EvidenceOrigin, KnowledgeState, Model, Text
from careerpilot.domain.jobs import (
    JobFitAssessment,
    JobPosting,
    RequirementCategory,
    RequirementOutcome,
    RequirementResult,
)
from careerpilot.domain.profile import CandidateProfile, LanguageLevel, SkillProficiency


class CriteriaError(ValueError):
    """Deterministic definitions are inconsistent with the job or policy."""


class CriterionKind(StrEnum):
    SKILL = "skill"
    LANGUAGE = "language"
    WORK_AUTHORIZATION = "work_authorization"
    UNSUPPORTED = "unsupported"


class Importance(StrEnum):
    HARD = "hard"
    SOFT = "soft"
    UNSPECIFIED = "unspecified"


class FindingReason(StrEnum):
    SUPPORTED = "supported"
    BELOW_MINIMUM = "below_minimum"
    EXPLICIT_NEGATIVE = "explicit_negative"
    NOT_RECORDED = "not_recorded"
    UNVERIFIED = "unverified"
    UNKNOWN_VALUE = "unknown_value"
    CONFLICTING = "conflicting_evidence"
    UNSUPPORTED = "unsupported_criterion"
    MISSING_DEFINITION = "missing_definition"
    NOT_COMPARABLE = "not_comparable"


class NameAlias(Model):
    alias: Text
    canonical: Text


def normalized_name(value: str) -> str:
    return " ".join(value.casefold().split())


class BaselineRules(Model):
    version: Text = "baseline-v1"
    aliases: tuple[NameAlias, ...] = ()
    accepted_origins: tuple[EvidenceOrigin, ...] = Field(
        default=(EvidenceOrigin.USER_STATEMENT, EvidenceOrigin.SOURCE_DOCUMENT), min_length=1
    )

    @model_validator(mode="after")
    def safe_policy(self) -> Self:
        if EvidenceOrigin.AI_INFERENCE in self.accepted_origins:
            raise ValueError("AI inferences cannot be accepted as verified facts")
        aliases = {
            normalized_name(item.alias): normalized_name(item.canonical) for item in self.aliases
        }
        if len(aliases) != len(self.aliases):
            raise ValueError("Duplicate aliases")
        if any(target in aliases and target != key for key, target in aliases.items()):
            raise ValueError("Alias chains/cycles are not supported")
        return self

    def name(self, value: str) -> str:
        key = normalized_name(value)
        return next(
            (
                normalized_name(item.canonical)
                for item in self.aliases
                if normalized_name(item.alias) == key
            ),
            key,
        )

    def trusted(self, evidence: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
        return tuple(item for item in evidence if item.origin in self.accepted_origins)


class CriterionDefinition(Model):
    requirement_id: UUID
    kind: CriterionKind
    name: Text | None = None
    minimum_skill: SkillProficiency | None = None
    minimum_language: LanguageLevel | None = None
    expected: StrictBool | None = None

    @model_validator(mode="after")
    def coherent_definition(self) -> Self:
        if self.kind in {CriterionKind.SKILL, CriterionKind.LANGUAGE} and self.name is None:
            raise ValueError("Named criteria require a name")
        if self.kind != CriterionKind.SKILL and self.minimum_skill is not None:
            raise ValueError("Skill threshold on a different criterion")
        if self.kind != CriterionKind.LANGUAGE and self.minimum_language is not None:
            raise ValueError("Language threshold on a different criterion")
        if self.kind == CriterionKind.WORK_AUTHORIZATION:
            if self.expected is None:
                raise ValueError("Authorization criterion must specify an expected boolean")
        elif self.expected is not None:
            raise ValueError("Boolean expectation on a different criterion")
        return self


class NormalizedCriterion(Model):
    requirement_id: UUID
    importance: Importance
    definition: CriterionDefinition | None

    @model_validator(mode="after")
    def same_identifier(self) -> Self:
        if self.definition is not None and self.definition.requirement_id != self.requirement_id:
            raise ValueError("Definition belongs to a different requirement")
        return self


def normalize_requirements(
    job: JobPosting, definitions: tuple[CriterionDefinition, ...], rules: BaselineRules
) -> tuple[NormalizedCriterion, ...]:
    lookup = {item.requirement_id: item for item in definitions}
    if len(lookup) != len(definitions):
        raise CriteriaError("Duplicate criterion definitions")
    if not set(lookup).issubset({item.id for item in job.requirements}):
        raise CriteriaError("Definition references another job's requirement")
    categories = {
        CriterionKind.SKILL: RequirementCategory.SKILL,
        CriterionKind.LANGUAGE: RequirementCategory.LANGUAGE,
        CriterionKind.WORK_AUTHORIZATION: RequirementCategory.ELIGIBILITY,
    }
    normalized: list[NormalizedCriterion] = []
    for requirement in job.requirements:
        definition = lookup.get(requirement.id)
        if definition is not None:
            category = categories.get(definition.kind)
            if category is not None and category != requirement.category:
                raise CriteriaError("Criterion kind does not match the source requirement category")
            definition = CriterionDefinition.model_validate(
                {
                    **definition.model_dump(),
                    "name": rules.name(definition.name) if definition.name is not None else None,
                }
            )
        importance = Importance.UNSPECIFIED
        if requirement.mandatory is True:
            importance = Importance.HARD
        elif requirement.mandatory is False:
            importance = Importance.SOFT
        normalized.append(
            NormalizedCriterion(
                requirement_id=requirement.id, importance=importance, definition=definition
            )
        )
    return tuple(normalized)


class RequirementFinding(Model):
    requirement_id: UUID
    importance: Importance
    outcome: RequirementOutcome
    reason: FindingReason
    evidence_ids: tuple[UUID, ...] = ()


class BaselineCounts(Model):
    hard_supported: int = Field(ge=0)
    hard_gaps: int = Field(ge=0)
    hard_unknown: int = Field(ge=0)
    soft_supported: int = Field(ge=0)
    soft_gaps: int = Field(ge=0)
    soft_unknown: int = Field(ge=0)
    unspecified: int = Field(ge=0)


class BaselineAssessment(Model):
    assessment: JobFitAssessment
    rules: BaselineRules
    findings: tuple[RequirementFinding, ...]

    @property
    def needs_information(self) -> bool:
        return not self.findings or any(
            item.outcome == RequirementOutcome.UNKNOWN or item.importance == Importance.UNSPECIFIED
            for item in self.findings
        )

    @property
    def counts(self) -> BaselineCounts:
        def count(importance: Importance, outcome: RequirementOutcome) -> int:
            return sum(
                item.importance == importance and item.outcome == outcome for item in self.findings
            )

        return BaselineCounts(
            hard_supported=count(Importance.HARD, RequirementOutcome.MET),
            hard_gaps=count(Importance.HARD, RequirementOutcome.UNMET),
            hard_unknown=count(Importance.HARD, RequirementOutcome.UNKNOWN),
            soft_supported=count(Importance.SOFT, RequirementOutcome.MET),
            soft_gaps=count(Importance.SOFT, RequirementOutcome.UNMET),
            soft_unknown=count(Importance.SOFT, RequirementOutcome.UNKNOWN),
            unspecified=sum(item.importance == Importance.UNSPECIFIED for item in self.findings),
        )


_SKILLS = {level: rank for rank, level in enumerate(SkillProficiency)}
_LANGUAGES = {
    level: rank for rank, level in enumerate(LanguageLevel) if level != LanguageLevel.NATIVE
}


class _Match(Model):
    outcome: RequirementOutcome
    reason: FindingReason
    evidence: tuple[Evidence, ...] = ()


def _unknown(reason: FindingReason) -> _Match:
    return _Match(outcome=RequirementOutcome.UNKNOWN, reason=reason)


def _combine(matches: list[_Match]) -> _Match:
    if not matches:
        return _unknown(FindingReason.NOT_RECORDED)
    decided = [item for item in matches if item.outcome != RequirementOutcome.UNKNOWN]
    if len({item.outcome for item in decided}) > 1:
        return _unknown(FindingReason.CONFLICTING)
    if not decided:
        return matches[0]
    evidence = {e.id: e for item in decided for e in item.evidence}
    return _Match(
        outcome=decided[0].outcome, reason=decided[0].reason, evidence=tuple(evidence.values())
    )


def _skill_match(
    profile: CandidateProfile, definition: CriterionDefinition, rules: BaselineRules
) -> _Match:
    matches: list[_Match] = []
    for skill in profile.skills:
        if rules.name(skill.name) != definition.name:
            continue
        evidence = rules.trusted(skill.evidence)
        if not evidence:
            matches.append(_unknown(FindingReason.UNVERIFIED))
            continue
        if definition.minimum_skill is None:
            matches.append(
                _Match(
                    outcome=RequirementOutcome.MET,
                    reason=FindingReason.SUPPORTED,
                    evidence=evidence,
                )
            )
            continue
        claim = skill.proficiency
        if claim.state != KnowledgeState.KNOWN or claim.value is None:
            matches.append(_unknown(FindingReason.UNKNOWN_VALUE))
            continue
        proficiency_evidence = rules.trusted(claim.evidence)
        if not proficiency_evidence:
            matches.append(_unknown(FindingReason.UNVERIFIED))
            continue
        met = _SKILLS[claim.value] >= _SKILLS[definition.minimum_skill]
        matches.append(
            _Match(
                outcome=RequirementOutcome.MET if met else RequirementOutcome.UNMET,
                reason=FindingReason.SUPPORTED if met else FindingReason.BELOW_MINIMUM,
                evidence=tuple(
                    {item.id: item for item in (*evidence, *proficiency_evidence)}.values()
                ),
            )
        )
    return _combine(matches)


def _language_match(
    profile: CandidateProfile, definition: CriterionDefinition, rules: BaselineRules
) -> _Match:
    matches: list[_Match] = []
    for language in profile.languages:
        if rules.name(language.language) != definition.name:
            continue
        evidence = rules.trusted(language.evidence)
        claim = language.proficiency
        if not evidence:
            matches.append(_unknown(FindingReason.UNVERIFIED))
        elif definition.minimum_language is None:
            matches.append(
                _Match(
                    outcome=RequirementOutcome.MET,
                    reason=FindingReason.SUPPORTED,
                    evidence=evidence,
                )
            )
        elif claim.value is None:
            matches.append(_unknown(FindingReason.UNKNOWN_VALUE))
        elif not rules.trusted(claim.evidence):
            matches.append(_unknown(FindingReason.UNVERIFIED))
        elif (claim.value == LanguageLevel.NATIVE) != (
            definition.minimum_language == LanguageLevel.NATIVE
        ):
            matches.append(_unknown(FindingReason.NOT_COMPARABLE))
        else:
            met = claim.value == definition.minimum_language or (
                claim.value != LanguageLevel.NATIVE
                and definition.minimum_language != LanguageLevel.NATIVE
                and _LANGUAGES[claim.value] >= _LANGUAGES[definition.minimum_language]
            )
            matches.append(
                _Match(
                    outcome=RequirementOutcome.MET if met else RequirementOutcome.UNMET,
                    reason=FindingReason.SUPPORTED if met else FindingReason.BELOW_MINIMUM,
                    evidence=tuple(
                        {e.id: e for e in (*evidence, *rules.trusted(claim.evidence))}.values()
                    ),
                )
            )
    return _combine(matches)


def _evaluate(
    profile: CandidateProfile, criterion: NormalizedCriterion, rules: BaselineRules
) -> _Match:
    definition = criterion.definition
    if definition is None:
        return _unknown(FindingReason.MISSING_DEFINITION)
    if definition.kind == CriterionKind.SKILL:
        return _skill_match(profile, definition, rules)
    if definition.kind == CriterionKind.LANGUAGE:
        return _language_match(profile, definition, rules)
    if definition.kind == CriterionKind.WORK_AUTHORIZATION:
        claim = profile.work_authorized
        if claim.value is None:
            return _unknown(FindingReason.UNKNOWN_VALUE)
        evidence = rules.trusted(claim.evidence)
        if not evidence:
            return _unknown(FindingReason.UNVERIFIED)
        met = claim.value == definition.expected
        return _Match(
            outcome=RequirementOutcome.MET if met else RequirementOutcome.UNMET,
            reason=FindingReason.SUPPORTED if met else FindingReason.EXPLICIT_NEGATIVE,
            evidence=evidence,
        )
    return _unknown(FindingReason.UNSUPPORTED)


def calculate_baseline(
    job: JobPosting,
    profile: CandidateProfile,
    criteria: tuple[NormalizedCriterion, ...],
    rules: BaselineRules,
    identity: Entity,
) -> BaselineAssessment:
    """All nondeterminism (identity and time) is supplied by the caller."""
    definitions = tuple(item.definition for item in criteria if item.definition is not None)
    if normalize_requirements(job, definitions, rules) != criteria:
        raise CriteriaError(
            "Criteria must preserve source order, category, importance and normalized names"
        )
    results: list[RequirementResult] = []
    findings: list[RequirementFinding] = []
    for requirement, criterion in zip(job.requirements, criteria, strict=True):
        importance = (
            Importance.HARD
            if requirement.mandatory is True
            else (Importance.SOFT if requirement.mandatory is False else Importance.UNSPECIFIED)
        )
        if criterion.importance != importance:
            raise CriteriaError("Normalized criterion changed requirement importance")
        match = _evaluate(profile, criterion, rules)
        results.append(
            RequirementResult(
                requirement_id=criterion.requirement_id,
                outcome=match.outcome,
                evidence=match.evidence,
            )
        )
        findings.append(
            RequirementFinding(
                requirement_id=criterion.requirement_id,
                importance=importance,
                outcome=match.outcome,
                reason=match.reason,
                evidence_ids=tuple(item.id for item in match.evidence),
            )
        )
    return BaselineAssessment(
        assessment=JobFitAssessment(
            id=identity.id,
            created_at=identity.created_at,
            candidate_id=profile.id,
            job_id=job.id,
            results=tuple(results),
        ),
        rules=rules,
        findings=tuple(findings),
    )
