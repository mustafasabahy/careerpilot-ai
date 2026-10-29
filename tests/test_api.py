"""Authenticated API integration: real business workflows, simulated external effects."""

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from careerpilot.api.app import create_app
from careerpilot.domain.common import utc_now
from careerpilot.portal import Portal


@pytest.fixture
def api(tmp_path: Path) -> tuple[Portal, TestClient, dict[str, str], dict[str, str]]:
    Portal.initialize(tmp_path)
    portal = Portal(tmp_path)
    first, second = uuid4(), uuid4()
    one = {"Authorization": "Bearer " + portal.security.issue(first)}
    two = {"Authorization": "Bearer " + portal.security.issue(second)}
    return portal, TestClient(create_app(portal, enable_demo=True)), one, two


def test_api_schema_auth_size_and_safe_errors(
    api: tuple[Portal, TestClient, dict[str, str], dict[str, str]],
) -> None:
    _, client, one, _ = api
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").status_code == 200
    assert client.get("/api/profiles").status_code == 401
    assert client.post("/api/profiles", headers=one, json={"PRIVATE": "secret"}).status_code == 422
    response = client.post("/api/jobs/import", headers=one, content=b"x" * (128 * 1024 + 1))
    assert response.status_code == 413
    assert (
        "PRIVATE" not in client.post("/api/profiles", headers=one, json={"PRIVATE": "secret"}).text
    )
    schema = client.get("/openapi.json").json()
    assert "/api/threads/{identifier}/decision" in schema["paths"]
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert schema["paths"]["/api/profiles"]["get"]["security"] == [{"HTTPBearer": []}]
    assert client.get("/").status_code == 200
    assert "frame-ancestors 'none'" in client.get("/").headers["content-security-policy"]


def test_full_local_manager_flow_and_cross_user_isolation(
    api: tuple[Portal, TestClient, dict[str, str], dict[str, str]],
) -> None:
    _, client, one, two = api
    seeded = client.post("/api/seed", headers=one).json()["data"]
    candidate, job = seeded["candidate_id"], seeded["job_ids"][0]
    for path in (f"/api/profiles/{candidate}", f"/api/jobs/{job}"):
        assert client.get(path, headers=two).status_code == 404
    request = {
        "id": str(uuid4()),
        "created_at": utc_now().isoformat(),
        "candidate_id": candidate,
        "job_id": job,
    }
    assert client.post("/api/analysis", headers=two, json=request).status_code == 404
    response = client.post("/api/analysis", headers=one, json=request)
    assert response.status_code == 200, response.text
    analysis = response.json()["data"]
    assert analysis["status"] == "completed"
    prep_request = {**request, "id": str(uuid4()), "assessment_id": analysis["assessment_id"]}
    response = client.post("/api/preparations", headers=one, json=prep_request)
    assert response.status_code == 201, response.text
    preparation = response.json()["data"]
    assert preparation["status"] == "drafts_created"
    resume = next(d for d in preparation["record"]["documents"] if d["kind"] == "resume")
    assert client.get(f"/api/versions/{resume['version_id']}", headers=two).status_code == 404
    app_response = client.post(
        "/api/applications",
        headers=one,
        json={
            "candidate_id": candidate,
            "job_id": job,
            "document_version_id": resume["version_id"],
        },
    )
    assert app_response.status_code == 201, app_response.text
    application = app_response.json()["data"]
    proposal_response = client.post(
        f"/api/applications/{application['id']}/proposals",
        headers=one,
        json={
            "content": {
                "kind": "change_application",
                "target_status": "ready",
                "reason": "User reviewed the draft",
            }
        },
    )
    assert proposal_response.status_code == 202, proposal_response.text
    waiting = proposal_response.json()["data"]
    assert waiting["paused"] and waiting["review"]["status"] == "pending"
    proposal = waiting["review"]["proposal"]
    thread = proposal["thread_id"]
    decision = {
        "id": str(uuid4()),
        "proposal_id": proposal["id"],
        "thread_id": thread,
        "proposal_version": proposal["version"],
        "content_sha256": proposal["content_sha256"],
        "decision": "approve",
    }
    assert (
        client.post(f"/api/threads/{thread}/decision", headers=two, json=decision).status_code
        == 404
    )
    result = client.post(f"/api/threads/{thread}/decision", headers=one, json=decision)
    assert result.status_code == 200, result.text
    assert result.json()["data"]["review"]["status"] == "applied"
    assert (
        client.post(f"/api/threads/{thread}/decision", headers=one, json=decision).status_code
        == 409
    )
    assert client.get(f"/api/threads/{thread}/history", headers=two).status_code == 404
    assert (
        client.get("/api/applications", headers=one).json()["data"]["items"][0]["status"] == "ready"
    )
    assert client.get("/api/applications", headers=two).json()["data"]["items"] == []
    assert client.delete(f"/api/profiles/{candidate}", headers=one).status_code == 409


def test_malicious_json_does_not_grant_tools_or_profile_authority(
    api: tuple[Portal, TestClient, dict[str, str], dict[str, str]],
) -> None:
    _, client, one, _ = api
    response = client.post(
        "/api/profiles",
        headers=one,
        json={
            "profile": {"display_name": {"state": "missing"}},
            "explicitly_confirmed": True,
            "shell": "echo SECRET",
            "owner_id": str(uuid4()),
        },
    )
    assert response.status_code == 422 and "SECRET" not in response.text
    invalid_file = client.post(
        "/api/jobs/import",
        headers=one,
        files={"file": ("resume.exe", b"MZ", "application/octet-stream")},
    )
    assert invalid_file.status_code in {400, 422}
