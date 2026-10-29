"""Phase 4 end-to-end journeys through the real API and LangGraph runtime."""

from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_chat_agent import ScriptedModel, call
from test_chat_graph import graph_service, post

from careerpilot.api.app import create_app
from careerpilot.application.chat_agent import ConversationalAgent
from careerpilot.application.chat_agent_contracts import ModelReply
from careerpilot.application.conversations import MessageInput
from careerpilot.domain.common import utc_now
from careerpilot.portal import Portal
from careerpilot.providers.chat_settings import ChatSettings
from careerpilot.providers.factory import build_chat_model


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Portal, UUID, dict[str, str]]:
    Portal.initialize(tmp_path)
    portal, owner = Portal(tmp_path), uuid4()
    monkeypatch.setenv("CAREERPILOT_CHAT_MODE", "llm")
    return portal, owner, {"Authorization": "Bearer " + portal.security.issue(owner)}


def conversation(client: TestClient, headers: dict[str, str]) -> str:
    return str(client.post("/api/conversations", headers=headers).json()["data"]["id"])


def test_journey_a_informational_without_tools(api: tuple[Portal, UUID, dict[str, str]]) -> None:
    portal, _, headers = api
    model = ScriptedModel(
        ModelReply(
            text="I compare your confirmed facts to stored jobs. I do not invent experience."
        )
    )
    client = TestClient(create_app(portal, chat_model=model))
    result = post(client, headers, conversation(client, headers), "What can CareerPilot help with?")
    assert result["status"] == "completed" and result["mode"] == "llm"
    assert result["agent_metadata"]["steps"] == []
    assert (
        "invent" not in result["response"]["content"].lower()
        or "do not invent" in result["response"]["content"].lower()
    )


def test_journey_b_job_analysis(api: tuple[Portal, UUID, dict[str, str]]) -> None:
    portal, owner, headers = api
    seeded = portal.seed(owner)
    jobs = seeded["job_ids"]
    assert isinstance(jobs, list)
    client = TestClient(create_app(portal, chat_model=ScriptedModel(call("analyze_job"))))
    conv = conversation(client, headers)
    first = post(client, headers, conv, "Analyze this role against my profile")
    assert first["response"]["cards"][0]["kind"] == "selection"
    post(client, headers, conv, "Select", selection={"kind": "job", "id": str(jobs[0])})
    done = post(
        client,
        headers,
        conv,
        "Select",
        selection={"kind": "candidate", "id": str(seeded["candidate_id"])},
    )
    card = done["response"]["cards"][0]
    assert done["status"] == "completed" and card["kind"] == "analysis"
    assert card["payload"]["analysis"]["status"] in {"completed", "needs_information"}
    assert "hiring prediction" not in card["text"].lower() or "not a hiring" in card["text"].lower()


def test_journey_c_missing_information_continues(
    api: tuple[Portal, UUID, dict[str, str]],
) -> None:
    portal, owner, headers = api
    seeded = portal.seed(owner)
    jobs = seeded["job_ids"]
    assert isinstance(jobs, list)
    client = TestClient(create_app(portal, chat_model=ScriptedModel(call("missing_information"))))
    conv = conversation(client, headers)
    first = post(client, headers, conv, "What information is missing?")
    assert first["task_status"] == "awaiting_clarification"
    client = TestClient(create_app(Portal(portal.root), chat_model=ScriptedModel()))
    post(client, headers, conv, "Select", selection={"kind": "job", "id": str(jobs[0])})
    done = post(
        client,
        headers,
        conv,
        "Select",
        selection={"kind": "candidate", "id": str(seeded["candidate_id"])},
    )
    assert done["status"] == "completed" and done["response"]["cards"][0]["kind"] == "missing"


def test_journey_d_application_preparation(api: tuple[Portal, UUID, dict[str, str]]) -> None:
    portal, owner, headers = api
    seeded = portal.seed(owner)
    jobs = seeded["job_ids"]
    assert isinstance(jobs, list)
    client = TestClient(create_app(portal, chat_model=ScriptedModel(call("prepare_application"))))
    conv = conversation(client, headers)
    post(client, headers, conv, "Prepare my application without inventing experience")
    post(client, headers, conv, "Select", selection={"kind": "job", "id": str(jobs[0])})
    done = post(
        client,
        headers,
        conv,
        "Select",
        selection={"kind": "candidate", "id": str(seeded["candidate_id"])},
    )
    payload = done["response"]["cards"][0]["payload"]
    assert payload["status"] == "drafts_created"
    assert "SQL" not in payload["drafts"][0]["content"]


