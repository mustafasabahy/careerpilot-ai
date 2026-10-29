"""Additive schema upgrade, rollback and cross-aggregate document/assessment integrity."""

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from test_preparation import preparation_setup as preparation_setup

from careerpilot.application.preparation import PreparationService
from careerpilot.application.preparation_contracts import PreparationRequest, PreparationSuccess
from careerpilot.application.storage import IntegrityViolation
from careerpilot.domain.applications import ApplicationDocument, DocumentKind, DocumentVersion
from careerpilot.domain.profile import CandidateProfile
from careerpilot.integrations.synthetic import FIXTURE_TIME, fixture_id
from careerpilot.persistence.schema import SCHEMA, SCHEMA_VERSION
from careerpilot.persistence.sqlite import SQLiteDatabase
from careerpilot.workflows.preparation import PreparationWorkflow


def create_v1(path: Path) -> tuple[CandidateProfile, ApplicationDocument, DocumentVersion]:
    profile = CandidateProfile(id=fixture_id("old-candidate"), created_at=FIXTURE_TIME)
    document = ApplicationDocument(
        id=fixture_id("old-document"),
        created_at=FIXTURE_TIME,
        candidate_id=profile.id,
        kind=DocumentKind.RESUME,
    )
    version = DocumentVersion(
        id=fixture_id("old-version"),
        created_at=FIXTURE_TIME,
        document_id=document.id,
        number=1,
        content="Original synthetic CV",
        generated_by_ai=False,
    )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO candidates(id,payload) VALUES (?,?)",
            (str(profile.id), profile.model_dump_json()),
        )
        connection.execute(
            "INSERT INTO documents(id,candidate_id,payload) VALUES (?,?,?)",
            (str(document.id), str(profile.id), document.model_dump_json()),
        )
        connection.execute(
            "INSERT INTO versions(id,document_id,candidate_id,number,payload) VALUES (?,?,?,?,?)",
            (str(version.id), str(document.id), str(profile.id), 1, version.model_dump_json()),
        )
        connection.execute("PRAGMA user_version=1")
    return profile, document, version


def test_v1_to_v2_is_additive_and_preserves_original_payloads(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.sqlite3"
    profile, document, version = create_v1(path)
    before: dict[str, str] = {}
    with sqlite3.connect(path) as connection:
        for table in ("candidates", "documents", "versions"):
            before[table] = connection.execute(f"SELECT payload FROM {table}").fetchone()[0]
    database = SQLiteDatabase(path)
    database.initialize()
    database.initialize()
    with database.transaction() as repository:
        assert repository.get(CandidateProfile, profile.id) == profile
        assert repository.get(ApplicationDocument, document.id) == document
        assert repository.get(DocumentVersion, version.id) == version
        assert repository.latest_document_version(document.id) == version
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
        assert connection.execute("SELECT COUNT(*) FROM preparations").fetchone()[0] == 0
        for table, payload in before.items():
            assert connection.execute(f"SELECT payload FROM {table}").fetchone()[0] == payload
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_failed_migration_rolls_back_schema_and_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from careerpilot.persistence.schema import MIGRATION_1_TO_2

    path = tmp_path / "rollback.sqlite3"
    profile, _, _ = create_v1(path)
    monkeypatch.setattr(
        "careerpilot.persistence.sqlite.MIGRATION_1_TO_2",
        MIGRATION_1_TO_2 + "INVALID SYNTHETIC SQL;",
    )
    with pytest.raises(sqlite3.OperationalError):
        SQLiteDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute("SELECT payload FROM candidates").fetchone()[0]
            == profile.model_dump_json()
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='preparations'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='assessments_identity'"
            ).fetchone()
            is None
        )


def test_raw_preparation_relationship_constraints(
    preparation_setup: tuple[SQLiteDatabase, PreparationService, PreparationRequest],
) -> None:
    database, service, request = preparation_setup
    result = PreparationWorkflow(service).run(request)
    assert isinstance(result, PreparationSuccess)
    metadata = {item.kind: item for item in result.record.documents}
    with sqlite3.connect(database.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO preparations(id,candidate_id,job_id,assessment_id,"
                "resume_version_id,cover_version_id,payload) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    str(request.candidate_id),
                    str(request.job_id),
                    str(uuid4()),
                    str(metadata[DocumentKind.RESUME].version_id),
                    str(metadata[DocumentKind.COVER_LETTER].version_id),
                    "{}",
                ),
            )
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_repository_rejects_forged_content_hash_or_document_metadata(
    preparation_setup: tuple[SQLiteDatabase, PreparationService, PreparationRequest],
) -> None:
    from careerpilot.domain.preparation import PreparationRecord

    database, service, request = preparation_setup
    result = PreparationWorkflow(service).run(request)
    assert isinstance(result, PreparationSuccess)
    data = result.record.model_dump(mode="json")
    documents = [item.model_dump(mode="json") for item in result.record.documents]
    documents[0]["content_sha256"] = "0" * 64
    forged = PreparationRecord.model_validate({**data, "id": str(uuid4()), "documents": documents})
    with database.transaction() as repository:
        with pytest.raises(IntegrityViolation):
            repository.add(forged)


def test_existing_assessment_for_another_job_cannot_label_the_drafts(
    preparation_setup: tuple[SQLiteDatabase, PreparationService, PreparationRequest],
) -> None:
    from careerpilot.domain.jobs import JobFitAssessment
    from careerpilot.integrations.synthetic import synthetic_bundle

    database, service, request = preparation_setup
    result = PreparationWorkflow(service).run(request)
    assert isinstance(result, PreparationSuccess)
    other = synthetic_bundle().jobs[0]
    assessment = JobFitAssessment(candidate_id=request.candidate_id, job_id=other.id)
    with database.transaction() as repository:
        repository.add(other)
        repository.add(assessment)
    versions = {item.kind: item.version_id for item in result.record.documents}
    with sqlite3.connect(database.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO preparations(id,candidate_id,job_id,assessment_id,"
                "resume_version_id,cover_version_id,payload) VALUES (?,?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    str(request.candidate_id),
                    str(request.job_id),
                    str(assessment.id),
                    str(versions[DocumentKind.RESUME]),
                    str(versions[DocumentKind.COVER_LETTER]),
                    "{}",
                ),
            )
