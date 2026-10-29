"""Application contracts, scope enforcement, source errors and pure deduplication."""

import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError

from careerpilot.application.discovery import DiscoveryTools
from careerpilot.application.discovery_contracts import (
    AccessDenied,
    AccessGrant,
    CandidateFactsInput,
    CompanyInfo,
    DiscoveryConflict,
    InvalidToolInput,
    JobRecord,
    JobReference,
    NormalizeJobsInput,
    PayloadLimitExceeded,
    Permission,
    ResourceNotFound,
    SearchBatch,
    SearchHit,
    SearchJobsInput,
    SourceDataError,
    SourceFailure,
    SourcePage,
    SourceRateLimited,
    SourceTimeout,
    UnknownSource,
)
from careerpilot.application.discovery_normalization import deduplicate
from careerpilot.domain.jobs import JobPosting
from careerpilot.integrations.local_jobs import MAX_IMPORT_BYTES, LocalJobSource
from careerpilot.integrations.mock_companies import MockCompanyAdapter
from careerpilot.integrations.synthetic import (
    FIXTURE_TIME,
    SyntheticBundle,
    fixture_id,
    seed_synthetic,
    synthetic_bundle,
)
from careerpilot.persistence.sqlite import SQLiteDatabase


@pytest.fixture
def discovery_setup(
    tmp_path: Path,
) -> tuple[SQLiteDatabase, SyntheticBundle, AccessGrant, DiscoveryTools]:
    database = SQLiteDatabase(tmp_path / "discovery.sqlite3")
    database.initialize()
    bundle = synthetic_bundle()
    with database.transaction() as repository:
        seed_synthetic(repository, bundle)
    grant = AccessGrant(
        fixture_id("actor"),
        frozenset(Permission),
        frozenset({"synthetic", "local"}),
        frozenset({bundle.profiles[0].id}),
        frozenset({"example"}),
    )
    tools = make_service(database, bundle, grant)
    return database, bundle, grant, tools


def make_service(
    database: SQLiteDatabase, bundle: SyntheticBundle, grant: AccessGrant
) -> DiscoveryTools:
    source = LocalJobSource.synthetic(bundle)
    companies = MockCompanyAdapter(
        (
            CompanyInfo.model_validate(
                {
                    "company_id": "example",
                    "name": "Example",
                    "description": "Synthetic company facts",
                    "source_url": "https://example.test/company",
                    "retrieved_at": FIXTURE_TIME,
                }
            ),
        )
    )
    return DiscoveryTools(
        grant, {"synthetic": source, "local": source}, companies, database.transaction, bundle.rules
    )


def discovery_request(bundle: SyntheticBundle, **overrides: object) -> dict[str, object]:
    return {
        "id": fixture_id("discovery-run"),
        "created_at": FIXTURE_TIME,
        "candidate_id": bundle.profiles[0].id,
        "source_ids": ["synthetic", "local"],
        "query": "Python",
        "limit": 5,
        **overrides,
    }


