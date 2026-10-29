"""Explicitly registered local sources. Paths are startup configuration, never tool input."""

import json
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError

from careerpilot.application.discovery_contracts import (
    JobRecord,
    PayloadLimitExceeded,
    ResourceNotFound,
    SourceDataError,
    SourcePage,
)
from careerpilot.domain.common import Entity, Model
from careerpilot.domain.jobs import JobSource
from careerpilot.integrations.synthetic import SyntheticBundle

MAX_IMPORT_BYTES = 256 * 1024


class ImportFile(Model):
    records: tuple[JobRecord, ...] = Field(max_length=100)


def _check_explicit_identity(model: BaseModel) -> None:
    if isinstance(model, Entity) and not {"id", "created_at"}.issubset(model.model_fields_set):
        raise SourceDataError()
    if isinstance(model, JobSource) and "retrieved_at" not in model.model_fields_set:
        raise SourceDataError()
    for name in type(model).model_fields:
        value = getattr(model, name)
        if isinstance(value, BaseModel):
            _check_explicit_identity(value)
        elif isinstance(value, tuple):
            for item in value:
                if isinstance(item, BaseModel):
                    _check_explicit_identity(item)


class LocalJobSource:
    def __init__(self, records: tuple[JobRecord, ...]) -> None:
        if len(records) > 100:
            raise PayloadLimitExceeded()
        self._records = tuple(
            JobRecord.model_validate_json(item.model_dump_json()) for item in records
        )
        identities: dict[UUID, JobRecord] = {}
        for record in self._records:
            if record.job.id in identities and identities[record.job.id] != record:
                raise SourceDataError()
            identities[record.job.id] = record

    @classmethod
    def from_file(cls, path: Path) -> "LocalJobSource":
        # Read at most the cap + 1 once. No traversal/URL/file tool is exposed.
        try:
            with path.open("rb") as source:
                content = source.read(MAX_IMPORT_BYTES + 1)
            if len(content) > MAX_IMPORT_BYTES:
                raise PayloadLimitExceeded()

            def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
                result: dict[str, object] = {}
                for key, value in pairs:
                    if key in result:
                        raise SourceDataError()
                    result[key] = value
                return result

            parsed = ImportFile.model_validate(
                json.loads(content.decode("utf-8"), object_pairs_hook=unique_object)
            )
            _check_explicit_identity(parsed)
            return cls(parsed.records)
        except (ValidationError, ValueError, UnicodeError, json.JSONDecodeError):
            raise SourceDataError() from None
        except OSError:
            raise SourceDataError() from None

    @classmethod
    def synthetic(cls, bundle: SyntheticBundle) -> "LocalJobSource":
        return cls(
            tuple(JobRecord(job=job, definitions=bundle.definitions[job.id]) for job in bundle.jobs)
        )

    def search(self, query: str, limit: int, *, timeout_seconds: float) -> SourcePage:
        if timeout_seconds <= 0:
            raise TimeoutError()
        words = query.casefold().split()
        matches = [
            record
            for record in self._records
            if all(
                word
                in (
                    f"{record.job.title} {record.job.employer} "
                    + " ".join(item.description for item in record.job.requirements)
                ).casefold()
                for word in words
            )
        ]
        return SourcePage(records=tuple(matches[:limit]), more_available=len(matches) > limit)

    def get(self, identifier: UUID, *, timeout_seconds: float) -> JobRecord:
        if timeout_seconds <= 0:
            raise TimeoutError()
        for record in self._records:
            if record.job.id == identifier:
                return record
        raise ResourceNotFound()
