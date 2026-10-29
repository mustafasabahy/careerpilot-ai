import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from careerpilot.application.health import check_health
from careerpilot.cli import main
from careerpilot.config import Settings
from careerpilot.observability import SafeJsonFormatter, configure_logging


def test_health_is_deterministic_and_immutable() -> None:
    health = check_health()
    assert health.model_dump() == {"service": "careerpilot-ai", "status": "ok"}
    with pytest.raises(ValidationError):
        health.status = "ok"  # type: ignore[misc]  # Exercise runtime frozen-model validation.


def test_configuration_defaults() -> None:
    assert Settings().environment == "development"
    assert Settings().log_level == "INFO"


def test_environment_overrides_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    Path(".env").write_text("CAREERPILOT_LOG_LEVEL=WARNING\n", encoding="utf-8")
    assert Settings().log_level == "WARNING"
    monkeypatch.setenv("CAREERPILOT_LOG_LEVEL", "ERROR")
    assert Settings().log_level == "ERROR"


@pytest.mark.parametrize(
    ("key", "value"),
    [("CAREERPILOT_LOG_LEVEL", "VERBOSE"), ("CAREERPILOT_ENVIRONMENT", "unknown")],
)
def test_invalid_configuration_is_rejected(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        Settings()


def test_logs_discard_sensitive_fields_and_exceptions() -> None:
    record = logging.LogRecord(
        "careerpilot", logging.INFO, "", 0, "private %s", ("SYNTHETIC",), None
    )
    record.event = "health_checked"
    record.personal_data = "SYNTHETIC"
    try:
        raise ValueError("SYNTHETIC exception")
    except ValueError:
        import sys

        record.exc_info = sys.exc_info()
    rendered = SafeJsonFormatter().format(record)
    assert "SYNTHETIC" not in rendered
    assert "private" not in rendered
    assert set(json.loads(rendered)) == {"timestamp", "level", "event"}
    assert json.loads(rendered)["event"] == "health_checked"
    record.event = "SYNTHETIC"
    assert json.loads(SafeJsonFormatter().format(record))["event"] == "event"


def test_logging_setup_is_repeatable_and_does_not_mutate_root() -> None:
    root_handlers = list(logging.getLogger().handlers)
    logger = configure_logging("INFO")
    configure_logging("WARNING")
    assert len(logger.handlers) == 1
    assert not logger.propagate
    assert logger.level == logging.WARNING
    assert logging.getLogger().handlers == root_handlers


def test_cli_health(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("sys.argv", ["careerpilot", "health"])
    main()
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "ok"
    events = [json.loads(line)["event"] for line in captured.err.splitlines()]
    assert events == ["application_started", "health_checked"]


def test_cli_rejects_unknown_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["careerpilot", "unknown"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