@pytest.mark.parametrize(
    ("schema", "raw"),
    [
        (SearchJobsInput, {"limit": 0}),
        (SearchJobsInput, {"limit": 11}),
        (SearchJobsInput, {"limit": True}),
        (SearchJobsInput, {"limit": "3"}),
        (SearchJobsInput, {"source_ids": []}),
        (SearchJobsInput, {"source_ids": ["synthetic"] * 2}),
        (SearchJobsInput, {"source_ids": ["a", "b", "c", "d"]}),
        (SearchJobsInput, {"source_ids": ["https://127.0.0.1"]}),
        (SearchJobsInput, {"source_ids": ["../../secret"]}),
        (SearchJobsInput, {"query": "x" * 161}),
        (JobReference, {"source_id": "synthetic", "job_id": "invalid"}),
        (CandidateFactsInput, {"candidate_id": uuid4(), "fields": ["display_name"]}),
        (CandidateFactsInput, {"candidate_id": uuid4(), "fields": ["skills", "skills"]}),
        (CandidateFactsInput, {"candidate_id": uuid4(), "per_field_limit": 11}),
        (NormalizeJobsInput, {"references": []}),
        (SearchJobsInput, {"actor_id": str(uuid4())}),
        (SearchJobsInput, {"url": "http://169.254.169.254"}),
        (SearchJobsInput, {"path": "/etc/passwd"}),
    ],
)
def test_invalid_tool_schemas(schema: type[BaseModel], raw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        schema.model_validate(raw)


def test_search_normalizes_and_bounds_results(
    discovery_setup: tuple[SQLiteDatabase, SyntheticBundle, AccessGrant, DiscoveryTools],
) -> None:
    _, bundle, _, service = discovery_setup
    result = service.search_jobs(
        {"query": " PYTHON ", "source_ids": ["synthetic", "local"], "limit": 1}
    )
    assert len(result.jobs) == 1
    assert result.jobs[0].job_id == bundle.jobs[0].id
    assert result.duplicates_removed == 1
    assert result.more_available
    assert len(result.jobs[0].references) == 2
    assert result.trust == "untrusted_source_content"
    assert result.jobs[0].references[0].source.retrieved_at == FIXTURE_TIME
    assert service.search_jobs({"query": "no-such-synthetic-role"}).jobs == ()


def test_retrieve_job_and_mock_company_without_fetching_url(
    discovery_setup: tuple[SQLiteDatabase, SyntheticBundle, AccessGrant, DiscoveryTools],
) -> None:
    _, bundle, _, service = discovery_setup
    job = service.retrieve_job({"source_id": "synthetic", "job_id": str(bundle.jobs[0].id)})
    assert job.job == bundle.jobs[0]
    company = service.company_info({"company_id": "example"})
    assert str(company.source_url) == "https://example.test/company"
    assert company.trust == "untrusted_mock"
    with pytest.raises(ResourceNotFound):
        service.retrieve_job({"source_id": "synthetic", "job_id": uuid4()})


def test_profile_facts_are_scoped_minimal_and_explicit_about_omissions(
    discovery_setup: tuple[SQLiteDatabase, SyntheticBundle, AccessGrant, DiscoveryTools],
) -> None:
    _, bundle, _, service = discovery_setup
    result = service.candidate_facts({"candidate_id": bundle.profiles[0].id, "per_field_limit": 1})
    assert len(result.skills) == 1 and len(result.languages) == 1
    assert {item.field.value: item.count for item in result.omitted} == {
        "skills": 1,
        "languages": 1,
    }
    assert result.work_authorized is not None and result.work_authorized.value is True
    assert "display_name" not in result.model_dump()
    assert result.skills[0].evidence == bundle.profiles[0].skills[0].evidence
    with pytest.raises(AccessDenied):
        service.candidate_facts({"candidate_id": bundle.profiles[1].id})
    assert (
        service.candidate_facts(
            {"candidate_id": bundle.profiles[0].id, "fields": ["skills"]}
        ).work_authorized
        is None
    )


@pytest.mark.parametrize("permission", list(Permission))
def test_permissions_are_enforced_outside_input(
    discovery_setup: tuple[SQLiteDatabase, SyntheticBundle, AccessGrant, DiscoveryTools],
    permission: Permission,
) -> None:
    database, bundle, grant, _ = discovery_setup
    service = make_service(
        database, bundle, replace(grant, permissions=frozenset(set(Permission) - {permission}))
    )
    with pytest.raises(AccessDenied):
        if permission == Permission.SEARCH:
            service.search_jobs({})
        elif permission == Permission.READ_JOB:
            service.retrieve_job({"source_id": "synthetic", "job_id": bundle.jobs[0].id})
        elif permission == Permission.READ_CANDIDATE:
            service.candidate_facts({"candidate_id": bundle.profiles[0].id})
        elif permission == Permission.READ_COMPANY:
            service.company_info({"company_id": "example"})
        else:
            service.authorize_discovery(discovery_request(bundle))


def test_resource_scope_and_extra_authority_cannot_be_overridden(
    discovery_setup: tuple[SQLiteDatabase, SyntheticBundle, AccessGrant, DiscoveryTools],
) -> None:
    database, bundle, grant, service = discovery_setup
    scoped = make_service(
        database,
        bundle,
        replace(grant, source_ids=frozenset({"synthetic"}), company_ids=frozenset()),
    )
    with pytest.raises(AccessDenied):
        scoped.search_jobs({"source_ids": ["local"]})
    with pytest.raises(AccessDenied):
        scoped.company_info({"company_id": "example"})
    with pytest.raises(InvalidToolInput):
        service.search_jobs({"grant": {"permissions": ["all"]}})
    configured_missing = make_service(
        database, bundle, replace(grant, source_ids=frozenset({"missing"}))
    )
    with pytest.raises(UnknownSource):
        configured_missing.search_jobs({"source_ids": ["missing"]})


class FailingSource:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure
        self.calls: list[float] = []

    def search(self, query: str, limit: int, *, timeout_seconds: float) -> SourcePage:
        self.calls.append(timeout_seconds)
        raise self.failure

    def get(self, identifier: UUID, *, timeout_seconds: float) -> JobRecord:
        raise self.failure


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (TimeoutError("SYNTHETIC private response"), SourceTimeout),
        (RuntimeError("SYNTHETIC secret"), SourceFailure),
        (SourceRateLimited(23), SourceRateLimited),
    ],
)
def test_source_failure_timeout_and_rate_limit_contract(
    discovery_setup: tuple[SQLiteDatabase, SyntheticBundle, AccessGrant, DiscoveryTools],
    failure: Exception,
    expected: type[Exception],
) -> None:
    database, bundle, grant, _ = discovery_setup
    source = FailingSource(failure)
    service = DiscoveryTools(
        grant,
        {"synthetic": source},
        MockCompanyAdapter(()),
        database.transaction,
        bundle.rules,
        timeout_seconds=2.5,
    )
    with pytest.raises(expected) as error:
        service.search_jobs({})
    assert source.calls == [2.5]
    assert "SYNTHETIC" not in str(error.value)
    if isinstance(error.value, SourceRateLimited):
        assert error.value.retry_after_seconds == 23


