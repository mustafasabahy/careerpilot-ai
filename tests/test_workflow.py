"""Actual compiled graph routes, persistence, state patches and local-only execution."""

import socket
from pathlib import Path
from uuid import uuid4

import pytest

from careerpilot.application.analysis import (
    AnalysisFailure,
    AnalysisReport,
    AnalysisRequest,
    AnalysisService,
    AssessmentConflict,
    AssessmentValidationError,
)
from careerpilot.application.storage import NotFound
from careerpilot.domain.baseline import BaselineAssessment, CriteriaError
from careerpilot.domain.jobs import FitDecision, JobFitAssessment
from careerpilot.integrations.synthetic import SyntheticBundle, seed_synthetic, synthetic_bundle
from careerpilot.persistence.sqlite import SQLiteDatabase
from careerpilot.workflows.job_analysis import JobAnalysisWorkflow


@pytest.fixture
def setup(
    tmp_path: Path,
) -> tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow]:
    database = SQLiteDatabase(tmp_path / "synthetic-workflow.sqlite3")
    database.initialize()
    bundle = synthetic_bundle()
    with database.transaction() as repository:
        seed_synthetic(repository, bundle)
    service = AnalysisService(database.transaction, bundle.definitions, bundle.rules)
    return database, bundle, service, JobAnalysisWorkflow(service)


def test_successful_graph_returns_typed_persisted_result(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
) -> None:
    database, bundle, _, workflow = setup
    result = workflow.run(bundle.request().model_dump(mode="json"))
    assert isinstance(result, AnalysisReport)
    assert result.status == "completed"
    assert result.decision == FitDecision.ELIGIBLE
    assert result.counts.hard_supported == 2
    with database.transaction() as repository:
        persisted = repository.get(JobFitAssessment, result.assessment_id)
        assert persisted.job_id == result.job_id
        assert persisted.candidate_id == result.candidate_id
        assert persisted.ai_score is None


def test_repeated_input_produces_identical_output_without_duplicate_insert(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
) -> None:
    _, bundle, _, workflow = setup
    assert workflow.run(bundle.request()) == workflow.run(bundle.request())


@pytest.mark.parametrize("raw", [None, {}, [], {"job_id": "not-a-uuid"}])
def test_invalid_inputs_route_directly_to_failure(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
    raw: object,
) -> None:
    _, _, _, workflow = setup
    updates = list(workflow.updates(raw))
    assert [next(iter(update)) for update in updates] == ["validate_request", "fail"]
    assert updates[-1]["fail"]["result"] == AnalysisFailure(code="invalid_request")
    assert updates[0]["validate_request"]["raw_request"] is None


@pytest.mark.parametrize(
    ("field", "code", "path"),
    [
        ("job_id", "job_not_found", ["validate_request", "load_job", "fail"]),
        (
            "candidate_id",
            "profile_not_found",
            ["validate_request", "load_job", "load_profile", "fail"],
        ),
    ],
)
def test_missing_objects_are_explicit_errors_and_stop_before_calculation(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
    field: str,
    code: str,
    path: list[str],
) -> None:
    database, bundle, _, workflow = setup
    raw = bundle.request().model_dump(mode="json")
    raw[field] = str(uuid4())
    updates = list(workflow.updates(raw))
    assert [next(iter(update)) for update in updates] == path
    result = workflow.run(raw)
    assert isinstance(result, AnalysisFailure)
    assert result.code == code
    with database.transaction() as repository:
        assert repository.find_duplicate_jobs(bundle.jobs[0].source) == (bundle.jobs[0],)


def test_success_state_patches_contain_ids_and_no_complete_profile_or_job(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
) -> None:
    _, bundle, _, workflow = setup
    patches = list(workflow.updates(bundle.request()))
    assert [next(iter(patch)) for patch in patches] == [
        "validate_request",
        "load_job",
        "load_profile",
        "normalize_requirements",
        "calculate_baseline",
        "validate_assessment",
        "return_result",
    ]
    assert patches[1]["load_job"] == {"job_id": bundle.jobs[0].id}
    assert patches[2]["load_profile"] == {"candidate_id": bundle.profiles[0].id}
    assert len(patches[3]["normalize_requirements"]["criteria"]) == 3
    assert isinstance(patches[4]["calculate_baseline"]["baseline"], BaselineAssessment)
    assert patches[5]["validate_assessment"]["needs_information"] is False
    assert patches[6]["return_result"]["baseline"] is None
    for patch in patches:
        for update in patch.values():
            assert "job" not in update and "profile" not in update and "documents" not in update


