"""Local discovery composition root. Import paths are operator-only CLI configuration."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

from careerpilot.application.discovery import DiscoveryTools
from careerpilot.application.discovery_contracts import (
    AccessGrant,
    CompanyInfo,
    DiscoveryError,
    DiscoveryFailure,
    JobSourceAdapter,
    Permission,
)
from careerpilot.config import configured_execution
from careerpilot.integrations.local_jobs import LocalJobSource
from careerpilot.integrations.mock_companies import MockCompanyAdapter
from careerpilot.integrations.synthetic import (
    FIXTURE_TIME,
    fixture_id,
    seed_synthetic,
    synthetic_bundle,
)
from careerpilot.persistence.sqlite import SQLiteDatabase
from careerpilot.workflows.discovery import DiscoveryWorkflow


def build_demo_service(database: SQLiteDatabase, import_file: Path | None = None) -> DiscoveryTools:
    bundle = synthetic_bundle()
    with database.transaction() as repository:
        seed_synthetic(repository, bundle)
    sources: dict[str, JobSourceAdapter] = {"synthetic": LocalJobSource.synthetic(bundle)}
    if import_file is not None:
        sources["local"] = LocalJobSource.from_file(import_file)
    grant = AccessGrant(
        actor_id=fixture_id("discovery-actor"),
        permissions=frozenset(Permission),
        source_ids=frozenset(sources),
        candidate_ids=frozenset({bundle.profiles[0].id}),
        company_ids=frozenset({"example-organization"}),
    )
    companies = MockCompanyAdapter(
        (
            CompanyInfo.model_validate(
                {
                    "company_id": "example-organization",
                    "name": "Example Organization",
                    "description": "Synthetic company information for local testing only.",
                    "source_url": "https://example.test/about",
                    "retrieved_at": FIXTURE_TIME,
                }
            ),
        )
    )
    return DiscoveryTools(grant, sources, companies, database.transaction, bundle.rules)


@configured_execution
def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded CareerPilot local job discovery")
    parser.add_argument("--query", default="Python")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--import-file", type=Path, help="Operator-approved JSON file; not exposed as an agent tool"
    )
    args = parser.parse_args()
    try:
        with TemporaryDirectory(prefix="careerpilot-discovery-") as directory:
            database = SQLiteDatabase(Path(directory) / "discovery.sqlite3")
            database.initialize()
            service = build_demo_service(database, args.import_file)
            raw = {
                "id": fixture_id("discovery-request"),
                "created_at": FIXTURE_TIME,
                "candidate_id": synthetic_bundle().profiles[0].id,
                "query": args.query,
                "limit": args.limit,
                "source_ids": ("synthetic", "local")
                if args.import_file is not None
                else ("synthetic",),
            }
            result = DiscoveryWorkflow(service).run(raw)
            print(result.model_dump_json(indent=2))
            if isinstance(result, DiscoveryFailure):
                raise SystemExit(2)
    except DiscoveryError as error:
        print(DiscoveryFailure(code=error.code, message=error.message).model_dump_json())
        raise SystemExit(2) from None
