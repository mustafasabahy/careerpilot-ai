"""A key alone enables chat; explicit demo always remains offline."""

from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ChatSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CAREERPILOT_CHAT_",
        env_file=".env",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )
    mode: Literal["demo", "llm"] | None = None
    provider: Literal["openai"] = "openai"
    model: str = Field(
        default="gpt-5.4-mini",
        min_length=1,
        max_length=128,
        pattern=r"^[\w.-]+$",
        validation_alias=AliasChoices("model", "CAREERPILOT_CHAT_MODEL", "OPENAI_MODEL"),
    )
    timeout_seconds: float = Field(default=30, gt=0, le=60, allow_inf_nan=False)
    max_retries: int = Field(default=0, ge=0, le=1)
    max_output_tokens: int = Field(default=2048, ge=256, le=4096)
    max_model_calls: int = Field(default=3, ge=1, le=8)
    max_tool_calls: int = Field(default=6, ge=1, le=12)
    overall_seconds: float = Field(default=90, gt=0, le=120, allow_inf_nan=False)
    history_messages: int = Field(default=12, ge=1, le=20)
    history_chars: int = Field(default=16000, ge=4000, le=32000)
    tool_bytes: int = Field(default=12000, ge=1000, le=16000)
    openai_api_key: SecretStr | None = Field(
        default=None, validation_alias="OPENAI_API_KEY", repr=False, exclude=True
    )

    @field_validator("openai_api_key")
    @classmethod
    def nonblank(cls, value: SecretStr | None) -> SecretStr | None:
        return value if value and value.get_secret_value().strip() else None

    @property
    def effective_mode(self) -> Literal["demo", "llm"]:
        return self.mode or ("llm" if self.openai_api_key else "demo")
