"""Evidence-bound extraction schemas and provider-neutral validation."""

from copy import deepcopy
from typing import Literal

import pytest
from pydantic import ValidationError

from careerpilot.application.extraction import (
    ExtractionRequest,
    ExtractionService,
    InvalidModelOutput,
    UngroundedExtraction,
)
from careerpilot.domain.extraction import ExtractedValue, ExtractionState, JobExtraction
from careerpilot.integrations.extraction_fixture import MOCK_JOB_TEXT, mock_extraction_payload
from careerpilot.integrations.synthetic import FIXTURE_TIME, fixture_id
from careerpilot.providers.mock import MockJobModel


class StubJobModel:
    provider: Literal["mock"] = "mock"
    model = "synthetic-stub"

    def __init__(self, output: object) -> None:
        self.output = output
        self.inputs: list[str] = []

    def extract(self, text: str) -> object:
        self.inputs.append(text)
        return self.output


def extraction_request(text: str = MOCK_JOB_TEXT) -> ExtractionRequest:
    return ExtractionRequest(
        id=fixture_id("extraction-document"), created_at=FIXTURE_TIME, text=text
    )


def test_complete_schema_and_provenance_roundtrip() -> None:
    result = ExtractionService(MockJobModel()).extract(extraction_request())
    extracted = result.extraction
    assert extracted.title.value == "Python Developer"
    assert extracted.skills[1].mandatory.value is False
    assert extracted.experience[0].minimum_years.value == 3
    assert extracted.work_arrangement.state == ExtractionState.CONFLICTING
    assert extracted.work_arrangement.value is None
    assert "contradictory_information" in extracted.warnings
    assert "missing_information" in extracted.warnings
    assert result.provenance.trust == "unreviewed_inference"
    assert result.provenance.provider == "mock"
    assert len(result.provenance.source_sha256) == 64
    assert type(result).model_validate_json(result.model_dump_json()) == result


def test_schema_missing_unknown_false_are_distinct() -> None:
    present = ExtractedValue[bool].model_validate(
        {
            "state": "present",
            "value": False,
            "evidence": [{"quote": "not required"}],
            "alternatives": [],
        }
    )
    missing = ExtractedValue[bool].model_validate(
        {"state": "missing", "value": None, "evidence": [], "alternatives": []}
    )
    unknown = ExtractedValue[bool].model_validate(
        {
            "state": "unknown",
            "value": None,
            "evidence": [{"quote": "may be required"}],
            "alternatives": [],
        }
    )
    assert present.value is False
    assert missing.state != unknown.state
    assert unknown.value is None


@pytest.mark.parametrize(
    "field",
    [
        {"state": "present", "value": "Python", "evidence": [], "alternatives": []},
        {"state": "missing", "value": "Python", "evidence": [], "alternatives": []},
        {"state": "unknown", "value": None, "evidence": [], "alternatives": []},
        {
            "state": "conflicting",
            "value": "remote",
            "evidence": [{"quote": "remote"}, {"quote": "onsite"}],
            "alternatives": ["remote", "onsite"],
        },
        {
            "state": "conflicting",
            "value": None,
            "evidence": [{"quote": "remote"}, {"quote": "remote"}],
            "alternatives": ["remote", "onsite"],
        },
        {
            "state": "conflicting",
            "value": None,
            "evidence": [{"quote": "remote"}, {"quote": "onsite"}],
            "alternatives": ["remote", "remote"],
        },
    ],
)
def test_invalid_field_states(field: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ExtractedValue[str].model_validate(field)


@pytest.mark.parametrize("output", [None, [], "not JSON", "{}", {"unexpected": "SYNTHETIC"}])
def test_malformed_model_output(output: object) -> None:
    with pytest.raises(InvalidModelOutput, match="schema"):
        ExtractionService(StubJobModel(output)).extract(extraction_request())


def test_ungrounded_quotes_are_rejected() -> None:
    payload = mock_extraction_payload()
    payload["title"] = {
        "state": "present",
        "value": "Invented",
        "evidence": [{"quote": "NOT IN SOURCE"}],
        "alternatives": [],
    }
    with pytest.raises(UngroundedExtraction):
        ExtractionService(StubJobModel(payload)).extract(extraction_request())


def test_unknown_and_contradictory_warnings_are_added_even_if_model_omits_them() -> None:
    payload = mock_extraction_payload()
    payload["warnings"] = []
    payload["location"] = {
        "state": "unknown",
        "value": None,
        "evidence": [{"quote": "Location: Berlin."}],
        "alternatives": [],
    }
    output = ExtractionService(StubJobModel(payload)).extract(extraction_request())
    assert {"ambiguous_information", "contradictory_information", "missing_information"}.issubset(
        output.extraction.warnings
    )


@pytest.mark.parametrize("value", [-1, 81, float("nan"), "3"])
def test_invalid_experience_years(value: object) -> None:
    payload = mock_extraction_payload()
    payload["experience"] = [
        {
            "description": {
                "state": "present",
                "value": "backend",
                "evidence": [{"quote": "backend"}],
                "alternatives": [],
            },
            "minimum_years": {
                "state": "present",
                "value": value,
                "evidence": [{"quote": "3 years"}],
                "alternatives": [],
            },
            "mandatory": {"state": "missing", "value": None, "evidence": [], "alternatives": []},
        }
    ]
    with pytest.raises(ValidationError):
        JobExtraction.model_validate(payload)


def test_boolean_string_does_not_become_a_fact() -> None:
    payload = mock_extraction_payload()
    payload["skills"] = [
        {
            "name": {
                "state": "present",
                "value": "Python",
                "evidence": [{"quote": "Python"}],
                "alternatives": [],
            },
            "minimum_proficiency": {
                "state": "missing",
                "value": None,
                "evidence": [],
                "alternatives": [],
            },
            "mandatory": {
                "state": "present",
                "value": "false",
                "evidence": [{"quote": "Python"}],
                "alternatives": [],
            },
        }
    ]
    with pytest.raises(ValidationError):
        JobExtraction.model_validate(payload)


def test_model_cannot_supply_candidate_facts_or_tool_permissions() -> None:
    payload = mock_extraction_payload()
    for key in ("candidate_profile", "tools", "system_policy", "user_confirmed"):
        malicious = deepcopy(payload)
        malicious[key] = "SYNTHETIC override"
        with pytest.raises(InvalidModelOutput):
            ExtractionService(StubJobModel(malicious)).extract(extraction_request())


def test_text_validation_hides_input_and_rejects_empty_or_oversized() -> None:
    for text in (" ", "SYNTHETIC" * 3000):
        with pytest.raises(ValidationError) as error:
            extraction_request(text)
        assert "SYNTHETIC" not in str(error.value)
    assert MOCK_JOB_TEXT not in repr(extraction_request())


def test_extraction_native_schema_has_required_properties_and_no_extra_fields() -> None:
    schema = JobExtraction.model_json_schema()
    objects = [schema, *schema.get("$defs", {}).values()]
    for definition in objects:
        if definition.get("type") == "object":
            assert definition.get("additionalProperties") is False
            assert set(definition.get("required", [])) == set(definition.get("properties", {}))
    assert "anyOf" not in schema


def test_application_port_accepts_another_provider_without_domain_or_workflow_changes() -> None:
    class AlternateModel:
        provider = "alternate-local-adapter"
        model = "synthetic-model"

        def extract(self, text: str) -> object:
            return mock_extraction_payload()

    result = ExtractionService(AlternateModel()).extract(extraction_request())
    assert result.provenance.provider == "alternate-local-adapter"
