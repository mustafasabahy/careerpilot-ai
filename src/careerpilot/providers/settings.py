"""LLM settings are separate so existing health/offline workflows stay compatible."""

from typing import Literal, Self

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CAREERPILOT_LLM_",
        env_file=".env",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )
    provider: Literal["mock", "openai"] = "mock"
    model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("model", "CAREERPILOT_LLM_MODEL", "OPENAI_MODEL"),
    )
    timeout_seconds: float = Field(default=30, gt=0, le=120, allow_inf_nan=False)
    max_retries: int = Field(default=0, ge=0, le=2)
    max_output_tokens: int = Field(default=4096, ge=512, le=8192)
    openai_api_key: SecretStr | None = Field(
        default=None, validation_alias="OPENAI_API_KEY", exclude=True, repr=False
    )

    @field_validator("model")
    @classmethod
    def valid_model_identifier(cls, value: str | None) -> str | None:
        if value is not None and (
            not value or len(value) > 128 or any(character.isspace() for character in value)
        ):
            raise ValueError("Model must be a nonempty identifier of at most 128 characters")
        return value

    @model_validator(mode="after")
    def valid_provider(self) -> Self:
        if self.provider == "openai":
            if self.model is None or not self.model.strip():
                raise ValueError("OpenAI mode requires CAREERPILOT_LLM_MODEL")
            if self.openai_api_key is None or not self.openai_api_key.get_secret_value().strip():
                raise ValueError(
                    "OpenAI mode requires OPENAI_API_KEY; "
                    "a Codex subscription is not an API credential"
                )
        elif self.model not in {None, "fixture-v1"}:
            raise ValueError("Mock mode supports only the fixture-v1 model")
        return self