def test_unknowns_take_the_missing_information_branch(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
) -> None:
    _, bundle, _, workflow = setup
    patches = list(workflow.updates(bundle.request("missing-information")))
    assert next(iter(patches[-1])) == "missing_information"
    result = patches[-1]["missing_information"]["result"]
    assert isinstance(result, AnalysisReport)
    assert result.status == "needs_information"
    assert result.decision == FitDecision.NEEDS_REVIEW
    assert result.counts.soft_unknown == 1
    assert result.counts.soft_gaps == 1
    assert result.counts.unspecified == 1


def test_known_hard_gap_is_completed_but_ineligible(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
) -> None:
    _, bundle, _, workflow = setup
    result = workflow.run(bundle.request("hard-gap"))
    assert isinstance(result, AnalysisReport)
    assert result.status == "completed"
    assert result.decision == FitDecision.INELIGIBLE


def test_assessment_validation_rejects_tampering_before_any_write(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
) -> None:
    database, bundle, service, _ = setup
    request = bundle.request()
    criteria = service.normalize(request.job_id)
    report = service.calculate(request, criteria)
    tampered = report.model_copy(update={"findings": ()})
    with pytest.raises(AssessmentValidationError):
        service.validate_and_store(request, criteria, tampered)
    with database.transaction() as repository:
        with pytest.raises(NotFound):
            repository.get(JobFitAssessment, report.assessment.id)


