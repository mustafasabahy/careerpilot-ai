"""Real LangGraph checkpoints, owner-scoped reads, resume, deletion and budgets."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from careerpilot.application.context import ContextBuilder, ContextMessage, trim_messages
from careerpilot.application.memory import (
    CareerMemory,
    MemoryConflict,
    MemoryDenied,
    MemoryPrincipal,
)
from careerpilot.domain.memory import CareerGoal, JobPreferences, MemoryWrite
from careerpilot.domain.profile import CandidateProfile
from careerpilot.integrations.synthetic import synthetic_bundle
from careerpilot.persistence.checkpoints import isolated_checkpointer, local_checkpointer
from careerpilot.persistence.memory_sqlite import SQLiteMemoryDatabase
from careerpilot.workflows.career_context import CareerContextWorkflow, ContextSelection


@pytest.fixture
def session(tmp_path: Path) -> tuple[CareerMemory, InMemorySaver, CareerContextWorkflow]:
    database = SQLiteMemoryDatabase(tmp_path / "memory.sqlite3")
    database.initialize()
    memory = CareerMemory(MemoryPrincipal(uuid4()), database)
    memory.confirm(
        MemoryWrite(
            id=uuid4(), value=JobPreferences(roles=("Synthetic developer",)), expected_version=0
        ),
        explicitly_confirmed=True,
    )
    saver = isolated_checkpointer()
    return memory, saver, CareerContextWorkflow(memory, saver)


def test_pause_history_resume_and_reference_only_state(
    session: tuple[CareerMemory, InMemorySaver, CareerContextWorkflow],
) -> None:
    memory, saver, workflow = session
    identifier = uuid4()
    paused = workflow.start(identifier)
    assert paused.status == "paused" and paused.next_nodes == ("review_context",)
    assert workflow.current(identifier) == paused
    assert workflow.context(identifier).memories == memory.list()
    completed = workflow.resume(identifier, accepted=True)
    assert completed.status == "completed" and completed.next_nodes == ()
    assert len(workflow.history(identifier)) >= 3
    assert workflow.history(identifier, limit=1) == (completed,)
    raw = list(saver.list(None))
    assert raw
    assert "Synthetic developer" not in repr(raw)
    assert all("statement" not in row.checkpoint["channel_values"] for row in raw)
    with pytest.raises(MemoryConflict):
        workflow.resume(identifier, accepted=True)
    with pytest.raises(MemoryConflict):
        workflow.start(identifier)


def test_user_memories_shared_between_own_threads_never_between_users(
    session: tuple[CareerMemory, InMemorySaver, CareerContextWorkflow],
) -> None:
    memory, saver, workflow = session
    first, second = uuid4(), uuid4()
    workflow.start(first)
    workflow.start(second)
    assert workflow.context(first).memories == workflow.context(second).memories
    workflow.resume(first, accepted=True)
    assert workflow.current(second).status == "paused"
    other_memory = CareerMemory(MemoryPrincipal(uuid4()), memory.database)
    other = CareerContextWorkflow(other_memory, saver)
    other_id = uuid4()
    other.start(other_id)
    assert other.context(other_id).memories == ()
    for operation in (
        lambda: other.current(first),
        lambda: other.history(first),
        lambda: other.context(first),
        lambda: other.resume(first, accepted=True),
        lambda: other.delete(first),
    ):
        with pytest.raises(MemoryDenied):
            operation()
    with pytest.raises(MemoryConflict):
        other.start(first)
    other.forget_all()
    assert workflow.context(first).memories == memory.list()


def test_memory_deletion_invalidates_paused_context_and_removes_data(
    session: tuple[CareerMemory, InMemorySaver, CareerContextWorkflow],
) -> None:
    memory, saver, workflow = session
    identifier = uuid4()
    workflow.start(identifier)
    item = memory.list()[0]
    memory.delete(item.id, expected_version=item.version)
    with pytest.raises(MemoryConflict):
        workflow.resume(identifier, accepted=True)
    with pytest.raises(MemoryConflict):
        workflow.context(identifier)
    assert "Synthetic developer" not in repr(list(saver.list(None)))
    workflow.delete(identifier)
    assert list(saver.list({"configurable": {"thread_id": str(identifier)}})) == []
    with pytest.raises(MemoryDenied):
        workflow.current(identifier)
    workflow.delete(identifier)  # Idempotent retry of physical cleanup.
    with pytest.raises(MemoryConflict):
        workflow.start(identifier)  # Tombstoned IDs cannot be recycled.


def test_cancel_and_full_user_purge(
    session: tuple[CareerMemory, InMemorySaver, CareerContextWorkflow],
) -> None:
    memory, saver, workflow = session
    identifiers = (uuid4(), uuid4())
    for identifier in identifiers:
        workflow.start(identifier)
    assert workflow.resume(identifiers[0], accepted=False).status == "cancelled"
    with pytest.raises(MemoryDenied):
        workflow.context(identifiers[0])
    workflow.forget_all()
    assert memory.list() == () and list(saver.list(None)) == []
    workflow.forget_all()
    for identifier in identifiers:
        with pytest.raises(MemoryDenied):
            workflow.current(identifier)


def test_reopen_sqlite_checkpointer_and_memory_then_resume(tmp_path: Path) -> None:
    memory_path, checkpoint_path = tmp_path / "memory.sqlite3", tmp_path / "checkpoint.sqlite3"
    owner, thread_id = uuid4(), uuid4()
    database = SQLiteMemoryDatabase(memory_path)
    database.initialize()
    memory = CareerMemory(MemoryPrincipal(owner), database)
    with local_checkpointer(checkpoint_path, separate_from=(memory_path,)) as saver:
        before = CareerContextWorkflow(memory, saver).start(thread_id)
    # New DB objects, connection, service and compiled graph after closing all resources.
    reopened = SQLiteMemoryDatabase(memory_path)
    reopened.initialize()
    with local_checkpointer(checkpoint_path, separate_from=(memory_path,)) as saver:
        workflow = CareerContextWorkflow(CareerMemory(MemoryPrincipal(owner), reopened), saver)
        assert workflow.current(thread_id) == before
        assert workflow.resume(thread_id, accepted=True).status == "completed"
        assert len(workflow.history(thread_id)) >= 3
        workflow.delete(thread_id)
        assert not list(saver.list(None))


def test_process_exit_and_new_process_resume(tmp_path: Path) -> None:
    # Crash after the synchronous pause checkpoint. No application/model network.
    script = """
