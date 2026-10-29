"""Configuration, readiness and stopped-service backup/restore verification."""

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from careerpilot.api.app import create_app
from careerpilot.backup import backup, restore
from careerpilot.deployment import ServerSettings
from careerpilot.portal import Portal


def test_production_requires_configuration_and_private_storage(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        ServerSettings(environment="production", directory=Path("relative"))
    settings = ServerSettings.model_validate(
        {
            "CAREERPILOT_ENVIRONMENT": "production",
            "directory": str(tmp_path),
            "public_origin": "https://careerpilot.example.com",
        }
    )
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError):
        settings.validate_storage()
    tmp_path.chmod(0o700)
    settings.validate_storage()


def test_readiness_storage_failure_is_safe_and_health_remains_process_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Portal.initialize(tmp_path)
    portal = Portal(tmp_path)
    client = TestClient(create_app(portal))

    def unavailable() -> sqlite3.Connection:
        raise sqlite3.OperationalError("PRIVATE credentials must not appear")

    monkeypatch.setattr(portal.database, "_connect", unavailable)
    response = client.get("/ready")
    assert response.status_code == 503 and response.json() == {"status": "unavailable"}
    assert "PRIVATE" not in response.text
    assert client.get("/health").status_code == 200


def test_backup_restore_preserves_business_security_and_job_definitions(tmp_path: Path) -> None:
    source, snapshot, restored = tmp_path / "source", tmp_path / "snapshot", tmp_path / "restored"
    Portal.initialize(source)
    portal = Portal(source)
    owner = uuid4()
    portal.seed(owner)
    token = portal.security.issue(owner)
    with pytest.raises(ValueError):
        backup(source, snapshot, confirmed_stopped=False)
    backup(source, snapshot, confirmed_stopped=True)
    restore(snapshot, restored)
    reopened = Portal(restored)
    assert reopened.security.authenticate(token).id == owner
    assert reopened.security.list(owner, "candidate") == portal.security.list(owner, "candidate")
    for job in reopened.security.list(owner, "job"):
        reopened.definitions(owner, job)
    assert TestClient(create_app(reopened)).get("/ready").status_code == 200
    with pytest.raises(ValueError):
        restore(snapshot, restored)
