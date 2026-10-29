"""LangChain tool calling; normalized failures and no content tracing."""

from collections.abc import Callable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langsmith import tracing_context
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAIError,
    RateLimitError,
)
from pydantic import ValidationError

from careerpilot.application.chat_agent_contracts import (
    AgentMessage,
    ModelReply,
    ToolCall,
    ToolDefinition,
)
from careerpilot.application.execution import current_run, remaining_timeout, retry_read
from careerpilot.application.extraction import (
    InvalidModelOutput,
    ProviderAuthenticationError,
    ProviderFailure,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderTransientFailure,
)
from careerpilot.domain.reliability import RetryPolicy
from careerpilot.providers.openai import retry_after


class OpenAIChatModel:
    def __init__(
        self,
        invoke: Callable[[list[BaseMessage], tuple[ToolDefinition, ...], float], object],
        max_retries: int = 0,
    ) -> None:
        self.invoke = invoke
        self.max_retries = max_retries

    def complete(
        self, messages: tuple[AgentMessage, ...], tools: tuple[ToolDefinition, ...], timeout: float
    ) -> ModelReply:
        native: list[BaseMessage] = []
        for message in messages:
            if message.role == "system":
                native.append(SystemMessage(content=message.content))
            elif message.role == "user":
                native.append(HumanMessage(content=message.content))
            elif message.role == "tool":
                native.append(
                    ToolMessage(content=message.content, tool_call_id=message.call_id or "")
                )
            else:
                native.append(
                    AIMessage(
                        content=message.content,
                        tool_calls=[
                            {
                                "id": call.id,
                                "name": call.name,
                                "args": call.arguments,
                                "type": "tool_call",
                            }
                            for call in message.calls
                        ],
                    )
                )
        return retry_read(
            lambda: self._once(native, tools, remaining_timeout(timeout)),
            RetryPolicy(max_retries=self.max_retries),
            resource="model_calls",
        )

    def _once(
        self, messages: list[BaseMessage], tools: tuple[ToolDefinition, ...], timeout: float
    ) -> ModelReply:
        try:
            with tracing_context(enabled=False):
                result = self.invoke(messages, tools, timeout)
        except APITimeoutError:
            raise ProviderTimeout("Model request timed out") from None
        except AuthenticationError:
            raise ProviderAuthenticationError("Check OPENAI_API_KEY and model access") from None
        except RateLimitError as error:
            raise ProviderRateLimited(
                "Provider quota or rate limit reached",
                retry_after_seconds=retry_after(error.response.headers),
                retryable=error.code not in {"insufficient_quota", "billing_hard_limit_reached"},
            ) from None
        except APIConnectionError:
            raise ProviderTransientFailure("Model connection unavailable") from None
        except APIStatusError as error:
            if error.status_code in {408, 500, 502, 503, 504}:
                raise ProviderTransientFailure(
                    "Provider temporarily unavailable",
                    retry_after_seconds=retry_after(error.response.headers),
                ) from None
            raise ProviderFailure("Provider rejected request") from None
        except OpenAIError:
            raise ProviderFailure("Provider request failed") from None
        if (
            not isinstance(result, AIMessage)
            or result.invalid_tool_calls
            or result.additional_kwargs.get("refusal")
            or result.response_metadata.get("finish_reason") in {"length", "content_filter"}
        ):
            raise InvalidModelOutput("Invalid or incomplete model response")
        if not isinstance(result.content, str):
            raise InvalidModelOutput("Unexpected model content format")
        try:
            reply = ModelReply(
                text=result.content,
                calls=tuple(
                    ToolCall(id=c.get("id") or "", name=c["name"], arguments=c["args"])
                    for c in result.tool_calls
                ),
                input_tokens=result.usage_metadata["input_tokens"] if result.usage_metadata else 0,
                output_tokens=result.usage_metadata["output_tokens"]
                if result.usage_metadata
                else 0,
            )
        except ValidationError:
            raise InvalidModelOutput("Malformed model tool call") from None
        if not reply.text.strip() and not reply.calls:
            raise InvalidModelOutput("Empty model response")
        if control := current_run():
            control.note_usage(reply.input_tokens, reply.output_tokens)
        return reply
