"""Provider-neutral conversational messages, budgets and durable execution summaries."""

from typing import Literal, Protocol

from pydantic import Field

from careerpilot.domain.common import Model


class ToolCall(Model):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z_]+$")
    arguments: dict[str, object]


class AgentMessage(Model):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(max_length=16000)
    calls: tuple[ToolCall, ...] = ()
    call_id: str | None = None


class ModelReply(Model):
    text: str = Field(default="", max_length=8000)
    calls: tuple[ToolCall, ...] = Field(default=(), max_length=4)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class ToolDefinition(Model):
    name: str
    description: str
    parameters: dict[str, object]
    effect: Literal["read", "write"]


class ChatModel(Protocol):
    def complete(
        self, messages: tuple[AgentMessage, ...], tools: tuple[ToolDefinition, ...], timeout: float
    ) -> ModelReply: ...


class AgentLimits(Model):
    model_calls: int = Field(default=3, ge=1, le=8)
    tool_calls: int = Field(default=6, ge=1, le=12)
    seconds: float = Field(default=90, gt=0, le=120, allow_inf_nan=False)
    history_messages: int = Field(default=12, ge=1, le=20)
    history_chars: int = Field(default=16000, ge=4000, le=32000)
    tool_bytes: int = Field(default=12000, ge=1000, le=16000)


class ToolStep(Model):
    name: str
    fingerprint: str
    effect: Literal["read", "write"]
    status: Literal["started", "completed", "failed", "unknown"]


class AgentMetadata(Model):
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    steps: tuple[ToolStep, ...] = ()
    error_code: str | None = None
    elapsed_seconds: float = 0
