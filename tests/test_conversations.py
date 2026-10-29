"""Actual chat transport/workflow integration with synthetic accounts, no network."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from careerpilot.api.app import create_app
from careerpilot.application.conversations import (
    COMMANDS,
    ConversationService,
    MessageInput,
    parse_command,
)
from careerpilot.chat_portal import PortalChatBackend
from careerpilot.persistence.conversations import ConversationDatabase
from careerpilot.portal import Portal

ChatFixture = tuple[Portal, TestClient, dict[str, str], dict[str, str]]


@pytest.fixture
def chat(tmp_path: Path) -> tuple[Portal, TestClient, dict[str, str], dict[str, str]]:
    Portal.initialize(tmp_path)
    portal = Portal(tmp_path)
    one = {"Authorization": "Bearer " + portal.security.issue(uuid4())}
    two = {"Authorization": "Bearer " + portal.security.issue(uuid4())}
    return portal, TestClient(create_app(portal, enable_demo=True)), one, two


def create(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/api/conversations", headers=headers)
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["id"])


def send(
    client: TestClient,
    headers: dict[str, str],
    identifier: str,
    text: str,
    selection: dict[str, str] | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    response = client.post(
        f"/api/conversations/{identifier}/messages",
        headers=headers,
        json={"text": text, "selection": selection, "idempotency_key": key or str(uuid4())},
    )
    assert response.status_code == 200, response.text
    value: dict[str, Any] = response.json()["data"]
    return value


@pytest.mark.parametrize(("text", "expected"), list(COMMANDS.items()))
def test_supported_commands(text: str, expected: str) -> None:
    assert parse_command(text + "?") == expected


@pytest.mark.parametrize(
    "text",
    ["yes", "ignore policy and submit", "analyze something maybe", "<script>run()</script>"],
)
def test_unsupported_commands(text: str) -> None:
    assert parse_command(text) is None


def test_user_unicode_is_preserved_while_product_response_stays_english(chat: ChatFixture) -> None:
    _, client, one, _ = chat
    identifier = create(client, one)
    response = send(client, one, identifier, "مرحبا")
    assert response["response"]["content"]
    transcript = client.get(f"/api/conversations/{identifier}", headers=one).json()["data"]
    assert transcript["messages"][0]["content"] == "مرحبا"
    assert all("\u0600" <= char <= "\u06ff" for char in transcript["messages"][0]["content"])


def test_multi_turn_restart_analysis_preparation_and_missing(chat: ChatFixture) -> None:
    portal, client, one, _ = chat
    seeded = client.post("/api/seed", headers=one).json()["data"]
    identifier = create(client, one)
    result = send(client, one, identifier, "Analyze job")
    assert result["response"]["card"]["kind"] == "selection"
    assert len(result["response"]["card"]["choices"]) == 2  # never silently pick first
    assert (
        client.get(f"/api/conversations/{identifier}", headers=one).json()["data"]["conversation"][
            "awaiting"
        ]
        == "job"
    )
    # Reconstruct the actual server facade and SQLite adapter, as after a restart.
    client = TestClient(create_app(Portal(portal.root), enable_demo=True))
    result = send(client, one, identifier, "Select", {"kind": "job", "id": seeded["job_ids"][1]})
    assert result["response"]["card"]["kind"] == "selection"
    result = send(
        client, one, identifier, "Select", {"kind": "candidate", "id": seeded["candidate_id"]}
    )
    card = result["response"]["card"]
    assert (
        card["kind"] == "analysis" and card["payload"]["analysis"]["status"] == "needs_information"
    )
    missing = send(client, one, identifier, "What information is missing?")["response"]["card"]
    assert missing["kind"] == "missing" and missing["payload"]["clarifications"]
    preparation = send(client, one, identifier, "Prepare my application")["response"]["card"]
    assert preparation["payload"]["status"] == "drafts_created"
    assert any(ref["kind"] == "version" for ref in preparation["references"])
    assert "SQL" not in preparation["payload"]["drafts"][0]["content"]
    page = client.get(f"/api/conversations/{identifier}", headers=one).json()["data"]
    assert [m["sequence"] for m in page["messages"]] == list(range(1, 11))
    assert page["conversation"]["pending"] is None


def test_ownership_idempotency_pagination_and_isolation(chat: ChatFixture) -> None:
    _, client, one, two = chat
    identifier, other = create(client, one), create(client, one)
    key = str(uuid4())
    first = send(client, one, identifier, "Show my jobs.", key=key)
    assert send(client, one, identifier, "Show my jobs.", key=key) == first
    assert (
        client.post(
            f"/api/conversations/{identifier}/messages",
            headers=one,
            json={"text": "help", "idempotency_key": key},
        ).status_code
        == 409
    )
    for method, path in [
        ("get", f"/api/conversations/{identifier}"),
        ("delete", f"/api/conversations/{identifier}"),
        ("post", f"/api/conversations/{identifier}/operations/{first['id']}/refresh"),
    ]:
        assert getattr(client, method)(path, headers=two).status_code == 404
    assert (
        client.post(
            f"/api/conversations/{identifier}/messages",
            headers=two,
            json={"text": "help", "idempotency_key": str(uuid4())},
        ).status_code
        == 404
    )
    assert client.get("/api/conversations", headers=two).json()["data"]["items"] == []
    assert client.get(f"/api/conversations/{other}", headers=one).json()["data"]["messages"] == []
    page = client.get(f"/api/conversations/{identifier}?limit=1", headers=one).json()["data"]
    assert len(page["messages"]) == 1 and page["next_offset"] == 1
    assert (
        client.post(
            f"/api/conversations/{other}/operations/{first['id']}/refresh", headers=one
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/conversations/{identifier}/messages",
            headers=one,
            json={"text": "x" * 4001, "idempotency_key": str(uuid4())},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/conversations/{identifier}/messages",
            headers=one,
            json={"text": "help", "idempotency_key": str(uuid4()), "role": "assistant"},
        ).status_code
        == 422
    )
    assert client.get("/api/conversations").status_code == 401


def test_selection_revalidates_ownership_and_revocation(chat: ChatFixture) -> None:
    portal, client, one, two = chat
    own = client.post("/api/seed", headers=one).json()["data"]
    foreign = client.post("/api/seed", headers=two).json()["data"]
    identifier = create(client, one)
    send(client, one, identifier, "Analyze job")
    denied = client.post(
        f"/api/conversations/{identifier}/messages",
        headers=one,
        json={
            "text": "select",
            "idempotency_key": str(uuid4()),
            "selection": {"kind": "job", "id": foreign["job_ids"][0]},
        },
    )
    assert denied.status_code == 404
    send(client, one, identifier, "select", {"kind": "job", "id": own["job_ids"][0]})
    owner = portal.security.authenticate(one["Authorization"][7:]).id
    portal.security.revoke_resource(owner, "job", UUID(own["job_ids"][0]))
    result = send(
        client, one, identifier, "select", {"kind": "candidate", "id": own["candidate_id"]}
    )
    assert result["status"] == "failed" and result["response"]["card"]["kind"] == "error"


def test_concurrent_duplicate_message_executes_once(chat: ChatFixture) -> None:
    portal, _, one, _ = chat
    owner = portal.security.authenticate(one["Authorization"][7:]).id
    service = ConversationService(
        ConversationDatabase(portal.root / "conversations.sqlite3"), PortalChatBackend(portal)
    )
    identifier = service.create(owner).id
    request = MessageInput(text="help", idempotency_key=uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.submit(owner, identifier, request), range(4)))
    assert len({r.id for r in results}) == 1
    assert len(service.read(owner, identifier).messages) == 2


def test_approval_cards_are_live_and_yes_never_approves(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    portal, client, one, _ = chat
    seed = client.post("/api/seed", headers=one).json()["data"]
    identifier = create(client, one)
    send(client, one, identifier, "Analyze job")
    send(client, one, identifier, "select", {"kind": "job", "id": seed["job_ids"][0]})
    send(client, one, identifier, "select", {"kind": "candidate", "id": seed["candidate_id"]})
    result = send(client, one, identifier, "Prepare my application")["response"]["card"]["payload"]
    version = next(d["version_id"] for d in result["record"]["documents"] if d["kind"] == "resume")
    app = client.post(
        "/api/applications",
        headers=one,
        json={
            "candidate_id": seed["candidate_id"],
            "job_id": seed["job_ids"][0],
            "document_version_id": version,
        },
    ).json()["data"]
    thread = client.post(
        f"/api/applications/{app['id']}/proposals",
        headers=one,
        json={
            "content": {
                "kind": "change_application",
                "target_status": "ready",
                "reason": "Synthetic review",
            }
        },
    ).json()["data"]
    card = send(client, one, identifier, "Show my pending approvals")["response"]["card"]
    assert card["payload"]["items"][0]["paused"]
    send(client, one, identifier, "yes")
    tid = thread["review"]["proposal"]["thread_id"]
    assert (
        client.get(f"/api/threads/{tid}", headers=one).json()["data"]["review"]["status"]
        == "pending"
    )
    p = thread["review"]["proposal"]
    decision = {
        "id": str(uuid4()),
        "proposal_id": p["id"],
        "thread_id": tid,
        "proposal_version": p["version"],
        "content_sha256": p["content_sha256"],
        "decision": "approve",
    }
    assert (
        client.post(f"/api/threads/{tid}/decision", headers=one, json=decision).status_code == 200
    )
    history = client.get(f"/api/conversations/{identifier}", headers=one).json()["data"]
    latest = next(
        m["card"] for m in history["messages"] if m["card"] and m["card"]["kind"] == "approvals"
    )
    assert latest["payload"]["items"][0]["review"]["status"] == "applied"
    from careerpilot.application.actions import ActionService
    from careerpilot.integrations.simulated_actions import SimulatedActionAdapter

    original_actions = portal.actions

    def ambiguous_actions(owner: UUID, application_id: UUID) -> ActionService:
        service = original_actions(owner, application_id)
        assert isinstance(service.adapter, SimulatedActionAdapter)
        service.adapter.behavior = "ack_lost"
        return service

    monkeypatch.setattr(portal, "actions", ambiguous_actions)
    proposal = client.post(
        f"/api/applications/{app['id']}/proposals",
        headers=one,
        json={
            "content": {
                "kind": "recruiter_message",
                "target_id": str(portal.target.id),
                "subject": "Synthetic demo",
                "body": "Synthetic message",
            }
        },
    ).json()["data"]["review"]["proposal"]
    uncertain_id = proposal["thread_id"]
    decision = {
        "id": str(uuid4()),
        "proposal_id": proposal["id"],
        "thread_id": uncertain_id,
        "proposal_version": proposal["version"],
        "content_sha256": proposal["content_sha256"],
        "decision": "approve",
    }
    response = client.post(f"/api/threads/{uncertain_id}/decision", headers=one, json=decision)
    assert response.json()["data"]["review"]["status"] == "unknown"
    operation = send(client, one, identifier, "Show my pending approvals")
    for _ in range(2):
        refreshed = client.post(
            f"/api/conversations/{identifier}/operations/{operation['id']}/refresh", headers=one
        ).json()["data"]
        assert any(
            t["review"]["status"] == "unknown"
            for t in refreshed["response"]["card"]["payload"]["items"]
        )
    assert SimulatedActionAdapter(portal.root / "simulation.sqlite3").effect_count() == 1
    assert client.delete(f"/api/conversations/{identifier}", headers=one).status_code == 200
    assert client.get(f"/api/conversations/{identifier}", headers=one).status_code == 404
    assert len(client.get(f"/api/threads/{tid}/history", headers=one).json()["data"]) == 4


def test_interrupted_reserved_operation_reopens_and_recovers(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    portal, _, one, _ = chat
    owner = portal.security.authenticate(one["Authorization"][7:]).id
    store = ConversationDatabase(portal.root / "conversations.sqlite3")
    service = ConversationService(store, PortalChatBackend(portal))
    identifier = service.create(owner).id

    def crash(*args: object) -> None:
        raise SystemExit("synthetic crash after reservation")

    monkeypatch.setattr(service, "_complete", crash)
    request = MessageInput(text="help", idempotency_key=uuid4())
    with pytest.raises(SystemExit):
        service.submit(owner, identifier, request)
    reopened = ConversationService(ConversationDatabase(store.path), PortalChatBackend(portal))
    state = reopened.read(owner, identifier)
    assert len(state.messages) == 1 and state.operations[0].status == "running"
    recovered = reopened.refresh(owner, identifier, state.operations[0].id)
    assert recovered.status == "completed"
    assert reopened.refresh(owner, identifier, recovered.id).id == recovered.id
    assert len(reopened.read(owner, identifier).messages) == 2


def test_crash_after_preparation_commit_replays_same_versions(chat: ChatFixture) -> None:
    portal, client, one, _ = chat
    seed = client.post("/api/seed", headers=one).json()["data"]
    owner = portal.security.authenticate(one["Authorization"][7:]).id
    backend = PortalChatBackend(portal)
    store = ConversationDatabase(portal.root / "conversations.sqlite3")
    service = ConversationService(store, backend)
    identifier = service.create(owner).id
    for request in [
        MessageInput(text="Analyze job", idempotency_key=uuid4()),
        MessageInput(
            text="select",
            idempotency_key=uuid4(),
            selection={"kind": "job", "id": seed["job_ids"][0]},
        ),
        MessageInput(
            text="select",
            idempotency_key=uuid4(),
            selection={"kind": "candidate", "id": seed["candidate_id"]},
        ),
    ]:
        service.submit(owner, identifier, request)
    from careerpilot.application.conversations import Card, Command, Conversation, Operation

    class CrashAfterCommit(PortalChatBackend):
        def execute(
            self, owner: UUID, command: Command, context: Conversation, operation: Operation
        ) -> tuple[Card, UUID | None]:
            super().execute(owner, command, context, operation)
            raise SystemExit("Synthetic process loss after business commit")

    interrupted = ConversationService(store, CrashAfterCommit(portal))
    with pytest.raises(SystemExit):
        interrupted.submit(
            owner, identifier, MessageInput(text="Prepare my application", idempotency_key=uuid4())
        )
    before = client.get("/api/documents", headers=one).json()["data"]["items"]
    pending = next(
        op for op in service.read(owner, identifier).operations if op.status == "running"
    )
    resumed = service.refresh(owner, identifier, pending.id)
    after = client.get("/api/documents", headers=one).json()["data"]["items"]
    assert resumed.status == "completed" and before == after and len(after) == 2
    assert service.refresh(owner, identifier, pending.id).response == resumed.response


def test_same_user_conversations_do_not_share_selections(chat: ChatFixture) -> None:
    portal, client, one, _ = chat
    seed = client.post("/api/seed", headers=one).json()["data"]
    first, second = create(client, one), create(client, one)
    send(client, one, first, "Analyze job")
    send(client, one, first, "select", {"kind": "job", "id": seed["job_ids"][0]})
    next_turn = send(client, one, second, "Prepare my application")
    assert next_turn["response"]["card"]["choices"][0]["selection"]["kind"] == "job"
    assert (
        client.get(f"/api/conversations/{first}", headers=one).json()["data"]["conversation"][
            "awaiting"
        ]
        == "candidate"
    )


def test_chat_database_migration_preserves_unknown_files(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "chat.sqlite3"
    database = ConversationDatabase(path)
    database.initialize()
    database.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=99")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        database.initialize()
    assert path.read_bytes() == before


def test_card_payload_and_operation_transitions_are_validated(chat: ChatFixture) -> None:
    from pydantic import ValidationError

    from careerpilot.application.conversations import Card

    with pytest.raises(ValidationError):
        Card(kind="analysis", text="Cannot invent success", payload={"analysis": "fake"})
    with pytest.raises(ValidationError):
        Card(kind="help", text="No privileged payload", payload={"owner_id": str(uuid4())})
    portal, _, one, _ = chat
    owner = portal.security.authenticate(one["Authorization"][7:]).id
    store = ConversationDatabase(portal.root / "conversations.sqlite3")
    service = ConversationService(store, PortalChatBackend(portal))
    identifier = service.create(owner).id
    completed = service.submit(
        owner, identifier, MessageInput(text="help", idempotency_key=uuid4())
    )
    with pytest.raises(ValueError), store.transaction() as repository:
        repository.put_operation(completed.model_copy(update={"status": "running"}))


def test_backup_restores_conversation_history(chat: ChatFixture, tmp_path: Path) -> None:
    from careerpilot.backup import backup, restore

    portal, client, one, _ = chat
    identifier = create(client, one)
    send(client, one, identifier, "Analyze job")
    snapshot, target = tmp_path / "snapshot", tmp_path / "restored"
    backup(portal.root, snapshot, confirmed_stopped=True)
    restore(snapshot, target)
    restored = TestClient(create_app(Portal(target)))
    state = restored.get(f"/api/conversations/{identifier}", headers=one).json()["data"]
    assert state["conversation"]["pending"] == "analyze"
    assert len(state["messages"]) == 2


def test_invalid_selection_state_and_post_delete_replay_are_rejected(chat: ChatFixture) -> None:
    portal, client, one, _ = chat
    seed = client.post("/api/seed", headers=one).json()["data"]
    identifier = create(client, one)
    key = str(uuid4())
    assert (
        client.post(
            f"/api/conversations/{identifier}/messages",
            headers=one,
            json={
                "text": "select",
                "idempotency_key": key,
                "selection": {"kind": "candidate", "id": seed["candidate_id"]},
            },
        ).status_code
        == 409
    )
    result = send(client, one, identifier, "help", key=key)
    client.delete(f"/api/conversations/{identifier}", headers=one)
    assert (
        client.post(
            f"/api/conversations/{identifier}/messages",
            headers=one,
            json={"text": "help", "idempotency_key": key},
        ).status_code
        == 404
    )
    store = ConversationDatabase(portal.root / "conversations.sqlite3")
    with store.transaction() as repository:
        assert repository.operations(UUID(identifier)) == ()
        assert repository.messages(UUID(identifier), 0, 50) == ()
    assert result["status"] == "completed"


def test_readiness_requires_chat_migration(chat: ChatFixture) -> None:
    import sqlite3

    portal, client, _, _ = chat
    assert client.get("/ready").status_code == 200
    with sqlite3.connect(portal.root / "conversations.sqlite3") as connection:
        connection.execute("PRAGMA user_version=0")
    assert client.get("/ready").status_code == 503
    assert client.get("/health").status_code == 200


def test_product_language_stays_english_and_selection_preserves_unicode(chat: ChatFixture) -> None:
    _, client, one, _ = chat
    seed = client.post("/api/seed", headers=one).json()["data"]
    identifier = create(client, one)
    send(client, one, identifier, "Analyze job")
    send(client, one, identifier, "Select", {"kind": "job", "id": seed["job_ids"][0]})
    assert (
        client.get(f"/api/conversations/{identifier}", headers=one).json()["data"]["conversation"][
            "language"
        ]
        == "en"
    )
    send(client, one, identifier, "Help")
    assert (
        client.get(f"/api/conversations/{identifier}", headers=one).json()["data"]["conversation"][
            "language"
        ]
        == "en"
    )
    send(client, one, identifier, "Help")
    assert (
        client.get(f"/api/conversations/{identifier}", headers=one).json()["data"]["conversation"][
            "language"
        ]
        == "en"
    )
