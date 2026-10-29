"""Adversarial local authentication and application policy checks."""

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from careerpilot.application.security import SecurityDenied, validate_json_import, validate_outbound
from careerpilot.persistence.security import SecurityStore


def test_credentials_are_hashed_scoped_and_revocable(tmp_path: Path) -> None:
    store = SecurityStore(tmp_path / "security.sqlite3")
    first, second, resource = uuid4(), uuid4(), uuid4()
    token = store.issue(first)
    assert store.authenticate(token).id == first
    store.bind(first, "candidate", resource)
    with pytest.raises(SecurityDenied):
        store.require(second, "candidate", resource)
    with pytest.raises(SecurityDenied):
        store.bind(second, "candidate", resource)
    with sqlite3.connect(store.path) as connection:
        assert token not in repr(connection.execute("SELECT * FROM access_tokens").fetchall())
    store.revoke_group(first, resource)
    with pytest.raises(SecurityDenied):
        store.require(first, "candidate", resource)
    store.revoke_tokens(first)
    with pytest.raises(SecurityDenied):
        store.authenticate(token)


@pytest.mark.parametrize(
    "name,media,data",
    [
        ("../../data.json", "application/json", b"{}"),
        ("resume.pdf", "application/pdf", b"%PDF"),
        ("archive.zip", "application/zip", b"PK"),
        ("jobs.json", "application/json", b"a" * (256 * 1024 + 1)),
        ("a\\jobs.json", "application/json", b"{}"),
    ],
)
def test_invalid_uploads_rejected(name: str, media: str, data: bytes) -> None:
    with pytest.raises(ValueError):
        validate_json_import(name, media, data)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.openai.com/v1",
        "https://127.0.0.1/",
        "https://169.254.169.254/",
        "https://api.openai.com.evil.invalid/",
        "https://user:secret@api.openai.com/",
        "file:///etc/passwd",
        "https://api.openai.com:8443/",
    ],
)
def test_arbitrary_urls_cannot_be_fetched(url: str) -> None:
    with pytest.raises((SecurityDenied, ValueError)):
        validate_outbound(url, purpose="model", enabled=True)


def test_provider_export_requires_explicit_enablement() -> None:
    with pytest.raises(SecurityDenied):
        validate_outbound("https://api.openai.com/v1", purpose="model", enabled=False)
    validate_outbound("https://api.openai.com/v1", purpose="model", enabled=True)


def test_actual_profile_and_documents_deleted_or_retained_explicitly(tmp_path: Path) -> None:
    from careerpilot.application.storage import NotFound
    from careerpilot.domain.applications import ApplicationDocument, DocumentKind, DocumentVersion
    from careerpilot.domain.profile import CandidateProfile
    from careerpilot.persistence.privacy import purge_candidate
    from careerpilot.persistence.sqlite import SQLiteDatabase

    database = SQLiteDatabase(tmp_path / "business.sqlite3")
    database.initialize()
    security = SecurityStore(tmp_path / "security.sqlite3")
    owner, other = uuid4(), uuid4()
    candidate = CandidateProfile()
    doc = ApplicationDocument(candidate_id=candidate.id, kind=DocumentKind.RESUME)
    version = DocumentVersion(document_id=doc.id, number=1, content="Synthetic private CV")
    with database.transaction() as repository:
        for entity in (candidate, doc, version):
            repository.add(entity)
    security.bind(owner, "candidate", candidate.id)
    security.bind(owner, "document", doc.id, candidate.id)
    security.bind(owner, "version", version.id, candidate.id)
    with pytest.raises(SecurityDenied):
        purge_candidate(database, security, other, candidate.id)
    purge_candidate(database, security, owner, candidate.id)
    with database.transaction() as repository, pytest.raises(NotFound):
        repository.get(CandidateProfile, candidate.id)
    with pytest.raises(SecurityDenied):
        security.require(owner, "version", version.id)