import os, sys
from pathlib import Path
from uuid import UUID
from careerpilot.application.memory import CareerMemory, MemoryPrincipal
from careerpilot.persistence.memory_sqlite import SQLiteMemoryDatabase
from careerpilot.persistence.checkpoints import local_checkpointer
from careerpilot.workflows.career_context import CareerContextWorkflow
root=Path(sys.argv[1]); owner=UUID(sys.argv[2]); thread=UUID(sys.argv[3])
db=SQLiteMemoryDatabase(root/'memory.sqlite3'); db.initialize()
with local_checkpointer(root/'checkpoint.sqlite3', separate_from=(db.path,)) as saver:
    flow=CareerContextWorkflow(CareerMemory(MemoryPrincipal(owner),db),saver)
    if sys.argv[4]=='pause':
        assert flow.start(thread).status=='paused'
        os._exit(0)
    print(flow.resume(thread,accepted=True).model_dump_json())
"""
    arguments = [sys.executable, "-c", script, str(tmp_path), str(uuid4()), str(uuid4())]
    environment = {**os.environ, "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"}
    first = subprocess.run(
        [*arguments, "pause"], capture_output=True, text=True, timeout=15, env=environment
    )
    assert first.returncode == 0, first.stderr
    second = subprocess.run(
        [*arguments, "resume"], capture_output=True, text=True, timeout=15, env=environment
    )
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["status"] == "completed"


def test_context_trimming_unicode_and_untrusted_tool_output() -> None:
    messages = tuple(
        ContextMessage(role="tool", text=f"{index}: " + "م" * 6000) for index in range(12)
    )
    trimmed, summary = trim_messages(messages)
    assert len(trimmed) <= 8 and sum(len(item.text.encode()) for item in trimmed) <= 8192
    assert all(len(item.text.encode()) <= 2048 for item in trimmed)
    assert trimmed[-1].text.startswith("11:")
    assert "omitted" in summary and "truncated" in summary
    with pytest.raises(ValueError):
        trim_messages((ContextMessage(role="assistant", text="not a verified fact"),) * 101)
    with pytest.raises(ValidationError):
        ContextMessage(role="tool", text="x" * 16001)


def test_memory_selection_and_context_summary_do_not_write_memory(
    session: tuple[CareerMemory, InMemorySaver, CareerContextWorkflow],
) -> None:
    memory, _, workflow = session
    memory.confirm(
        MemoryWrite(id=uuid4(), value=CareerGoal(description="Practice"), expected_version=0),
        explicitly_confirmed=True,
    )
    identifier = uuid4()
    workflow.start(identifier, ContextSelection(kinds=("career_goal",), limit=1))
    before = memory.list()
    context = workflow.context(
        identifier,
        (ContextMessage(role="assistant", text="I claim the candidate won a Nobel prize"),),
    )
    assert len(context.memories) == 1 and context.memories[0].value.kind == "career_goal"
    assert context.authoritative_summary is False
    assert context.trust == "data_only_no_permissions"
    assert memory.list() == before


def test_profile_facts_loaded_only_on_demand_and_authorized(tmp_path: Path) -> None:
    profile = synthetic_bundle().profiles[0]
    principal = MemoryPrincipal(uuid4(), frozenset({profile.id}))
    database = SQLiteMemoryDatabase(tmp_path / "memory.sqlite3")
    database.initialize()
    calls: list[UUID] = []

    def loader(identifier: UUID) -> CandidateProfile:
        calls.append(identifier)
        return profile

    workflow = CareerContextWorkflow(
        CareerMemory(principal, database),
        isolated_checkpointer(),
        ContextBuilder(principal, loader),
    )
    identifier = uuid4()
    workflow.start(identifier, candidate_id=profile.id)
    assert not calls
    context = workflow.context(identifier)
    assert calls == [profile.id] and context.profile_facts
    assert all(fact.verification == "user_confirmed" for fact in context.profile_facts)
    assert all("SQL" not in fact.text for fact in context.profile_facts)
    with pytest.raises(MemoryDenied):
        workflow.start(uuid4(), candidate_id=uuid4())
    other_principal = MemoryPrincipal(principal.owner_id)
    revoked = CareerContextWorkflow(CareerMemory(other_principal, database), workflow._checkpointer)
    with pytest.raises(MemoryDenied):
        revoked.context(identifier)


def test_concurrent_resume_serializes_across_instances(tmp_path: Path) -> None:
    database = SQLiteMemoryDatabase(tmp_path / "memory.sqlite3")
    database.initialize()
    principal = MemoryPrincipal(uuid4())
    checkpoint_path = tmp_path / "checkpoint.sqlite3"
    identifier = uuid4()
    with local_checkpointer(checkpoint_path, separate_from=(database.path,)) as saver:
        CareerContextWorkflow(CareerMemory(principal, database), saver).start(identifier)

    def resume(_: int) -> str:
        # Different connections/savers, same on-disk lock/registry.
        with local_checkpointer(checkpoint_path, separate_from=(database.path,)) as saver:
            workflow = CareerContextWorkflow(CareerMemory(principal, database), saver)
            try:
                return workflow.resume(identifier, accepted=True).status
            except MemoryConflict:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(resume, (1, 2))) == ["completed", "conflict"]


def test_checkpoint_path_separation_and_history_bounds(
    session: tuple[CareerMemory, InMemorySaver, CareerContextWorkflow], tmp_path: Path
) -> None:
    _, _, workflow = session
    identifier = uuid4()
    workflow.start(identifier)
    for limit in (0, 101, True):
        with pytest.raises(ValueError):
            workflow.history(identifier, limit=limit)
    with pytest.raises(ValueError):
        with local_checkpointer(
            tmp_path / "same.sqlite3", separate_from=(tmp_path / "same.sqlite3",)
        ):
            pass


def test_physical_purge_failure_still_denies_access_and_allows_retry(
    session: tuple[CareerMemory, InMemorySaver, CareerContextWorkflow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, saver, workflow = session
    identifier = uuid4()
    workflow.start(identifier)
    original = saver.delete_thread

    def fail_purge(thread_id: str) -> None:
        raise OSError("Synthetic disk failure")

    monkeypatch.setattr(saver, "delete_thread", fail_purge)
    with pytest.raises(OSError):
        workflow.delete(identifier)
    with pytest.raises(MemoryDenied):
        workflow.current(identifier)
    monkeypatch.setattr(saver, "delete_thread", original)
    workflow.delete(identifier)
    assert not list(saver.list(None))


def test_profile_selection_is_bounded_and_relevant(tmp_path: Path) -> None:
    from careerpilot.application.context import ProfileContextSelection
    from careerpilot.domain.preparation import FactKind

    profile = synthetic_bundle().profiles[0]
    principal = MemoryPrincipal(uuid4(), frozenset({profile.id}))
    builder = ContextBuilder(principal, lambda identifier: profile)
    result = builder.build(
        (), profile.id, selection=ProfileContextSelection(kinds=(FactKind.SKILL,))
    )
    assert result.profile_facts
    assert all(fact.kind == FactKind.SKILL for fact in result.profile_facts)
    exact_id = result.profile_facts[0].id
    exact = builder.build(
        (),
        profile.id,
        selection=ProfileContextSelection(kinds=(FactKind.SKILL,), fact_ids=(exact_id,)),
    )
    assert tuple(fact.id for fact in exact.profile_facts) == (exact_id,)


def test_checkpoint_and_memory_databases_reject_business_files(tmp_path: Path) -> None:
    from careerpilot.persistence.sqlite import SQLiteDatabase

    path = tmp_path / "business.sqlite3"
    business = SQLiteDatabase(path)
    business.initialize()
    with pytest.raises(ValueError):
        SQLiteMemoryDatabase(path).initialize()
    with pytest.raises(ValueError):
        with local_checkpointer(path, separate_from=()):
            pass
    with business.transaction():
        pass  # The domain schema and adapter remain usable.


def test_demo_executes_real_pause_and_reopen(tmp_path: Path) -> None:
    from careerpilot.memory_demo import demonstrate

    result = demonstrate(tmp_path)
    assert result["paused"] == result["reopened"]
    assert isinstance(result["resumed"], dict) and result["resumed"]["status"] == "completed"
    assert isinstance(result["context"], dict) and len(result["context"]["memories"]) == 4


def test_large_memory_context_rejected_without_truncating_confirmed_facts(
    session: tuple[CareerMemory, InMemorySaver, CareerContextWorkflow],
) -> None:
    memory, _, workflow = session
    memory.confirm(
        MemoryWrite(
            id=uuid4(),
            expected_version=0,
            value=JobPreferences(roles=("界" * 500,) * 10, skills=("界" * 500,) * 10),
        ),
        explicitly_confirmed=True,
    )
    identifier = uuid4()
    workflow.start(identifier)
    with pytest.raises(MemoryConflict, match="budget"):
        workflow.context(identifier)
    assert len(memory.list()) == 2


def test_ambient_tracing_is_disabled_and_restored(
    session: tuple[CareerMemory, InMemorySaver, CareerContextWorkflow],
) -> None:
    from langsmith import get_tracing_context, tracing_context

    _, _, workflow = session
    identifier = uuid4()
    with tracing_context(enabled=True):
        workflow.start(identifier)
        workflow.current(identifier)
        workflow.history(identifier)
        workflow.resume(identifier, accepted=True)
        workflow.context(identifier)
        workflow.delete(identifier)
        assert get_tracing_context()["enabled"] is True
