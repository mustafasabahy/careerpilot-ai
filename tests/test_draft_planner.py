"""Real LangChain/SDK planning transport simulation and strict fact-selection boundaries."""

import json
from uuid import uuid4

import httpx2 as httpx
import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr
from test_preparation import preparation_setup as preparation_setup

from careerpilot.application.draft_generation import planning_input, validate_plan
from careerpilot.application.preparation import PreparationService
from careerpilot.application.preparation_contracts import (
    PreparationFailure,
    PreparationGenerationError,
    PreparationRequest,
    PreparationValidationError,
)
from careerpilot.domain.preparation import DraftPlan, FactKind
from careerpilot.persistence.sqlite import SQLiteDatabase
from careerpilot.providers.draft_planner import DeterministicDraftPlanner, OpenAIDraftPlanner
from careerpilot.providers.factory import build_draft_planner
from careerpilot.workflows.preparation import PreparationWorkflow


def test_default_planner_is_offline() -> None:
    assert isinstance(build_draft_planner(), DeterministicDraftPlanner)


def test_provider_sees_only_minimized_data_not_raw_cv_or_identity(
    preparation_setup: tuple[SQLiteDatabase, PreparationService, PreparationRequest],
) -> None:
    _, service, request = preparation_setup
    plan_input = planning_input(service.inspect(request).intelligence)
    captured: list[BaseMessage] = []
    expected = DeterministicDraftPlanner().plan(plan_input)

    def invoke(messages: list[BaseMessage]) -> object:
        captured.extend(messages)
        return {"raw": AIMessage(content=""), "parsed": expected, "parsing_error": None}

    actual = OpenAIDraftPlanner("synthetic-model", invoke).plan(plan_input)
    assert actual == expected
    assert isinstance(captured[0], SystemMessage) and isinstance(captured[1], HumanMessage)
    data = json.loads(str(captured[1].content))
    assert all(set(fact) == {"id", "kind", "text"} for fact in data["facts"])
    assert not any(
        fact["kind"] in {FactKind.NAME.value, FactKind.AUTHORIZATION.value}
        for fact in data["facts"]
    )
    assert "Original user CV" not in str(captured[1].content)
    assert "Synthetic Candidate" not in str(captured[1].content)
    assert "no tools" in str(captured[0].content).lower()


def test_malicious_uuid_selection_from_real_sdk_is_rejected_after_schema_parse(
    preparation_setup: tuple[SQLiteDatabase, PreparationService, PreparationRequest],
) -> None:
    _, service, request = preparation_setup
    plan_input = planning_input(service.inspect(request).intelligence)
    unknown = str(uuid4())

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["response_format"]["type"] == "json_schema"
        assert "tools" not in body
        return httpx.Response(
            200,
            json={
                "id": "synthetic-plan",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {"resume_fact_ids": [unknown], "cover_fact_ids": [unknown]}
                            ),
                        },
                    }
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = ChatOpenAI(
            model="gpt-4o-mini",
            api_key=SecretStr("synthetic-placeholder"),
            http_client=client,
            max_retries=0,
            use_responses_api=False,
        )
        structured = model.with_structured_output(
            DraftPlan, method="json_schema", strict=True, include_raw=True
        )
        proposed = OpenAIDraftPlanner("gpt-4o-mini", structured.invoke).plan(plan_input)
    with pytest.raises(PreparationValidationError):
        validate_plan(proposed, plan_input)


def test_planner_refusal_or_provider_failure_is_not_a_document(
    preparation_setup: tuple[SQLiteDatabase, PreparationService, PreparationRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, service, request = preparation_setup
    plan_input = planning_input(service.inspect(request).intelligence)
    planner = OpenAIDraftPlanner(
        "synthetic",
        lambda messages: {
            "raw": AIMessage(content="", additional_kwargs={"refusal": "SYNTHETIC"}),
            "parsed": None,
            "parsing_error": None,
        },
    )
    with pytest.raises(PreparationGenerationError):
        planner.plan(plan_input)
    monkeypatch.setattr(service, "generate", lambda context: planner.plan(plan_input))
    result = PreparationWorkflow(service).run(request)
    assert isinstance(result, PreparationFailure) and result.code == "generation_failed"