def test_journey_e_sensitive_action_requires_exact_approval(
    api: tuple[Portal, UUID, dict[str, str]],
) -> None:
    portal, _, headers = api
    client = TestClient(create_app(portal, chat_model=ScriptedModel(call("approve"))))
    result = post(client, headers, conversation(client, headers), "yes send it now")
    assert result["status"] == "failed"
    assert result["agent_metadata"]["steps"] == []


def test_journey_f_recovery_after_interrupted_tool(
    api: tuple[Portal, UUID, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    portal, owner, _ = api
    chat, _ = graph_service(portal.root, ScriptedModel(call("get_preferences")))
    conv = chat.create(owner)
    original = ConversationalAgent.act

    def crash(*args: object, **kwargs: object) -> None:
        raise SystemExit("controlled interruption")

    monkeypatch.setattr(ConversationalAgent, "act", crash)
    with pytest.raises(SystemExit):
        chat.submit(owner, conv.id, MessageInput(text="preferences", idempotency_key=uuid4()))
    with chat.store.transaction() as repo:
        op = repo.operations(conv.id)[0]
    import json
    import sqlite3

    with sqlite3.connect(portal.root / "conversations.sqlite3") as conn:
        payload = json.loads(
            conn.execute("SELECT payload FROM operations WHERE id=?", (str(op.id),)).fetchone()[0]
        )
        payload["claim_expires_at"] = (utc_now() - timedelta(seconds=1)).isoformat()
        conn.execute(
            "UPDATE operations SET payload=? WHERE id=?", (json.dumps(payload), str(op.id))
        )
    monkeypatch.setattr(ConversationalAgent, "act", original)
    restarted, _ = graph_service(
        portal.root, ScriptedModel(ModelReply(text="Preferences are unchanged."))
    )
    recovered = restarted.refresh(owner, conv.id, op.id)
    assert recovered.status == "completed"
    assert recovered.agent_metadata.steps[0].name == "get_preferences"


def test_journey_g_conversation_isolation(api: tuple[Portal, UUID, dict[str, str]]) -> None:
    portal, owner, headers = api
    other = uuid4()
    other_headers = {"Authorization": "Bearer " + portal.security.issue(other)}
    client = TestClient(
        create_app(portal, chat_model=ScriptedModel(ModelReply(text="Owner-only reply")))
    )
    conv = conversation(client, headers)
    post(client, headers, conv, "Hello")
    assert client.get("/api/conversations/" + conv, headers=other_headers).status_code == 404
    assert (
        client.post(
            "/api/conversations/" + conv + "/messages",
            headers=other_headers,
            json={"text": "leak", "idempotency_key": str(uuid4())},
        ).status_code
        == 404
    )
    with pytest.raises(PermissionError):
        graph_service(portal.root, ScriptedModel())[0].read(other, UUID(conv))


def test_journey_h_missing_api_key_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Portal.initialize(tmp_path)
    portal, owner = Portal(tmp_path), uuid4()
    monkeypatch.setenv("CAREERPILOT_CHAT_MODE", "llm")
    client = TestClient(create_app(portal))
    headers = {"Authorization": "Bearer " + portal.security.issue(owner)}
    config = client.get("/api/chat/config", headers=headers).json()["data"]
    assert config["mode"] == "llm" and config["ready"] is False
    assert "OPENAI_API_KEY" in config["error"]
    conv = conversation(client, headers)
    result = post(client, headers, conv, "Hello")
    assert result["status"] == "failed" and "OPENAI_API_KEY" in result["response"]["content"]
    monkeypatch.delenv("CAREERPILOT_CHAT_MODE")
    demo_root = tmp_path / "offline"
    Portal.initialize(demo_root)
    demo_portal = Portal(demo_root)
    demo = TestClient(create_app(demo_portal))
    demo_headers = {"Authorization": "Bearer " + demo_portal.security.issue(uuid4())}
    assert demo.get("/api/chat/config", headers=demo_headers).json()["data"]["mode"] == "demo"


def test_key_enables_default_model_without_source_edits(monkeypatch: pytest.MonkeyPatch) -> None:
    from careerpilot.application.extraction import ProviderConfigurationError

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-not-a-real-credential")
    settings = ChatSettings()
    assert settings.effective_mode == "llm" and settings.provider == "openai"
    assert settings.model == "gpt-5.4-mini"
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(ProviderConfigurationError):
        build_chat_model(ChatSettings(mode="llm"))
