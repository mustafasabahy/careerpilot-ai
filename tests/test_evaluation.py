"""Offline dataset reproducibility and telemetry content minimization."""

from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from careerpilot.application.execution import RunControl, consume, execution_scope
from careerpilot.evaluation import run_dataset
from careerpilot.telemetry.langsmith import LangSmithObserver, TraceSettings


def test_offline_dataset_blocks_any_deterministic_failure() -> None:
    path = Path(__file__).resolve().parents[1] / "evals" / "dataset-v1.json"
    first, second = run_dataset(path), run_dataset(path)
    assert first == second
    assert first["passed"] == first["total"] == 12


def test_chat_agent_offline_dataset() -> None:
    path = Path(__file__).resolve().parents[1] / "evals" / "chat-agent-v1.json"
    first, second = run_dataset(path), run_dataset(path)
    assert first == second
    assert first["passed"] == first["total"] == 4


def test_trace_opt_in_and_metadata_only() -> None:
    captured: list[dict[str, Any]] = []

    class FakeClient:
        def create_run(
            self, name: str, inputs: dict[str, Any], run_type: Literal["chain"], **kwargs: Any
        ) -> None:
            captured.append({"name": name, "inputs": inputs, "type": run_type, **kwargs})

    client = FakeClient()
    with pytest.raises(ValidationError):
        TraceSettings(enabled=True)
    disabled = LangSmithObserver(TraceSettings(), client)
    disabled.observe(RunControl().record)
    assert not captured
    settings = TraceSettings(enabled=True, LANGSMITH_API_KEY=SecretStr("SYNTHETIC-SECRET"))
    observer = LangSmithObserver(settings, client)
    owner = uuid4()
    control = RunControl(owner_id=owner, observer=observer)
    with execution_scope(control):
        consume("steps")
    assert captured and captured[0]["inputs"] == {} and captured[0]["outputs"] == {}
    assert str(owner) not in str(captured) and "SYNTHETIC-SECRET" not in str(captured)
    assert "api_key" not in settings.model_dump()
