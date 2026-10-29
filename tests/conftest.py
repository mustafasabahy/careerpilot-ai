"""Synthetic fixtures with no dependency on developer environment or local .env."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CAREERPILOT_ENVIRONMENT", raising=False)
    monkeypatch.delenv("CAREERPILOT_LOG_LEVEL", raising=False)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live-llm",
        action="store_true",
        default=False,
        help="Enable marked paid LLM tests; separate environment opt-in is also required",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--run-live-llm"):
        marker = pytest.mark.skip(
            reason="Live LLM tests require --run-live-llm and explicit environment configuration"
        )
        for item in items:
            if item.get_closest_marker("live_llm"):
                item.add_marker(marker)


@pytest.fixture(autouse=True)
def isolate_llm_and_block_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.node.get_closest_marker("live_llm"):
        return
    import socket

    for key in (
        "CAREERPILOT_CHAT_MODE",
        "CAREERPILOT_CHAT_MAX_MODEL_CALLS",
        "CAREERPILOT_CHAT_MAX_TOOL_CALLS",
        "CAREERPILOT_CHAT_OVERALL_SECONDS",
        "CAREERPILOT_CHAT_HISTORY_MESSAGES",
        "CAREERPILOT_CHAT_HISTORY_CHARS",
        "CAREERPILOT_CHAT_TOOL_BYTES",
        "CAREERPILOT_CHAT_PROVIDER",
        "CAREERPILOT_CHAT_MODEL",
        "CAREERPILOT_CHAT_TIMEOUT_SECONDS",
        "CAREERPILOT_CHAT_MAX_RETRIES",
        "CAREERPILOT_CHAT_MAX_OUTPUT_TOKENS",
        "CAREERPILOT_LLM_PROVIDER",
        "CAREERPILOT_LLM_MODEL",
        "CAREERPILOT_LLM_TIMEOUT_SECONDS",
        "CAREERPILOT_LLM_MAX_RETRIES",
        "CAREERPILOT_LLM_MAX_OUTPUT_TOKENS",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    def deny_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("Network is forbidden in the normal test suite")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
