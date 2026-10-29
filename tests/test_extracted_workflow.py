"""Extracted jobs feed the existing graph without promoting model claims or editing profiles."""

from pathlib import Path

import pytest
from test_extraction import StubJobModel, extraction_request

from careerpilot.application.analysis import AnalysisReport
from careerpilot.application.extraction import (
    ExtractionService,
    InvalidModelOutput,
    MockInputUnsupported,
)
from careerpilot.application.storage import NotFound
from careerpilot.domain.common import EvidenceOrigin
from careerpilot.domain.jobs import FitDecision, JobPosting
from careerpilot.domain.profile import CandidateProfile
from careerpilot.extract_demo import main
from careerpilot.integrations.extraction_fixture import mock_extraction_payload
from careerpilot.integrations.synthetic import (
    SyntheticBundle,
    fixture_id,
    seed_synthetic,
    synthetic_bundle,
)
from careerpilot.persistence.sqlite import SQLiteDatabase
from careerpilot.providers.mock import MockJobModel
from careerpilot.workflows.extracted_job import ExtractedJobWorkflow


@pytest.fixture
def extraction_setup(tmp_path: Path) -> tuple[SQLiteDatabase, SyntheticBundle]:
    database = SQLiteDatabase(tmp_path / "extracted.sqlite3")
    database.initialize()
    bundle = synthetic_bundle()
    with database.transaction() as repository:
        seed_synthetic(repository, bundle)
    return database, bundle


def test_mock_to_actual_graph_preserves_candidate_and_inference_provenance(
    extraction_setup: tuple[SQLiteDatabase, SyntheticBundle],
) -> None:
    database, bundle = extraction_setup
    workflow = ExtractedJobWorkflow(
        ExtractionService(MockJobModel()), database.transaction, bundle.rules
    )
    result = workflow.run(
        extraction_request(),
        candidate_id=bundle.profiles[0].id,
        analysis_id=fixture_id("extracted-analysis"),
    )
    assert result.status == "requires_review"
    assert isinstance(result.analysis, AnalysisReport)
    assert result.analysis.status == "needs_information"
    assert result.analysis.decision == FitDecision.NEEDS_REVIEW
    assert result.analysis.counts.unspecified == 5
    assert result.extraction.extraction.skills[0].mandatory.value is True
    with database.transaction() as repository:
        assert repository.get(CandidateProfile, bundle.profiles[0].id) == bundle.profiles[0]
        job = repository.get(JobPosting, result.analysis.job_id)
        assert all(item.mandatory is None for item in job.requirements)
        assert all(
            evidence.origin == EvidenceOrigin.AI_INFERENCE
            for requirement in job.requirements
            for evidence in requirement.evidence
        )
    assert (
        workflow.run(
            extraction_request(),
            candidate_id=bundle.profiles[0].id,
            analysis_id=fixture_id("extracted-analysis"),
        )
        == result
    )


@pytest.mark.parametrize("state", ["missing", "unknown", "conflicting"])
def test_missing_or_conflicting_identity_returns_extraction_only_without_invented_fields(
    extraction_setup: tuple[SQLiteDatabase, SyntheticBundle],
    state: str,
) -> None:
    database, bundle = extraction_setup
    payload = mock_extraction_payload()
    payload["title"] = {
        "state": state,
        "value": None,
        "evidence": [] if state == "missing" else [{"quote": "Title: Python Developer"}],
        "alternatives": [],
    }
    if state == "conflicting":
        payload["title"] = {
            "state": state,
            "value": None,
            "evidence": [
                {"quote": "Title: Python Developer"},
                {"quote": "Employer: Example Organization"},
            ],
            "alternatives": ["Python Developer", "Example Organization"],
        }
    result = ExtractedJobWorkflow(
        ExtractionService(StubJobModel(payload)), database.transaction, bundle.rules
    ).run(
        extraction_request(),
        candidate_id=bundle.profiles[0].id,
        analysis_id=fixture_id("identity-analysis"),
    )
    assert result.analysis is None
    assert result.review_reason == "missing_or_conflicting_job_identity"
    assert result.extraction.extraction.title.state.value == state


def test_missing_candidate_is_detected_before_model_call(
    extraction_setup: tuple[SQLiteDatabase, SyntheticBundle],
) -> None:
    database, bundle = extraction_setup
    model = StubJobModel(mock_extraction_payload())
    with pytest.raises(NotFound):
        ExtractedJobWorkflow(ExtractionService(model), database.transaction, bundle.rules).run(
            extraction_request(),
            candidate_id=fixture_id("nonexistent"),
            analysis_id=fixture_id("analysis"),
        )
    assert not model.inputs


def test_malformed_extraction_aborts_before_job_analysis(
    extraction_setup: tuple[SQLiteDatabase, SyntheticBundle],
) -> None:
    database, bundle = extraction_setup
    with pytest.raises(InvalidModelOutput):
        ExtractedJobWorkflow(
            ExtractionService(StubJobModel({})), database.transaction, bundle.rules
        ).run(
            extraction_request(),
            candidate_id=bundle.profiles[0].id,
            analysis_id=fixture_id("analysis"),
        )


def test_mock_cannot_fabricate_extraction_for_unknown_text(
    extraction_setup: tuple[SQLiteDatabase, SyntheticBundle],
) -> None:
    database, bundle = extraction_setup
    with pytest.raises(MockInputUnsupported):
        ExtractedJobWorkflow(
            ExtractionService(MockJobModel()), database.transaction, bundle.rules
        ).run(
            extraction_request("SYNTHETIC unknown source"),
            candidate_id=bundle.profiles[0].id,
            analysis_id=fixture_id("analysis"),
        )


def test_extraction_cli_default_is_offline(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    monkeypatch.setattr("sys.argv", ["careerpilot-extract"])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "requires_review"
    assert result["extraction"]["provenance"]["provider"] == "mock"
    assert result["analysis"]["decision"] == "needs_review"


def test_extraction_cli_fails_fast_without_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CAREERPILOT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("CAREERPILOT_LLM_MODEL", "synthetic-model")
    monkeypatch.setattr("sys.argv", ["careerpilot-extract"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "OPENAI_API_KEY" in captured.err
    assert not captured.out


def test_health_is_compatible_even_when_real_llm_settings_are_incomplete(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from careerpilot.cli import main as health

    monkeypatch.setenv("CAREERPILOT_LLM_PROVIDER", "openai")
    monkeypatch.setattr("sys.argv", ["careerpilot", "health"])
    health()
    assert '"status":"ok"' in capsys.readouterr().out


def test_saved_offline_example_matches_the_executable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    examples = Path(__file__).resolve().parents[1] / "examples/extraction"
    monkeypatch.setattr(
        "sys.argv", ["careerpilot-extract", "--text", str(examples / "synthetic-job.txt")]
    )
    main()
    assert json.loads(capsys.readouterr().out) == json.loads(
        (examples / "mock-output.json").read_text()
    )
