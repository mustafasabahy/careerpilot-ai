"""Provider-neutral extraction boundary and evidence grounding."""

import hashlib
from collections.abc import Iterator
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from careerpilot.domain.common import Entity, Model, Text, UtcTimestamp
from careerpilot.domain.extraction import (
    ExtractedValue,
    ExtractionState,
    ExtractionWarning,
    JobExtraction,
    SourceSnippet,
)


class ExtractionError(RuntimeError):
    """Safe public error: callers should not serialize underlying provider errors."""


class ProviderConfigurationError(ExtractionError):
    pass


class InvalidModelOutput(ExtractionError):
    pass


class UngroundedExtraction(InvalidModelOutput):
    pass


class ProviderTimeout(ExtractionError):
    pass


class ProviderFailure(ExtractionError):
    pass


class ProviderTransientFailure(ProviderFailure):
    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderAuthenticationError(ProviderFailure):
    pass


class ProviderRateLimited(ProviderFailure):
    def __init__(
        self, message: str, *, retry_after_seconds: float | None = None, retryable: bool = True
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.retryable = retryable


class ModelRefusal(ExtractionError):
    pass


class MockInputUnsupported(ExtractionError):
    pass


class ExtractionRequest(Entity):
    model_config = ConfigDict(hide_input_in_errors=True)
    id: UUID = Field(...)
    created_at: UtcTimestamp = Field(...)
    text: str = Field(repr=False)

    @field_validator("text")
    @classmethod
    def bounded_text(cls, value: str) -> str:
        if not value.strip() or len(value) > 20_000:
            raise ValueError("Job text must contain 1–20000 characters")
        return value  # Preserve text exactly for source evidence checks.


class JobModel(Protocol):
    @property
    def provider(self) -> str: ...
    @property
    def model(self) -> str: ...
    def extract(self, text: str) -> object: ...


class ExtractionProvenance(Model):
    document_id: UUID
    source_sha256: str
    provider: Text
    model: Text
    prompt_version: Literal["job-extraction-v1"] = "job-extraction-v1"
    received_at: UtcTimestamp
    trust: Literal["unreviewed_inference"] = "unreviewed_inference"


class ExtractionEnvelope(Model):
    provenance: ExtractionProvenance
    extraction: JobExtraction


def _models(model: BaseModel) -> Iterator[BaseModel]:
    yield model
    for name in type(model).model_fields:
        value = getattr(model, name)
        if isinstance(value, BaseModel):
            yield from _models(value)
        elif isinstance(value, tuple):
            for item in value:
                if isinstance(item, BaseModel):
                    yield from _models(item)


class ExtractionService:
    def __init__(self, model: JobModel) -> None:
        self._model = model

    def extract(self, request: ExtractionRequest) -> ExtractionEnvelope:
        from careerpilot.application.execution import execution_scope

        with execution_scope():
            return self._extract(request)

    def _extract(self, request: ExtractionRequest) -> ExtractionEnvelope:
        request = ExtractionRequest.model_validate(request.model_dump(warnings=False))
        raw = self._model.extract(request.text)
        try:
            if isinstance(raw, BaseModel):
                raw = raw.model_dump(warnings=False)
            extraction = (
                JobExtraction.model_validate_json(raw)
                if isinstance(raw, (str, bytes))
                else JobExtraction.model_validate(raw)
            )
        except ValidationError:
            raise InvalidModelOutput("Model output does not match the extraction schema") from None
        states: set[ExtractionState] = set()
        for model in _models(extraction):
            if isinstance(model, SourceSnippet) and model.quote not in request.text:
                raise UngroundedExtraction("Extraction contains evidence absent from the source")
            if isinstance(model, ExtractedValue):
                states.add(model.state)
        warnings = set(extraction.warnings)
        if ExtractionState.CONFLICTING in states:
            warnings.add(ExtractionWarning.CONTRADICTORY_INFORMATION)
        if ExtractionState.UNKNOWN in states:
            warnings.add(ExtractionWarning.AMBIGUOUS_INFORMATION)
        if ExtractionState.MISSING in states:
            warnings.add(ExtractionWarning.MISSING_INFORMATION)
        extraction = JobExtraction.model_validate(
            {**extraction.model_dump(), "warnings": tuple(sorted(warnings))}
        )
        return ExtractionEnvelope(
            provenance=ExtractionProvenance(
                document_id=request.id,
                source_sha256=hashlib.sha256(request.text.encode()).hexdigest(),
                provider=self._model.provider,
                model=self._model.model,
                received_at=request.created_at,
            ),
            extraction=extraction,
        )
