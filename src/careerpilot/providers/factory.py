"""The only module constructing provider clients/models."""

import warnings
from collections.abc import Callable

from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from careerpilot.application.chat_agent_contracts import ChatModel, ToolDefinition
from careerpilot.application.execution import remaining_timeout
from careerpilot.application.extraction import JobModel, ProviderConfigurationError
from careerpilot.application.preparation_contracts import DraftPlanner
from careerpilot.application.security import validate_outbound
from careerpilot.domain.extraction import JobExtraction
from careerpilot.providers.chat_settings import ChatSettings
from careerpilot.providers.mock import MockJobModel
from careerpilot.providers.openai import OpenAIJobModel
from careerpilot.providers.settings import LLMSettings


def _configuration(settings: LLMSettings | None) -> LLMSettings:
    try:
        # Revalidate manually constructed/copied settings too.
        configured = (
            LLMSettings()
            if settings is None
            else LLMSettings.model_validate(
                {
                    **settings.model_dump(),
                    "OPENAI_API_KEY": settings.openai_api_key,
                }
            )
        )
    except ValidationError:
        raise ProviderConfigurationError(
            "Invalid LLM configuration: use mock/fixture-v1, or set provider=openai, "
            "CAREERPILOT_LLM_MODEL and OPENAI_API_KEY; check timeout/retry/token bounds. "
            "Codex/ChatGPT subscriptions do not supply application API credentials."
        ) from None
    return configured


def _structured(
    configured: LLMSettings, schema: type[BaseModel]
) -> Callable[[list[BaseMessage]], object]:
    # These were checked above. No environment fallback or arbitrary endpoint is used.
    if configured.model is None or configured.openai_api_key is None:
        raise ProviderConfigurationError(
            "OpenAI mode requires an explicit model and API credential"
        )
    validate_outbound("https://api.openai.com/v1", purpose="model", enabled=True)
    try:
        model = ChatOpenAI(
            model=configured.model,
            api_key=configured.openai_api_key,
            base_url="https://api.openai.com/v1",
            timeout=configured.timeout_seconds,
            max_retries=0,
            max_completion_tokens=configured.max_output_tokens,
            use_responses_api=False,
            streaming=False,
            store=False,
        )
    except ValueError:
        raise ProviderConfigurationError(
            "Could not construct the model client; check SDK and environment configuration"
        ) from None
    try:
        with warnings.catch_warnings():
            # LangChain may warn and fall back to tool calling for legacy models.
            # This adapter promises native JSON output, so reject that fallback.
            warnings.simplefilter("error", UserWarning)
            structured = model.with_structured_output(
                schema,
                method="json_schema",
                strict=True,
                include_raw=True,
            )
    except UserWarning:
        raise ProviderConfigurationError(
            "Selected model/schema does not support the required native structured-output mode"
        ) from None

    def invoke(messages: list[BaseMessage]) -> object:
        return structured.invoke(messages, timeout=remaining_timeout(configured.timeout_seconds))

    return invoke


def build_job_model(settings: LLMSettings | None = None) -> JobModel:
    configured = _configuration(settings)
    if configured.provider == "mock":
        return MockJobModel()
    return OpenAIJobModel(
        configured.model or "",
        _structured(configured, JobExtraction),
        max_retries=configured.max_retries,
    )


def build_draft_planner(settings: LLMSettings | None = None) -> "DraftPlanner":
    from careerpilot.domain.preparation import DraftPlan
    from careerpilot.providers.draft_planner import DeterministicDraftPlanner, OpenAIDraftPlanner

    configured = _configuration(settings)
    if configured.provider == "mock":
        return DeterministicDraftPlanner()
    return OpenAIDraftPlanner(
        configured.model or "",
        _structured(configured, DraftPlan),
        max_retries=configured.max_retries,
    )


def build_chat_model(settings: "ChatSettings") -> "ChatModel":
    from careerpilot.providers.chat import OpenAIChatModel

    if settings.effective_mode != "llm" or settings.openai_api_key is None:
        raise ProviderConfigurationError("LLM chat requires OPENAI_API_KEY on the server")
    validate_outbound("https://api.openai.com/v1", purpose="model", enabled=True)
    try:
        model = ChatOpenAI(
            model=settings.model,
            api_key=settings.openai_api_key,
            base_url="https://api.openai.com/v1",
            timeout=settings.timeout_seconds,
            max_retries=0,
            max_completion_tokens=settings.max_output_tokens,
            use_responses_api=False,
            streaming=False,
            store=False,
        )
    except (ValueError, TypeError):
        raise ProviderConfigurationError(
            "Unable to initialize chat provider; check server configuration"
        ) from None

    def invoke(
        messages: list[BaseMessage], definitions: tuple[ToolDefinition, ...], timeout: float
    ) -> object:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": d.name,
                    "description": d.description,
                    "parameters": d.parameters,
                    "strict": True,
                },
            }
            for d in definitions
        ]
        return model.bind_tools(tools, strict=True, parallel_tool_calls=False).invoke(
            messages, config={"callbacks": []}, timeout=min(timeout, settings.timeout_seconds)
        )

    return OpenAIChatModel(invoke, settings.max_retries)
