"""Standalone deterministic demo; leaves the existing health command unchanged."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from careerpilot.application.analysis import AnalysisFailure, AnalysisService
from careerpilot.config import configured_execution
from careerpilot.integrations.synthetic import seed_synthetic, synthetic_bundle
from careerpilot.persistence.sqlite import SQLiteDatabase
from careerpilot.workflows.job_analysis import JobAnalysisWorkflow


@configured_execution
def main() -> None:
    parser = argparse.ArgumentParser(description="CareerPilot deterministic synthetic job analysis")
    parser.add_argument(
        "--scenario", choices=["complete", "missing-information", "hard-gap"], default="complete"
    )
    parser.add_argument(
        "--input", type=Path, help="JSON request using synthetic IDs; no personal data"
    )
    args = parser.parse_args()
    bundle = synthetic_bundle()
    raw: object = bundle.request(args.scenario).model_dump(mode="json")
    if args.input is not None:
        try:
            raw = json.loads(args.input.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(AnalysisFailure(code="invalid_request").model_dump_json())
            raise SystemExit(2) from None
    with TemporaryDirectory(prefix="careerpilot-synthetic-") as directory:
        database = SQLiteDatabase(Path(directory) / "demo.sqlite3")
        database.initialize()
        with database.transaction() as repository:
            seed_synthetic(repository, bundle)
        workflow = JobAnalysisWorkflow(
            AnalysisService(database.transaction, bundle.definitions, bundle.rules)
        )
        result = workflow.run(raw)
        print(result.model_dump_json(indent=2))
        if isinstance(result, AnalysisFailure):
            raise SystemExit(2)
