"""Confirmed durable memory, corrections, isolation and optimistic concurrency."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from careerpilot.application.memory import (
    CareerMemory,
    MemoryConflict,
    MemoryDenied,
    MemoryPrincipal,
)
from careerpilot.domain.common import Evidence, EvidenceOrigin
from careerpilot.domain.memory import (
    CareerGoal,
    ConfirmedCareerFact,
    ExplicitCorrection,
    JobPreferences,
    LocationPreferences,
    MemoryWrite,
)
from careerpilot.persistence.memory_sqlite import SQLiteMemoryDatabase


@pytest.fixture
def memory(tmp_path: Path) -> CareerMemory:
    database = SQLiteMemoryDatabase(tmp_path / "memory.sqlite3")
    database.initialize()
    return CareerMemory(MemoryPrincipal(uuid4()), database)


def confirmed_fact(statement: str = "I maintain synthetic Python projects") -> ConfirmedCareerFact:
    return ConfirmedCareerFact(
        statement=statement,
        evidence=(Evidence(origin=EvidenceOrigin.USER_STATEMENT, reference="synthetic:confirmed"),),
    )


@pytest.mark.parametrize(
    "value",
    [
        JobPreferences(roles=("Python developer",), skills=("Python",)),
        LocationPreferences(locations=("Example City",), arrangements=("remote",)),
        CareerGoal(description="Practice technical interviews"),
        confirmed_fact(),
    ],
)
def test_valid_memory_values_persist_and_reopen(memory: CareerMemory, value: object) -> None:
    item = memory.confirm(
        {"id": uuid4(), "value": value, "expected_version": 0}, explicitly_confirmed=True
    )
    assert isinstance(memory.database, SQLiteMemoryDatabase)
    database = SQLiteMemoryDatabase(memory.database.path)
    database.initialize()
    restored = CareerMemory(memory.principal, database)
    assert restored.list() == (item,)
    assert item.confirmed_at.utcoffset() is not None


def test_confirmation_required_and_model_cannot_assert_ownership(memory: CareerMemory) -> None:
    request = MemoryWrite(id=uuid4(), value=confirmed_fact(), expected_version=0)
    with pytest.raises(MemoryDenied):
        memory.confirm(request, explicitly_confirmed=False)
    with pytest.raises(ValidationError):
        memory.confirm(
            {**request.model_dump(), "owner_id": uuid4(), "assistant_verified": True},
            explicitly_confirmed=True,
        )
    assert memory.list() == ()


@pytest.mark.parametrize("origin", [EvidenceOrigin.AI_INFERENCE, EvidenceOrigin.SOURCE_DOCUMENT])
def test_inferred_and_unconfirmed_document_claims_rejected(origin: EvidenceOrigin) -> None:
    with pytest.raises(ValidationError):
        ConfirmedCareerFact(
            statement="An assistant asserted this",
            evidence=(Evidence(origin=origin, reference="synthetic:unverified"),),
        )


def test_update_conflict_and_isolation(memory: CareerMemory) -> None:
    identifier = uuid4()
    first = memory.confirm(
        MemoryWrite(id=identifier, value=JobPreferences(roles=("Developer",)), expected_version=0),
        explicitly_confirmed=True,
    )
    other = CareerMemory(MemoryPrincipal(uuid4()), memory.database)
    assert other.list() == ()
    # Same resource identifier is scoped by owner, not globally shared.
    second = other.confirm(
        MemoryWrite(id=identifier, value=CareerGoal(description="Other goal"), expected_version=0),
        explicitly_confirmed=True,
    )
    with pytest.raises(MemoryConflict):
        memory.confirm(
            MemoryWrite(id=identifier, value=JobPreferences(), expected_version=0),
            explicitly_confirmed=True,
        )
    updated = memory.confirm(
        MemoryWrite(id=identifier, value=JobPreferences(roles=("Engineer",)), expected_version=1),
        explicitly_confirmed=True,
    )
    assert updated.version == 2 and first.version == 1
    assert other.list() == (second,)
    memory.delete(identifier, expected_version=2)
    assert not memory.list() and other.list() == (second,)


def test_explicit_correction_retires_old_fact_and_is_atomic(memory: CareerMemory) -> None:
    original = memory.confirm(
        MemoryWrite(id=uuid4(), value=confirmed_fact("Incorrect date"), expected_version=0),
        explicitly_confirmed=True,
    )
    correction = ExplicitCorrection(
        replaces_id=original.id,
        replaces_version=1,
        replacement=confirmed_fact("Corrected date"),
        reason="User corrected a transcription error",
    )
    other = CareerMemory(MemoryPrincipal(uuid4()), memory.database)
    with pytest.raises(MemoryConflict):
        other.confirm(
            MemoryWrite(id=uuid4(), value=correction, expected_version=0), explicitly_confirmed=True
        )
    assert memory.list() == (original,)
    corrected = memory.confirm(
        MemoryWrite(id=uuid4(), value=correction, expected_version=0), explicitly_confirmed=True
    )
    assert memory.list() == (corrected,)
    assert "Incorrect date" not in corrected.model_dump_json()
    memory.delete(corrected.id, expected_version=1)
    assert not memory.list()  # Never resurrects the retired statement.


def test_concurrent_updates_one_wins_without_lost_write(memory: CareerMemory) -> None:
    identifier = uuid4()
    memory.confirm(
        MemoryWrite(id=identifier, value=JobPreferences(), expected_version=0),
        explicitly_confirmed=True,
    )

    def update(role: str) -> str:
        try:
            memory.confirm(
                MemoryWrite(id=identifier, value=JobPreferences(roles=(role,)), expected_version=1),
                explicitly_confirmed=True,
            )
            return "updated"
        except MemoryConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, ("First", "Second"))) == ["conflict", "updated"]
    assert memory.list()[0].version == 2


def test_memory_limit_and_input_bounds(memory: CareerMemory) -> None:
    with pytest.raises(ValidationError):
        JobPreferences(roles=("x" * 501,))
    with pytest.raises(ValidationError):
        LocationPreferences.model_validate({"arrangements": ["anywhere"]})
    for _ in range(100):
        memory.confirm(
            MemoryWrite(id=uuid4(), value=CareerGoal(description="Synthetic"), expected_version=0),
            explicitly_confirmed=True,
        )
    with pytest.raises(MemoryConflict):
        memory.confirm(
            MemoryWrite(id=uuid4(), value=CareerGoal(description="Extra"), expected_version=0),
            explicitly_confirmed=True,
        )
    assert len(memory.list()) == 100


def test_fact_edits_require_explicit_correction(memory: CareerMemory) -> None:
    original = memory.confirm(
        MemoryWrite(id=uuid4(), value=confirmed_fact(), expected_version=0),
        explicitly_confirmed=True,
    )
    with pytest.raises(MemoryConflict):
        memory.confirm(
            MemoryWrite(id=original.id, value=confirmed_fact("New statement"), expected_version=1),
            explicitly_confirmed=True,
        )
    with pytest.raises(MemoryConflict):
        memory.confirm(
            MemoryWrite(
                id=uuid4(),
                expected_version=0,
                value=ExplicitCorrection(
                    replaces_id=original.id,
                    replaces_version=2,
                    replacement=confirmed_fact("Corrected"),
                    reason="Fix",
                ),
            ),
            explicitly_confirmed=True,
        )
    assert memory.list() == (original,)


def test_corrections_can_be_corrected_without_resurrecting_retired_facts(
    memory: CareerMemory,
) -> None:
    original = memory.confirm(
        MemoryWrite(id=uuid4(), value=confirmed_fact("First statement"), expected_version=0),
        explicitly_confirmed=True,
    )
    first_correction = memory.confirm(
        MemoryWrite(
            id=uuid4(),
            expected_version=0,
            value=ExplicitCorrection(
                replaces_id=original.id,
                replaces_version=1,
                replacement=confirmed_fact("Second statement"),
                reason="First fix",
            ),
        ),
        explicitly_confirmed=True,
    )
    final = memory.confirm(
        MemoryWrite(
            id=uuid4(),
            expected_version=0,
            value=ExplicitCorrection(
                replaces_id=first_correction.id,
                replaces_version=1,
                replacement=confirmed_fact("Final statement"),
                reason="Second fix",
            ),
        ),
        explicitly_confirmed=True,
    )
    assert memory.list() == (final,)
    assert "First statement" not in final.model_dump_json()
    assert "Second statement" not in final.model_dump_json()
    with pytest.raises(ValidationError):
        memory.delete(final.id, expected_version=True)
    with pytest.raises(MemoryConflict):
        CareerMemory(MemoryPrincipal(uuid4()), memory.database).delete(final.id, expected_version=1)
    assert memory.list() == (final,)
