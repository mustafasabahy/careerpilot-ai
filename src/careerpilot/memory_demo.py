"""Synthetic local memory/checkpoint demonstration; no model calls or real identity."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from careerpilot.application.memory import CareerMemory, MemoryPrincipal
from careerpilot.config import configured_execution
from careerpilot.domain.common import Evidence, EvidenceOrigin
from careerpilot.domain.memory import (
    CareerGoal,
    ConfirmedCareerFact,
    JobPreferences,
    LocationPreferences,
    MemoryWrite,
)
from careerpilot.integrations.synthetic import FIXTURE_TIME, fixture_id
from careerpilot.persistence.checkpoints import local_checkpointer
from careerpilot.persistence.memory_sqlite import SQLiteMemoryDatabase
from careerpilot.workflows.career_context import CareerContextWorkflow

DEMO_OWNER = fixture_id("memory-demo-owner")
DEMO_THREAD = fixture_id("memory-demo-thread")


def action(directory: Path, operation: str, thread_id: UUID) -> object:
    directory.mkdir(parents=True, exist_ok=True)
    memory_path = directory / "memory.sqlite3"
    database = SQLiteMemoryDatabase(memory_path)
    database.initialize()
    memory = CareerMemory(MemoryPrincipal(DEMO_OWNER), database)
    with local_checkpointer(
        directory / "checkpoints.sqlite3", separate_from=(memory_path,)
    ) as saver:
        workflow = CareerContextWorkflow(memory, saver)
        if operation == "pause":
            if not memory.list():
                values = (
                    JobPreferences(roles=("Python developer",), skills=("Python",)),
                    LocationPreferences(locations=("Example City",), arrangements=("remote",)),
                    ConfirmedCareerFact(
                        statement="I maintain synthetic Python projects",
                        evidence=(
                            Evidence(
                                id=fixture_id("memory-demo-evidence"),
                                created_at=FIXTURE_TIME,
                                origin=EvidenceOrigin.USER_STATEMENT,
                                reference="synthetic:user-confirmed",
                            ),
                        ),
                    ),
                    CareerGoal(description="Practice evidence-based interview answers"),
                )
                for value in values:
                    memory.confirm(
                        MemoryWrite(
                            id=fixture_id(f"memory-demo:{value.kind}"),
                            value=value,
                            expected_version=0,
                        ),
                        explicitly_confirmed=True,
                    )
            return workflow.start(thread_id).model_dump(mode="json")
        if operation == "resume":
            return workflow.resume(thread_id, accepted=True).model_dump(mode="json")
        if operation == "state":
            return workflow.current(thread_id).model_dump(mode="json")
        if operation == "history":
            return [item.model_dump(mode="json") for item in workflow.history(thread_id)]
        if operation == "context":
            return workflow.context(thread_id).model_dump(mode="json")
        if operation == "forget":
            workflow.forget_all()
            return {"status": "forgotten", "remaining_memory_count": len(memory.list())}
        raise ValueError("Unknown demo operation")


def demonstrate(directory: Path) -> dict[str, object]:
    # Each action closes its connection and recompiles the graph. The resume action
    # cannot depend on the original process's graph/checkpointer Python objects.
    paused = action(directory, "pause", DEMO_THREAD)
    reopened = action(directory, "state", DEMO_THREAD)
    resumed = action(directory, "resume", DEMO_THREAD)
    return {
        "paused": paused,
        "reopened": reopened,
        "resumed": resumed,
        "history": action(directory, "history", DEMO_THREAD),
        "context": action(directory, "context", DEMO_THREAD),
    }


@configured_execution
def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic memory/checkpoint demo only")
    parser.add_argument("--directory", type=Path)
    parser.add_argument(
        "--action",
        choices=("demo", "pause", "resume", "state", "history", "context", "forget"),
        default="demo",
    )
    parser.add_argument("--thread", type=UUID, default=DEMO_THREAD)
    arguments = parser.parse_args()
    if arguments.action != "demo" and arguments.directory is None:
        parser.error("Durable actions require --directory")
    if arguments.directory is not None:
        result = (
            demonstrate(arguments.directory)
            if arguments.action == "demo"
            else action(arguments.directory, arguments.action, arguments.thread)
        )
    else:
        with TemporaryDirectory(prefix="careerpilot-memory-") as directory:
            result = demonstrate(Path(directory))
    print(json.dumps(result, ensure_ascii=False, indent=2))
