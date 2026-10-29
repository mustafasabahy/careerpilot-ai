"""Opt-in metadata-only LangSmith telemetry. Never export business inputs/outputs."""

import os
from datetime import timedelta
from typing import Any, Literal, Protocol

from langsmith import Client
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from urllib3.util import Retry

from careerpilot.application.security import validate_outbound
from careerpilot.domain.common import utc_now
from careerpilot.domain.reliability import RunRecord


class TraceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CAREERPILOT_TRACE_", extra="ignore", frozen=True)
    enabled: bool = False
    endpoint: Literal["https://api.smith.langchain.com", "https://eu.api.smith.langchain.com"] = (
        "https://api.smith.langchain.com"
    )
    project: str = Field(default="careerpilot-metadata", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    api_key: SecretStr | None = Field(
        default=None, validation_alias="LANGSMITH_API_KEY", repr=False, exclude=True
    )

    @model_validator(mode="after")
    def key_required(self) -> "TraceSettings":
        if self.enabled and (
            os.getenv("LANGSMITH_RUNS_ENDPOINTS") or os.getenv("LANGCHAIN_RUNS_ENDPOINTS")
        ):
            raise ValueError("Replica tracing endpoints are not permitted")
        if self.enabled and (self.api_key is None or not self.api_key.get_secret_value().strip()):
            raise ValueError("Enabled telemetry requires LANGSMITH_API_KEY")
        return self


class TraceClient(Protocol):
    def create_run(
        self, name: str, inputs: dict[str, Any], run_type: Literal["chain"], **kwargs: Any
    ) -> None: ...


class LangSmithObserver:
    def __init__(self, settings: TraceSettings, client: TraceClient | None = None) -> None:
        self.settings = TraceSettings.model_validate(
            {**settings.model_dump(), "LANGSMITH_API_KEY": settings.api_key}
        )
        self.client = client
        if settings.enabled:
            validate_outbound(settings.endpoint, purpose="telemetry", enabled=True)
            if client is None:
                assert settings.api_key is not None
                self.client = Client(
                    api_url=settings.endpoint,
                    api_key=settings.api_key.get_secret_value(),
                    timeout_ms=2000,
                    retry_config=Retry(total=0),
                    auto_batch_tracing=False,
                    hide_inputs=True,
                    hide_outputs=True,
                    omit_traced_runtime_info=True,
                    otel_enabled=False,
                )

    def observe(self, record: RunRecord) -> None:
        if not self.settings.enabled or self.client is None:
            return
        end = utc_now()
        self.client.create_run(
            name="careerpilot.execution",
            inputs={},
            run_type="chain",
            id=record.operation_id,
            project_name=self.settings.project,
            start_time=end - timedelta(seconds=record.elapsed_seconds),
            end_time=end,
            outputs={},
            error=record.failure.code if record.failure else None,
            extra={
                "metadata": {
                    "workflow_id": str(record.workflow_id),
                    "correlation_id": str(record.correlation_id),
                    "status": record.status,
                    "latency_seconds": record.elapsed_seconds,
                    "node_counts": record.nodes,
                    "tool_calls": record.tool_calls,
                    "model_calls": record.model_calls,
                    "retry_count": record.retry_count,
                    "input_tokens": record.input_tokens,
                    "output_tokens": record.output_tokens,
                    "cost_usd": record.cost_usd,
                }
            },
        )
