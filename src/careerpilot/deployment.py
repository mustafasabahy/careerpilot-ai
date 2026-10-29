"""Fail-fast single-host server configuration, separate from local demo defaults."""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, HttpUrl, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CAREERPILOT_SERVER_",
        env_file=".env",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
        populate_by_name=True,
    )
    environment: Literal["development", "test", "production"] = Field(
        default="development", validation_alias="CAREERPILOT_ENVIRONMENT"
    )
    directory: Path = Path("work/server")
    port: int = Field(default=8000, ge=1024, le=65535)
    public_origin: HttpUrl | None = None

    @model_validator(mode="after")
    def production_boundary(self) -> Self:
        if self.environment == "production":
            if not self.directory.is_absolute() or self.public_origin is None:
                raise ValueError(
                    "Production requires an absolute data directory and HTTPS public origin"
                )
            origin = self.public_origin
            if (
                origin.scheme != "https"
                or origin.username
                or origin.password
                or origin.query
                or origin.fragment
                or origin.path not in {None, "/"}
                or not origin.host
                or origin.host.endswith(".invalid")
            ):
                raise ValueError("Production public origin must be an actual HTTPS origin")
        return self

    def validate_storage(self) -> None:
        if self.environment == "production" and self.directory.stat().st_mode & 0o077:
            raise ValueError("Production data directory must be private (mode 0700)")
