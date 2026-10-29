"""Actual LangChain tool schemas/invocation; no model or agent is constructed."""

import asyncio
import json
from dataclasses import replace

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langsmith import get_tracing_context, tracing_context
from test_discovery import discovery_setup as discovery_setup
from test_discovery import make_service

from careerpilot.application.discovery import DiscoveryTools
from careerpilot.application.discovery_contracts import AccessGrant, Permission
from careerpilot.integrations.synthetic import SyntheticBundle
from careerpilot.persistence.sqlite import SQLiteDatabase
from careerpilot.tools.langchain import build_discovery_tools


def test_all_six_tool_schemas_hide_authority_and_unsafe_capabilities(
    discovery_setup: tuple[SQLiteDatabase, SyntheticBundle, AccessGrant, DiscoveryTools],
) -> None:
    _, _, grant, service = discovery_setup
    tools = build_discovery_tools(service)
    assert {tool.name for tool in tools} == {
        "search_synthetic_jobs",
        "search_local_jobs",
        "get_job",
        "get_candidate_facts",
        "get_company_info",
        "normalize_job_results",
    }
    for tool in tools:
        schema = convert_to_openai_tool(tool)
        encoded = json.dumps(schema)
        assert str(grant.actor_id) not in encoded
        assert "untrusted data" in encoded
        assert not {"actor_id", "grant", "path", "url", "sql", "shell", "permissions"} & set(
            tool.args
        )


def test_langchain_tools_call_the_same_authorized_application_services(
    discovery_setup: tuple[SQLiteDatabase, SyntheticBundle, AccessGrant, DiscoveryTools],
) -> None:
    _, bundle, _, service = discovery_setup
    tools = {tool.name: tool for tool in build_discovery_tools(service)}
    result = tools["search_synthetic_jobs"].invoke({"query": "Python", "limit": 1})
    assert result["jobs"][0]["job_id"] == str(bundle.jobs[0].id)
    local = tools["search_local_jobs"].invoke({"limit": 1})
    assert local["jobs"][0]["job_id"] == str(bundle.jobs[0].id)
    job = tools["get_job"].invoke({"source_id": "synthetic", "job_id": str(bundle.jobs[0].id)})
    assert job["trust"] == "untrusted_source_content"
    facts = tools["get_candidate_facts"].invoke({"candidate_id": str(bundle.profiles[0].id)})
    assert facts["candidate_id"] == str(bundle.profiles[0].id)
    company = tools["get_company_info"].invoke({"company_id": "example"})
    assert company["trust"] == "untrusted_mock"
    normalized = tools["normalize_job_results"].invoke(
        {
            "references": [
                {"source_id": "synthetic", "job_id": str(bundle.jobs[0].id)},
                {"source_id": "local", "job_id": str(bundle.jobs[0].id)},
            ]
        }
    )
    assert len(normalized["jobs"]) == 1 and normalized["duplicates_removed"] == 1


def test_langchain_validation_and_authorization_have_explicit_error_status(
    discovery_setup: tuple[SQLiteDatabase, SyntheticBundle, AccessGrant, DiscoveryTools],
) -> None:
    _, bundle, _, service = discovery_setup
    tools = {tool.name: tool for tool in build_discovery_tools(service)}
    invalid = tools["search_synthetic_jobs"].invoke({"limit": 100, "grant": "SYNTHETIC"})
    assert json.loads(invalid)["status"] == "error"
    assert "SYNTHETIC" not in invalid
    denied = tools["get_candidate_facts"].invoke({"candidate_id": str(bundle.profiles[1].id)})
    assert json.loads(denied)["code"] == "access_denied"
    tool_message = tools["get_candidate_facts"].invoke(
        {
            "type": "tool_call",
            "id": "synthetic-call",
            "name": "get_candidate_facts",
            "args": {"candidate_id": str(bundle.profiles[1].id)},
        }
    )
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.status == "error"


def test_optional_tools_do_not_override_bound_permissions(
    discovery_setup: tuple[SQLiteDatabase, SyntheticBundle, AccessGrant, DiscoveryTools],
) -> None:
    database, bundle, grant, _ = discovery_setup
    service = make_service(
        database, bundle, replace(grant, permissions=frozenset({Permission.SEARCH}))
    )
    tools = {tool.name: tool for tool in build_discovery_tools(service)}
    denied = tools["get_job"].invoke({"source_id": "synthetic", "job_id": str(bundle.jobs[0].id)})
    assert json.loads(denied)["code"] == "access_denied"


def test_sync_and_async_tool_calls_restore_tracing_context(
    discovery_setup: tuple[SQLiteDatabase, SyntheticBundle, AccessGrant, DiscoveryTools],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, service = discovery_setup
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    tool = build_discovery_tools(service)[0]
    with tracing_context(enabled=True):
        assert tool.invoke({"limit": 1})["status"] == "ok"
        assert get_tracing_context()["enabled"] is True
        assert asyncio.run(tool.ainvoke({"limit": 1}))["status"] == "ok"
        assert get_tracing_context()["enabled"] is True