def test_broken_calculation_is_not_converted_to_success(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, bundle, service, workflow = setup

    def broken(request: AnalysisRequest, criteria: object) -> BaselineAssessment:
        raise RuntimeError("Synthetic calculation failure")

    monkeypatch.setattr(service, "calculate", broken)
    with pytest.raises(RuntimeError, match="Synthetic calculation failure"):
        workflow.run(bundle.request())


def test_normalization_failure_is_not_converted_to_missing_information(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
) -> None:
    database, bundle, _, _ = setup
    definitions = dict(bundle.definitions)
    definitions[bundle.jobs[0].id] = (bundle.definitions[bundle.jobs[0].id][0],) * 2
    workflow = JobAnalysisWorkflow(AnalysisService(database.transaction, definitions, bundle.rules))
    with pytest.raises(CriteriaError):
        workflow.run(bundle.request())


def test_missing_definition_is_information_needed_not_an_execution_error(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
) -> None:
    database, bundle, _, _ = setup
    workflow = JobAnalysisWorkflow(AnalysisService(database.transaction, {}, bundle.rules))
    result = workflow.run(bundle.request())
    assert isinstance(result, AnalysisReport)
    assert result.status == "needs_information"
    assert result.counts.hard_unknown == 2


def test_conflicting_persisted_assessment_is_not_silently_replaced(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
) -> None:
    database, bundle, service, workflow = setup
    request = bundle.request()
    report = service.calculate(request, service.normalize(request.job_id))
    conflicting = JobFitAssessment(
        id=report.assessment.id,
        created_at=report.assessment.created_at,
        job_id=request.job_id,
        candidate_id=request.candidate_id,
    )
    with database.transaction() as repository:
        repository.add(conflicting)
    with pytest.raises(AssessmentConflict):
        workflow.run(request)
    with database.transaction() as repository:
        assert repository.get(JobFitAssessment, conflicting.id) == conflicting


def test_no_network_even_when_host_enables_tracing(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, bundle, _, workflow = setup
    connections: list[object] = []

    def deny_network(*args: object, **kwargs: object) -> None:
        connections.append(args)
        raise AssertionError("Workflow must not use the network")

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    from langsmith import tracing_context

    with tracing_context(enabled=True):
        assert isinstance(workflow.run(bundle.request()), AnalysisReport)
        assert list(workflow.updates(bundle.request()))
    assert not connections


def test_invalid_extra_fields_are_not_echoed_into_failure_output(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
) -> None:
    _, bundle, _, workflow = setup
    raw = {**bundle.request().model_dump(mode="json"), "private": "SYNTHETIC-SENSITIVE"}
    result = workflow.run(raw)
    assert result == AnalysisFailure(code="invalid_request")
    assert "SYNTHETIC-SENSITIVE" not in result.model_dump_json()


def test_raw_state_cannot_inject_a_fabricated_success(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
) -> None:
    _, _, _, workflow = setup
    assert workflow.run({"result": {"status": "completed"}}) == AnalysisFailure(
        code="invalid_request"
    )


def test_synthetic_seed_is_repeatable_and_preserves_existing_data(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
) -> None:
    database, bundle, _, _ = setup
    with database.transaction() as repository:
        seed_synthetic(repository, synthetic_bundle())
    assert synthetic_bundle() == bundle


def test_diagram_describes_real_conditional_routes(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
) -> None:
    _, _, _, workflow = setup
    diagram = workflow.mermaid()
    assert "validate_request" in diagram and "missing_information" in diagram
    assert "fail" in diagram and "validate_assessment" in diagram


def test_empty_job_is_information_needed_instead_of_a_perfect_match(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
) -> None:
    from careerpilot.domain.jobs import JobPosting, JobSource

    database, bundle, _, workflow = setup
    job = JobPosting(
        title="Synthetic empty job", employer="Example", source=JobSource(name="synthetic-empty")
    )
    with database.transaction() as repository:
        repository.add(job)
    raw = {**bundle.request().model_dump(mode="json"), "job_id": str(job.id)}
    result = workflow.run(raw)
    assert isinstance(result, AnalysisReport)
    assert result.status == "needs_information"
    assert result.decision == FitDecision.NEEDS_REVIEW
    assert not result.findings


def test_invalid_assessment_from_a_node_aborts_graph_before_persistence(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, bundle, service, workflow = setup
    request = bundle.request()
    valid = service.calculate(request, service.normalize(request.job_id))

    def tampered(request: AnalysisRequest, criteria: object) -> BaselineAssessment:
        return valid.model_copy(update={"findings": ()})

    monkeypatch.setattr(service, "calculate", tampered)
    with pytest.raises(AssessmentValidationError):
        workflow.run(request)
    with database.transaction() as repository:
        with pytest.raises(NotFound):
            repository.get(JobFitAssessment, valid.assessment.id)


def test_documented_examples_match_actual_graph_output(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
) -> None:
    import json

    _, _, _, workflow = setup
    examples = Path(__file__).resolve().parents[1] / "examples/job-analysis"
    for scenario in ("complete", "missing-information", "hard-gap", "invalid"):
        raw = json.loads((examples / f"{scenario}-input.json").read_text())
        expected = json.loads((examples / f"{scenario}-output.json").read_text())
        assert workflow.run(raw).model_dump(mode="json") == expected
    assert workflow.mermaid() == (examples / "workflow.mmd").read_text()


def test_changed_policy_gets_distinct_deterministic_assessment_identity(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
) -> None:
    from careerpilot.domain.baseline import BaselineRules

    database, bundle, _, workflow = setup
    first = workflow.run(bundle.request())
    changed = JobAnalysisWorkflow(
        AnalysisService(database.transaction, bundle.definitions, BaselineRules())
    )
    second = changed.run(bundle.request())
    assert isinstance(first, AnalysisReport) and isinstance(second, AnalysisReport)
    assert first.assessment_id != second.assessment_id
    assert second == changed.run(bundle.request())


def test_request_identity_and_utc_time_are_required_and_validated(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
) -> None:
    _, bundle, _, workflow = setup
    for field in ("id", "created_at"):
        raw = bundle.request().model_dump(mode="json")
        raw.pop(field)
        assert workflow.run(raw) == AnalysisFailure(code="invalid_request")
    raw = {**bundle.request().model_dump(mode="json"), "created_at": "2026-01-01T00:00:00"}
    assert workflow.run(raw) == AnalysisFailure(code="invalid_request")


def test_unsafe_request_objects_are_revalidated(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
) -> None:
    _, bundle, _, workflow = setup
    unsafe = bundle.request().model_copy(update={"job_id": "not-a-uuid"})
    assert workflow.run(unsafe) == AnalysisFailure(code="invalid_request")


def test_partial_update_stream_restores_callers_tracing_context(
    setup: tuple[SQLiteDatabase, SyntheticBundle, AnalysisService, JobAnalysisWorkflow],
) -> None:
    from langsmith import get_tracing_context, tracing_context

    _, bundle, _, workflow = setup
    with tracing_context(enabled=True):
        updates = workflow.updates(bundle.request())
        assert next(updates)["validate_request"]["request"] == bundle.request()
        assert get_tracing_context()["enabled"] is True
        assert next(updates)["load_job"]["job_id"] == bundle.jobs[0].id
        assert get_tracing_context()["enabled"] is True
        # Consume the remainder so the actual graph finishes, also without context leaks.
        assert list(updates)
        assert get_tracing_context()["enabled"] is True
