"""Profile aggregate and small evidence-bearing career records."""

from datetime import date
from enum import StrEnum
from typing import Self

from pydantic import Field, StrictBool, model_validator

from careerpilot.domain.common import Claim, Entity, EvidenceSet, Text


class SkillProficiency(StrEnum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"
    EXPERT = "expert"


class LanguageLevel(StrEnum):
    A1 = "A1"
    A2 = "A2"
    B1 = "B1"
    B2 = "B2"
    C1 = "C1"
    C2 = "C2"
    NATIVE = "native"


class CareerRecord(Entity):
    evidence: EvidenceSet


class WorkExperience(CareerRecord):
    employer: Text
    title: Text
    started_on: date
    ended_on: date | None = None
    is_current: Claim[StrictBool] = Field(default_factory=Claim[StrictBool])

    @model_validator(mode="after")
    def valid_period(self) -> Self:
        if self.ended_on is not None and self.ended_on < self.started_on:
            raise ValueError("Experience end precedes start")
        if self.ended_on is not None and self.is_current.value is True:
            raise ValueError("Current experience cannot have an end date")
        return self


class Skill(CareerRecord):
    name: Text
    proficiency: Claim[SkillProficiency] = Field(default_factory=Claim[SkillProficiency])


class Education(CareerRecord):
    institution: Text
    qualification: Text
    started_on: date
    ended_on: date | None = None

    @model_validator(mode="after")
    def valid_period(self) -> Self:
        if self.ended_on is not None and self.ended_on < self.started_on:
            raise ValueError("Education end precedes start")
        return self


class Certification(CareerRecord):
    name: Text
    issuer: Text
    issued_on: date
    expires_on: date | None = None

    @model_validator(mode="after")
    def valid_period(self) -> Self:
        if self.expires_on is not None and self.expires_on < self.issued_on:
            raise ValueError("Certification expiry precedes issue")
        return self


class LanguageProficiency(CareerRecord):
    language: Text
    proficiency: Claim[LanguageLevel] = Field(default_factory=Claim[LanguageLevel])


class CandidateProfile(Entity):
    display_name: Claim[Text] = Field(default_factory=Claim[Text])
    work_authorized: Claim[StrictBool] = Field(default_factory=Claim[StrictBool])
    experience: tuple[WorkExperience, ...] = ()
    skills: tuple[Skill, ...] = ()
    education: tuple[Education, ...] = ()
    certifications: tuple[Certification, ...] = ()
    languages: tuple[LanguageProficiency, ...] = ()

    @model_validator(mode="after")
    def unique_record_ids(self) -> Self:
        records = (
            *self.experience,
            *self.skills,
            *self.education,
            *self.certifications,
            *self.languages,
        )
        if len({record.id for record in records}) != len(records):
            raise ValueError("Profile record identifiers must be unique")
        return self
