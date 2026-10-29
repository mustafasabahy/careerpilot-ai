"""Explicit extraction command. Only selecting openai mode can make a paid call."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from pydantic import ValidationError

from careerpilot.application.extraction import ExtractionError, ExtractionRequest, ExtractionService
from careerpilot.config import configured_execution
from careerpilot.domain.common import utc_now
from careerpilot.integrations.extraction_fixture import MOCK_JOB_TEXT
from careerpilot.integrations.synthetic import (
    FIXTURE_TIME,
    fixture_id,
    seed_synthetic,
    synthetic_bundle,
)
from careerpilot.persistence.sqlite import SQLiteDatabase
from careerpilot.providers.factory import build_job_model
from careerpilot.workflows.extracted_job import ExtractedJobWorkflow


@configured_execution
def main() -> None:
    parser = argparse.ArgumentParser(
        description="CareerPilot structured job extraction (mock by default)"
    )
    parser.add_argument(
        "--text", type=Path, help="UTF-8 job description; real mode sends this to the provider"
    )
    args = parser.parse_args()
    try:
        model = build_job_model()  # Fail on invalid real-mode configuration before loading data.
        text = MOCK_JOB_TEXT if args.text is None else args.text.read_text(encoding="utf-8")
        document = ExtractionRequest(
            id=fixture_id("extraction-document") if model.provider == "mock" else uuid4(),
            created_at=FIXTURE_TIME if model.provider == "mock" else utc_now(),
            text=text,
        )
        bundle = synthetic_bundle()
        with TemporaryDirectory(prefix="careerpilot-extraction-") as directory:
            database = SQLiteDatabase(Path(directory) / "extraction.sqlite3")
            database.initialize()
            with database.transaction() as repository:
                seed_synthetic(repository, bundle)
            result = ExtractedJobWorkflow(
                ExtractionService(model), database.transaction, bundle.rules
            ).run(
                document,
                candidate_id=bundle.profiles[0].id,
                analysis_id=fixture_id("extracted-analysis"),
            )
            print(result.model_dump_json(indent=2))
    except ValidationError:
        parser.exit(2, "Invalid extraction request: job text must contain 1–20000 characters\n")
    except OSError:
        parser.exit(2, "Could not read the job text file\n")
    except ExtractionError as error:
        # Error classes have fixed safe messages; no provider bodies/text/credentials are printed.
        parser.exit(2, f"{type(error).__name__}: {error}\n")
