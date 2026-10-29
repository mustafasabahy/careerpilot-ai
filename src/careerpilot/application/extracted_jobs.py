"""Prepare unreviewed job snapshots without changing verified profile facts."""

import hashlib
import json
from uuid import uuid5

from pydantic import BaseModel

from careerpilot.application.extraction import ExtractionEnvelope
from careerpilot.domain.baseline import CriterionDefinition, CriterionKind
from careerpilot.domain.common import Evidence, EvidenceOrigin, Model
from careerpilot.domain.extraction import (
    ExtractedLanguage,
    ExtractedSkill,
    ExtractionState,
    SourceSnippet,
)
from careerpilot.domain.jobs import JobPosting, JobRequirement, JobSource, RequirementCategory


class PreparedJob(Model):
    job: JobPosting
    definitions: tuple[CriterionDefinition, ...]


def _quotes(model: BaseModel) -> tuple[str, ...]:
    if isinstance(model, SourceSnippet):
        return (model.quote,)
    found: list[str] = []
    for name in type(model).model_fields:
        value = getattr(model, name)
        if isinstance(value, BaseModel):
            found.extend(_quotes(value))
        elif isinstance(value, tuple):
            for item in value:
                if isinstance(item, BaseModel):
                    found.extend(_quotes(item))
    return tuple(dict.fromkeys(found))


def prepare_job(envelope: ExtractionEnvelope) -> PreparedJob | None:
    extraction = envelope.extraction
    # Existing persisted JobPosting requires a title/employer. Do not invent placeholders
    # or change that domain invariant just to force an incomplete extraction into storage.
    if (
        extraction.title.state != ExtractionState.PRESENT
        or extraction.employer.state != ExtractionState.PRESENT
    ):
        return None
    if extraction.title.value is None or extraction.employer.value is None:
        return None
    provenance = envelope.provenance
    material = json.dumps(
        {
            "source": provenance.source_sha256,
            "provider": provenance.provider,
            "model": provenance.model,
            "prompt": provenance.prompt_version,
            "received_at": provenance.received_at.isoformat(),
            "extraction": extraction.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    job_id = uuid5(provenance.document_id, material)
    requirements: list[JobRequirement] = []
    definitions: list[CriterionDefinition] = []
    groups = (
        ("skill", extraction.skills, RequirementCategory.SKILL),
        ("qualification", extraction.qualifications, RequirementCategory.EDUCATION),
        ("experience", extraction.experience, RequirementCategory.EXPERIENCE),
        ("language", extraction.languages, RequirementCategory.LANGUAGE),
    )
    for label, entries, category in groups:
        for index, entry in enumerate(entries):
            identifier = uuid5(job_id, f"{label}:{index}")
            quotes = _quotes(entry)
            if not quotes:
                raise ValueError("Extracted requirement has no provenance")
            evidence = tuple(
                Evidence(
                    id=uuid5(identifier, quote),
                    created_at=provenance.received_at,
                    origin=EvidenceOrigin.AI_INFERENCE,
                    reference=f"document:{provenance.document_id}:quote-sha256:{hashlib.sha256(quote.encode()).hexdigest()}",
                )
                for quote in quotes
            )
            requirements.append(
                JobRequirement(
                    id=identifier,
                    created_at=provenance.received_at,
                    description=quotes[0],
                    category=category,
                    mandatory=None,
                    evidence=evidence,
                )
            )
            definition = CriterionDefinition(
                requirement_id=identifier, kind=CriterionKind.UNSUPPORTED
            )
            if isinstance(entry, ExtractedSkill) and entry.name.state == ExtractionState.PRESENT:
                if entry.minimum_proficiency.state in {
                    ExtractionState.MISSING,
                    ExtractionState.PRESENT,
                }:
                    definition = CriterionDefinition(
                        requirement_id=identifier,
                        kind=CriterionKind.SKILL,
                        name=entry.name.value,
                        minimum_skill=entry.minimum_proficiency.value,
                    )
            elif (
                isinstance(entry, ExtractedLanguage) and entry.name.state == ExtractionState.PRESENT
            ):
                if entry.minimum_level.state in {ExtractionState.MISSING, ExtractionState.PRESENT}:
                    definition = CriterionDefinition(
                        requirement_id=identifier,
                        kind=CriterionKind.LANGUAGE,
                        name=entry.name.value,
                        minimum_language=entry.minimum_level.value,
                    )
            definitions.append(definition)
    return PreparedJob(
        job=JobPosting(
            id=job_id,
            created_at=provenance.received_at,
            title=extraction.title.value,
            employer=extraction.employer.value,
            source=JobSource(
                name=f"unreviewed-extraction:{provenance.provider}",
                external_id=str(job_id),
                retrieved_at=provenance.received_at,
            ),
            requirements=tuple(requirements),
        ),
        definitions=tuple(definitions),
    )
