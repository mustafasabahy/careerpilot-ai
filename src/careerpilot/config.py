"""Validated environment configuration, loaded explicitly at startup."""

from collections.abc import Callable
from functools import wraps
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from careerpilot.domain.reliability import ExecutionLimits


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CAREERPILOT_", env_file=".env", extra="ignore", frozen=True
    )

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class ReliabilitySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CAREERPILOT_RUN_", env_file=".env", extra="ignore", frozen=True
    )
    max_steps: int = 32
    max_tool_calls: int = 30
    max_retrieved_jobs: int = 30
    max_model_calls: int = 3
    overall_seconds: float = 120

    def limits(self) -> "ExecutionLimits":
        return ExecutionLimits.model_validate(self.model_dump())


def configured_execution[**P, T](operation: "Callable[P, T]") -> "Callable[P, T]":
    @wraps(operation)
    def execute(*args: P.args, **kwargs: P.kwargs) -> T:
        from careerpilot.application.execution import configured_limits
        from careerpilot.observability import configure_logging

        configure_logging(Settings().log_level)
        with configured_limits(ReliabilitySettings().limits()):
            return operation(*args, **kwargs)

    return execute