def test_malformed_source_page_fails_instead_of_partial_success(
    discovery_setup: tuple[SQLiteDatabase, SyntheticBundle, AccessGrant, DiscoveryTools],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, bundle, grant, _ = discovery_setup
    source = LocalJobSource.synthetic(bundle)
    monkeypatch.setattr(source, "search", lambda *args, **kwargs: {"records": [{"job": {}}]})
    service = DiscoveryTools(
        grant, {"synthetic": source}, MockCompanyAdapter(()), database.transaction, bundle.rules
    )
    with pytest.raises(SourceDataError):
        service.search_jobs({})


def record_variant(
    record: JobRecord,
    *,
    identifier: UUID | None = None,
    external: str | None = None,
    url: str | None = None,
) -> JobRecord:
    job = record.job.model_dump(mode="json")
    job["id"] = str(identifier or uuid4())
    job["source"] = {
        **record.job.source.model_dump(mode="json"),
        "external_id": external,
        "url": url,
    }
    return JobRecord(job=JobPosting.model_validate(job), definitions=record.definitions)


def test_transitive_dedup_keeps_all_source_provenance() -> None:
    bundle = synthetic_bundle()
    original = JobRecord(job=bundle.jobs[0], definitions=bundle.definitions[bundle.jobs[0].id])
    a = record_variant(original, external="one", url="https://example.test/jobs/1?utm_source=a")
    b = record_variant(original, external="one", url="https://example.test/jobs/2")
    c = record_variant(original, external="two", url="https://EXAMPLE.test/jobs/2#details")
    batch = SearchBatch(
        hits=tuple(SearchHit(source_id="synthetic", record=record) for record in (a, b, c)),
        searched_sources=("synthetic",),
        limit=10,
    )
    result = deduplicate(batch)
    assert len(result.groups) == 1
    assert result.groups[0].selected.record.job.id == a.job.id
    assert len(result.groups[0].references) == 3
    assert result.duplicates_removed == 2
    assert result.groups[0].references[0].canonical_url == "https://example.test/jobs/1"


def test_duplicate_identifier_with_different_payload_is_a_conflict() -> None:
    bundle = synthetic_bundle()
    original = JobRecord(job=bundle.jobs[0])
    changed = JobRecord(
        job=JobPosting.model_validate({**original.job.model_dump(), "title": "Changed"})
    )
    with pytest.raises(DiscoveryConflict):
        deduplicate(
            SearchBatch(
                hits=(
                    SearchHit(source_id="synthetic", record=original),
                    SearchHit(source_id="local", record=changed),
                ),
                searched_sources=("synthetic", "local"),
                limit=10,
            )
        )


def test_no_false_duplicates_when_source_identifiers_are_absent() -> None:
    bundle = synthetic_bundle()
    record = JobRecord(job=bundle.jobs[0])
    a = record_variant(record)
    b = record_variant(record)
    result = deduplicate(
        SearchBatch(
            hits=(
                SearchHit(source_id="synthetic", record=a),
                SearchHit(source_id="synthetic", record=b),
            ),
            searched_sources=("synthetic",),
            limit=10,
        )
    )
    assert len(result.groups) == 2
    assert result.duplicates_removed == 0


def test_local_import_roundtrip_and_read_only_after_registration(tmp_path: Path) -> None:
    bundle = synthetic_bundle()
    path = tmp_path / "approved.json"
    data = {
        "records": [
            JobRecord(job=job, definitions=bundle.definitions[job.id]).model_dump(mode="json")
            for job in bundle.jobs
        ]
    }
    path.write_text(json.dumps(data))
    source = LocalJobSource.from_file(path)
    path.write_text("not valid anymore")
    assert source.search("Python", 1, timeout_seconds=1).more_available
    assert source.get(bundle.jobs[0].id, timeout_seconds=1).job == bundle.jobs[0]


@pytest.mark.parametrize(
    "content", ["not JSON", "[]", '{"records":[{"job":{}}]}', '{"records":[],"shell":"exec"}']
)
def test_malformed_local_file_is_rejected_without_echo(tmp_path: Path, content: str) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(content)
    with pytest.raises(SourceDataError):
        LocalJobSource.from_file(path)


def test_import_requires_explicit_stable_identity_and_retrieval_time(tmp_path: Path) -> None:
    bundle = synthetic_bundle()
    for field in ("id", "created_at", "retrieved_at"):
        data = bundle.jobs[0].model_dump(mode="json")
        if field == "retrieved_at":
            source = data["source"]
            assert isinstance(source, dict)
            source.pop(field)
        else:
            data.pop(field)
        path = tmp_path / f"missing-{field}.json"
        path.write_text(json.dumps({"records": [{"job": data}]}))
        with pytest.raises(SourceDataError):
            LocalJobSource.from_file(path)


def test_import_and_tool_record_payload_limits(
    tmp_path: Path,
    discovery_setup: tuple[SQLiteDatabase, SyntheticBundle, AccessGrant, DiscoveryTools],
) -> None:
    path = tmp_path / "too-large.json"
    path.write_bytes(b"x" * (MAX_IMPORT_BYTES + 1))
    with pytest.raises(PayloadLimitExceeded):
        LocalJobSource.from_file(path)
    database, bundle, grant, _ = discovery_setup
    job = bundle.jobs[0]
    requirements = [
        {
            **job.requirements[0].model_dump(mode="json"),
            "id": str(uuid4()),
            "description": "x" * 2000,
        }
        for _ in range(12)
    ]
    giant = JobPosting.model_validate({**job.model_dump(), "requirements": requirements})
    service = DiscoveryTools(
        grant,
        {"synthetic": LocalJobSource((JobRecord(job=giant),))},
        MockCompanyAdapter(()),
        database.transaction,
        bundle.rules,
    )
    with pytest.raises(PayloadLimitExceeded):
        service.search_jobs({})


def test_duplicate_json_object_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-keys.json"
    path.write_text('{"records": [], "records": []}')
    with pytest.raises(SourceDataError):
        LocalJobSource.from_file(path)


def test_source_cannot_ignore_requested_result_limit(
    discovery_setup: tuple[SQLiteDatabase, SyntheticBundle, AccessGrant, DiscoveryTools],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, bundle, grant, _ = discovery_setup
    source = LocalJobSource.synthetic(bundle)
    page = source.search("", 10, timeout_seconds=1)
    monkeypatch.setattr(source, "search", lambda *args, **kwargs: page)
    service = DiscoveryTools(
        grant, {"synthetic": source}, MockCompanyAdapter(()), database.transaction, bundle.rules
    )
    with pytest.raises(SourceDataError):
        service.search_jobs({"limit": 1})


def test_aggregate_candidate_payload_cap_is_enforced(
    discovery_setup: tuple[SQLiteDatabase, SyntheticBundle, AccessGrant, DiscoveryTools],
) -> None:
    from careerpilot.application.discovery import MAX_RECORD_BYTES

    database, bundle, grant, _ = discovery_setup
    base = bundle.jobs[0]
    records: list[JobRecord] = []
    for _index in range(10):
        requirements = [
            {
                **base.requirements[0].model_dump(mode="json"),
                "id": str(uuid4()),
                "description": "x" * 2000,
            }
            for _ in range(6)
        ]
        job = JobPosting.model_validate(
            {**base.model_dump(), "id": str(uuid4()), "requirements": requirements}
        )
        record = JobRecord(job=job)
        assert len(record.model_dump_json().encode()) < MAX_RECORD_BYTES
        records.append(record)
    service = DiscoveryTools(
        grant,
        {"synthetic": LocalJobSource(tuple(records))},
        MockCompanyAdapter(()),
        database.transaction,
        bundle.rules,
    )
    with pytest.raises(PayloadLimitExceeded):
        service.search_jobs({"limit": 10})


def test_explicit_false_profile_fact_stays_false_for_authorized_owner(
    discovery_setup: tuple[SQLiteDatabase, SyntheticBundle, AccessGrant, DiscoveryTools],
) -> None:
    database, bundle, grant, _ = discovery_setup
    service = make_service(
        database, bundle, replace(grant, candidate_ids=frozenset({bundle.profiles[1].id}))
    )
    facts = service.candidate_facts(
        {"candidate_id": bundle.profiles[1].id, "fields": ["work_authorized"]}
    )
    assert facts.work_authorized is not None
    assert facts.work_authorized.value is False and facts.work_authorized.state.value == "known"


def test_local_source_rejects_ambiguous_same_id_before_lookup() -> None:
    bundle = synthetic_bundle()
    original = JobRecord(job=bundle.jobs[0])
    changed = JobRecord(
        job=JobPosting.model_validate({**original.job.model_dump(), "title": "Conflicting"})
    )
    with pytest.raises(SourceDataError):
        LocalJobSource((original, changed))
