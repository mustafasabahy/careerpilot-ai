"""Phase F bounded confirmed-memory retrieval tests."""

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from careerpilot.application.context_retrieval import ConfirmedMemoryRetriever
from careerpilot.application.memory import CareerMemory, MemoryPrincipal
from careerpilot.domain.common import Evidence, EvidenceOrigin
from careerpilot.domain.memory import ConfirmedCareerFact, JobPreferences
from careerpilot.persistence.memory_sqlite import SQLiteMemoryDatabase


def make_memory(tmp_path: Path, owner: UUID | None = None) -> CareerMemory:
    database = SQLiteMemoryDatabase(tmp_path / "memory.sqlite3")
    database.initialize()
    return CareerMemory(MemoryPrincipal(owner or uuid4()), database)


def fact(statement: str) -> ConfirmedCareerFact:
    return ConfirmedCareerFact(
        statement=statement,
        evidence=(Evidence(origin=EvidenceOrigin.USER_STATEMENT, reference="synthetic:user"),),
    )


def test_retrieval_is_owner_scoped_relevant_and_versioned(tmp_path: Path) -> None:
    owner = uuid4()
    memory = make_memory(tmp_path, owner)
    item = memory.confirm(
        {"id": uuid4(), "value": fact("Python and testing"), "expected_version": 0},
        explicitly_confirmed=True,
    )
    memory.confirm(
        {"id": uuid4(), "value": JobPreferences(roles=("Designer",)), "expected_version": 0},
        explicitly_confirmed=True,
    )
    result = ConfirmedMemoryRetriever(memory).retrieve("python", limit=1)
    assert len(result.items) == 1
    assert result.items[0].reference.id == item.id
    assert result.items[0].reference.version == item.version
    assert result.items[0].confirmed
    assert result.items[0].reference.provenance == "explicit_user_confirmation"

    other = CareerMemory(MemoryPrincipal(uuid4()), memory.database)
    assert ConfirmedMemoryRetriever(other).retrieve().items == ()


def test_limits_empty_results_and_context_size(tmp_path: Path) -> None:
    memory = make_memory(tmp_path)
    memory.confirm(
        {"id": uuid4(), "value": fact("Python developer"), "expected_version": 0},
        explicitly_confirmed=True,
    )
    retriever = ConfirmedMemoryRetriever(memory)
    assert retriever.retrieve("unmatched").items == ()
    bounded = retriever.retrieve(max_context_chars=1)
    assert bounded.items == () and bounded.truncated
    with pytest.raises(ValueError):
        retriever.retrieve(limit=21)
    with pytest.raises(ValueError):
        retriever.retrieve(max_context_chars=0)


def test_deleted_and_corrected_memories_are_not_returned(tmp_path: Path) -> None:
    memory = make_memory(tmp_path)
    original = memory.confirm(
        {"id": uuid4(), "value": fact("Old Python claim"), "expected_version": 0},
        explicitly_confirmed=True,
    )
    memory.delete(original.id, expected_version=original.version)
    assert ConfirmedMemoryRetriever(memory).retrieve("python").items == ()
