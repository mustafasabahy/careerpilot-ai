"""Local process health, without infrastructure or provider calls."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class HealthStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    service: Literal["careerpilot-ai"] = "careerpilot-ai"
    status: Literal["ok"] = "ok"


def check_health() -> HealthStatus:
    """Report process health only; this is not a dependency readiness probe."""
    return HealthStatus()
