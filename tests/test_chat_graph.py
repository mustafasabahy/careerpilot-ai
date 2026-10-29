"""Persistent agent graph: API journeys, actual SQLite restart and write replay boundaries."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver
from test_chat_agent import ScriptedModel, call

from careerpilot.agent_portal import PortalAgentTools
from careerpilot.api.app import create_app
from careerpilot.application.chat_agent import AgentProgress, ConversationalAgent
from careerpilot.application.chat_agent_contracts import ModelReply
from careerpilot.application.conversations import ConversationService, MessageInput, Operation
from careerpilot.chat_portal import PortalChatBackend
from careerpilot.domain.common import utc_now
from careerpilot.persistence.checkpoints import local_checkpointer
from careerpilot.persistence.conversations import ConversationDatabase
from careerpilot.portal import Portal
from careerpilot.workflows.chat_agent import ChatAgentGraph


def graph_service(root: Path, model: ScriptedModel) -> tuple[ConversationService, ChatAgentGraph]:
    portal = Portal(root)
    store = ConversationDatabase(root / "conversations.sqlite3")
    graph = ChatAgentGraph(
        ConversationalAgent(model, PortalAgentTools(portal)),
        store,
        lambda: local_checkpointer(root / "agent-checkpoints.sqlite3", separate_from=(store.path,)),
    )
    return ConversationService(store, PortalChatBackend(portal), graph), graph


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Portal, UUID, dict[str, str]]:
    Portal.initialize(tmp_path)
    portal, owner = Portal(tmp_path), uuid4()
    monkeypatch.setenv("CAREERPILOT_CHAT_MODE", "llm")
    return portal, owner, {"Authorization": "Bearer " + portal.security.issue(owner)}


def post(
    client: TestClient, headers: dict[str, str], identifier: str, text: str, **extra: object
) -> dict[str, Any]:
    response = client.post(
        "/api/conversations/" + identifier + "/messages",
        headers=headers,
        json={"text": text, "idempotency_key": str(uuid4()), **extra},
    )
    assert response.status_code == 200, response.text
    return dict(response.json()["data"])


def test_runtime_graph_multistep_and_private_state(
    runtime: tuple[Portal, UUID, dict[str, str]],
) -> None:
    portal, owner, headers = runtime
    portal.seed(owner)
    model = ScriptedModel(
        call("search_jobs", query="", offset=0),
        call("get_preferences"),
        ModelReply(text="Here are your local options."),
    )
    client = TestClient(create_app(portal, chat_model=model))
    identifier = client.post("/api/conversations", headers=headers).json()["data"]["id"]
    result = post(client, headers, identifier, "Show my jobs")
    assert result["status"] == "completed", result
    assert len(result["agent_metadata"]["steps"]) == 2
    assert "agent_progress" not in result
    with local_checkpointer(portal.root / "agent-checkpoints.sqlite3", separate_from=()) as saver:
        snapshots = list(saver.list({"configurable": {"thread_id": "chat-agent:" + result["id"]}}))
    assert len(snapshots) >= 6
    blob = json.dumps([s.checkpoint for s in snapshots], default=str)
    assert "Here are your" not in blob
    assert "chat-agent-v1" in blob
    assert len(model.history) == 3


def test_api_clarification_restart_and_preparation(
    runtime: tuple[Portal, UUID, dict[str, str]],
) -> None:
    portal, owner, headers = runtime
    seeded = portal.seed(owner)
    jobs = seeded["job_ids"]
    assert isinstance(jobs, list)
    model = ScriptedModel(call("prepare_application"))
    client = TestClient(create_app(portal, chat_model=model))
    identifier = client.post("/api/conversations", headers=headers).json()["data"]["id"]
    first = post(client, headers, identifier, "Prepare my application")
    assert first["response"]["cards"][0]["kind"] == "selection"
    client = TestClient(create_app(Portal(portal.root), chat_model=ScriptedModel()))
    second = post(
        client, headers, identifier, "Select", selection={"kind": "job", "id": str(jobs[0])}
    )
    assert second["response"]["cards"][0]["kind"] == "selection"
    third = post(
        client,
        headers,
        identifier,
        "Select",
        selection={"kind": "candidate", "id": str(seeded["candidate_id"])},
    )
    assert third["status"] == "completed", third
    assert third["response"]["cards"][0]["payload"]["status"] == "drafts_created"
    assert "SQL" not in third["response"]["cards"][0]["payload"]["drafts"][0]["content"]


def test_restart_between_model_and_tool_resumes_saved_call(
    runtime: tuple[Portal, UUID, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    portal, owner, _ = runtime
    chat, graph = graph_service(portal.root, ScriptedModel(call("get_preferences")))
    conv = chat.create(owner)
    original = ConversationalAgent.act

    def crash(*args: object, **kwargs: object) -> None:
        raise SystemExit("simulated process loss before tool")

    monkeypatch.setattr(ConversationalAgent, "act", crash)
    with pytest.raises(SystemExit):
        chat.submit(owner, conv.id, MessageInput(text="preferences", idempotency_key=uuid4()))
    with chat.store.transaction() as repo:
        op = repo.operations(conv.id)[0]
    # Simulate elapsed lease using the actual SQLite payload; no business records are reset.
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
    new_model = ScriptedModel(ModelReply(text="Your preferences are available."))
    restarted, _ = graph_service(portal.root, new_model)
    result = restarted.refresh(owner, conv.id, op.id)
    assert result.status == "completed", result
    assert len(result.agent_metadata.steps) == 1 and len(new_model.history) == 1
    assert restarted.refresh(owner, conv.id, op.id) == result


def test_checkpoint_commit_failure_does_not_repeat_completed_tool(
    runtime: tuple[Portal, UUID, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    portal, owner, _ = runtime
    model = ScriptedModel(call("get_preferences"), ModelReply(text="Preferences checked."))
    chat, graph = graph_service(portal.root, model)
    original = SqliteSaver.put
    broken = {"once": False}

    def put(self: Any, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        values = checkpoint.get("channel_values", {})
        if (
            metadata.get("source") == "loop"
            and values.get("stage") == "model"
            and metadata.get("step", 0) > 1
            and not broken["once"]
        ):
            broken["once"] = True
            raise OSError("simulated checkpoint failure")
        return original(self, config, checkpoint, metadata, new_versions)

    monkeypatch.setattr(SqliteSaver, "put", put)
    conv = chat.create(owner)
    result = chat.submit(owner, conv.id, MessageInput(text="preferences", idempotency_key=uuid4()))
    assert broken["once"] and result.status == "recoverable", result
    restarted, _ = graph_service(portal.root, model)
    recovered = restarted.refresh(owner, conv.id, result.id)
    assert recovered.status == "completed", recovered
    assert len(recovered.agent_metadata.steps) == 1


def test_checkpoint_foreign_owner_rejected(runtime: tuple[Portal, UUID, dict[str, str]]) -> None:
    portal, owner, _ = runtime
    chat, graph = graph_service(portal.root, ScriptedModel(ModelReply(text="Hello")))
    conv = chat.create(owner)
    op = Operation(
        conversation_id=conv.id,
        context=conv,
        request=MessageInput(text="Hi", idempotency_key=uuid4()),
        mode="llm",
    )
    with chat.store.transaction() as repo:
        repo.put_operation(op)
    with pytest.raises(PermissionError):
        graph.run(uuid4(), op, (), lambda _: None)
    with pytest.raises(PermissionError):
        graph.check({"version": "chat-agent-v1", "owner_id": str(uuid4())}, owner, op)


def test_interrupted_write_marker_is_unknown_not_repeated(
    runtime: tuple[Portal, UUID, dict[str, str]],
) -> None:
    portal, owner, _ = runtime
    chat, graph = graph_service(portal.root, ScriptedModel())
    conv = chat.create(owner)
    op = Operation(
        conversation_id=conv.id,
        context=conv,
        request=MessageInput(text="prepare", idempotency_key=uuid4()),
        mode="llm",
    )
    progress = AgentProgress(context=conv, stage="tools")
    from careerpilot.application.chat_agent_contracts import AgentMetadata, ToolStep

    progress.metadata = AgentMetadata(
        steps=(
            ToolStep(
                name="prepare_application", fingerprint="abc", effect="write", status="started"
            ),
        )
    )
    graph.agent.advance(progress, owner, op, lambda _: None)
    assert progress.status == "unknown" and progress.stage == "final"


def test_llm_proposal_exact_approval_and_restart(
    runtime: tuple[Portal, UUID, dict[str, str]],
) -> None:
    portal, owner, headers = runtime
    seeded = portal.seed(owner)
    jobs = seeded["job_ids"]
    assert isinstance(jobs, list)
    model = ScriptedModel(call("prepare_application"))
    client = TestClient(create_app(portal, chat_model=model))
    conv = client.post("/api/conversations", headers=headers).json()["data"]["id"]
    post(client, headers, conv, "Prepare a CV")
    post(client, headers, conv, "Select", selection={"kind": "job", "id": str(jobs[0])})
    draft = post(
        client,
        headers,
        conv,
        "Select",
        selection={"kind": "candidate", "id": str(seeded["candidate_id"])},
    )
    docs = draft["response"]["cards"][0]["payload"]["record"]["documents"]
    version = next(d["version_id"] for d in docs if d["kind"] == "resume")
    application = client.post(
        "/api/applications",
        headers=headers,
        json={
            "candidate_id": str(seeded["candidate_id"]),
            "job_id": str(jobs[0]),
            "document_version_id": version,
        },
    ).json()["data"]
    model.replies.extend(
        [
            call(
                "propose_recruiter_message",
                application_id=application["id"],
                subject="Synthetic interest",
                body="Please review my evidence-based draft.",
            ),
            ModelReply(text="Review the proposal using its controls."),
            call("approve"),
        ]
    )
    result = post(client, headers, conv, "Prepare a recruiter proposal for " + application["id"])
    assert result["task_status"] == "awaiting_approval", result
    thread = result["response"]["cards"][0]["payload"]["items"][0]
    assert thread["paused"] and thread["review"]["status"] == "pending"
    p = thread["review"]["proposal"]
    tid = p["thread_id"]
    post(client, headers, conv, "yes go ahead")
    assert (
        client.get("/api/threads/" + tid, headers=headers).json()["data"]["review"]["status"]
        == "pending"
    )
    decision = {
        "id": str(uuid4()),
        "thread_id": tid,
        "proposal_id": p["id"],
        "proposal_version": p["version"],
        "content_sha256": p["content_sha256"],
        "decision": "approve",
    }
    other = {"Authorization": "Bearer " + portal.security.issue(uuid4())}
    assert (
        client.post("/api/threads/" + tid + "/decision", headers=other, json=decision).status_code
        == 404
    )
    assert (
        client.post(
            "/api/threads/" + tid + "/decision",
            headers=headers,
            json={**decision, "content_sha256": "0" * 64},
        ).status_code
        == 409
    )
    client = TestClient(create_app(Portal(portal.root), chat_model=ScriptedModel()))
    approved = client.post("/api/threads/" + tid + "/decision", headers=headers, json=decision)
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["review"]["status"] == "simulated"
    assert (
        client.post("/api/threads/" + tid + "/decision", headers=headers, json=decision).status_code
        == 409
    )
    restored = client.get("/api/conversations/" + conv, headers=headers).json()["data"]
    cards = [
        card
        for message in restored["messages"]
        for card in message.get("cards", [])
        if card["kind"] == "approvals"
    ]
    assert cards[0]["payload"]["items"][0]["review"]["outcome"]["status"] == "simulated"


def test_missing_checkpoint_is_explicit_and_does_not_reexecute(
    runtime: tuple[Portal, UUID, dict[str, str]],
) -> None:
    portal, owner, _ = runtime
    chat, graph = graph_service(portal.root, ScriptedModel())
    conv = chat.create(owner)
    op = Operation(
        conversation_id=conv.id,
        context=conv,
        mode="llm",
        request=MessageInput(text="test", idempotency_key=uuid4()),
        claim_token=uuid4(),
    )
    progress = AgentProgress(context=conv)
    op = op.model_copy(update={"agent_progress": progress.model_dump(mode="json")})
    with chat.store.transaction() as repo:
        repo.put_operation(op)
    result = graph.run(owner, op, (), lambda _: None)
    assert result.status == "recoverable" and result.metadata.error_code == "checkpoint"
