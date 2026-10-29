"""Scoped corrections found during the independent final review."""

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from careerpilot.api.app import create_app
from careerpilot.application.discovery_contracts import JobRecord
from careerpilot.application.storage import NotFound
from careerpilot.domain.jobs import JobPosting
from careerpilot.integrations.synthetic import synthetic_bundle
from careerpilot.portal import Portal
from careerpilot.telemetry.langsmith import LangSmithObserver, TraceSettings


def test_definition_write_failure_rolls_back_authoritative_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Portal.initialize(tmp_path)
    portal = Portal(tmp_path)
    bundle = synthetic_bundle()
    record = JobRecord(job=bundle.jobs[0], definitions=bundle.definitions[bundle.jobs[0].id])
    owner = uuid4()
    original = Path.write_text

    def fail_definition(path: Path, data: str, *args: Any, **kwargs: Any) -> int:
        if path.suffix == ".tmp":
            raise OSError("Synthetic interrupted definition write")
        return original(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_definition)
    with pytest.raises(OSError):
        portal.import_job(owner, record)
    with portal.database.transaction() as repository, pytest.raises(NotFound):
        repository.get(JobPosting, record.job.id)
    assert portal.get_many(owner, "job", JobPosting, (record.job.id,)) == ()
    monkeypatch.setattr(Path, "write_text", original)
    portal.import_job(owner, record)
    assert portal.import_job(owner, record) == record.job
    assert portal.definitions(owner, record.job.id) == record


def test_native_trace_client_cannot_add_environment_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def create_run(self, *args: Any, **kwargs: Any) -> None:
            pass

    monkeypatch.setattr("careerpilot.telemetry.langsmith.Client", Client)
    LangSmithObserver(TraceSettings(enabled=True, LANGSMITH_API_KEY=SecretStr("synthetic")))
    assert captured["omit_traced_runtime_info"] is True
    assert captured["otel_enabled"] is False
    assert captured["retry_config"].total == 0
    monkeypatch.setenv("LANGSMITH_RUNS_ENDPOINTS", "https://unapproved.invalid")
    with pytest.raises(ValidationError):
        TraceSettings(enabled=True, LANGSMITH_API_KEY=SecretStr("synthetic"))


def test_stale_owner_reservation_never_breaks_list_or_leaks(tmp_path: Path) -> None:
    Portal.initialize(tmp_path)
    portal = Portal(tmp_path)
    owner = uuid4()
    portal.security.bind(owner, "candidate", uuid4())
    token = portal.security.issue(owner)
    response = TestClient(create_app(portal)).get(
        "/api/profiles", headers={"Authorization": "Bearer " + token}
    )
    assert response.status_code == 200 and response.json()["data"]["items"] == []


def test_api_docs_work_under_self_only_content_security_policy(tmp_path: Path) -> None:
    Portal.initialize(tmp_path)
    client = TestClient(create_app(Portal(tmp_path)))
    page = client.get("/docs")
    assert page.status_code == 200
    assert "cdn.jsdelivr.net" not in page.text
    assert "/assets/api-docs.js" in page.text
    assert client.get("/assets/api-docs.js").status_code == 200


def test_shared_runtime_metrics_do_not_lose_concurrent_updates() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from careerpilot.application.execution import RunControl
    from careerpilot.application.failures import BudgetExceeded
    from careerpilot.domain.reliability import ExecutionLimits

    control = RunControl(ExecutionLimits(max_model_calls=10))

    def attempt(_: int) -> bool:
        try:
            control.consume("model_calls")
        except BudgetExceeded:
            return False
        control.note_node("model")
        control.note_usage(2, 1)
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(100))) == 10
    assert control.record.model_calls == control.record.nodes["model"] == 10
    assert control.record.input_tokens == 20 and control.record.output_tokens == 10


def test_resource_exhaustion_reports_nonretryable_limit_without_internal_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from careerpilot.application.analysis import AnalysisRequest
    from careerpilot.application.failures import BudgetExceeded
    from careerpilot.domain.common import utc_now

    Portal.initialize(tmp_path)
    portal = Portal(tmp_path)
    token = portal.security.issue(uuid4())

    def exhausted(owner: object, request: AnalysisRequest) -> None:
        raise BudgetExceeded()

    monkeypatch.setattr(portal, "analyze", exhausted)
    response = TestClient(create_app(portal)).post(
        "/api/analysis",
        headers={"Authorization": "Bearer " + token},
        json={
            "id": str(uuid4()),
            "created_at": utc_now().isoformat(),
            "candidate_id": str(uuid4()),
            "job_id": str(uuid4()),
        },
    )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "limit_exceeded"
    assert "Traceback" not in response.text
