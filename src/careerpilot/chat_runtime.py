"""Chat composition root; startup builds clients but performs no model requests."""

from pydantic import ValidationError

from careerpilot.agent_portal import PortalAgentTools
from careerpilot.application.chat_agent import ConversationalAgent
from careerpilot.application.chat_agent_contracts import AgentLimits, ChatModel
from careerpilot.application.conversations import ConversationService
from careerpilot.application.extraction import ProviderConfigurationError
from careerpilot.chat_portal import PortalChatBackend
from careerpilot.persistence.checkpoints import local_checkpointer
from careerpilot.persistence.conversations import ConversationDatabase
from careerpilot.portal import Portal
from careerpilot.providers.chat_settings import ChatSettings
from careerpilot.providers.factory import build_chat_model
from careerpilot.workflows.chat_agent import ChatAgentGraph


def configure_chat(
    portal: Portal, model: ChatModel | None = None
) -> tuple[ConversationService, dict[str, object]]:
    error = None
    limits = AgentLimits()
    mode = "llm"
    try:
        settings = ChatSettings()
        mode = settings.effective_mode
        limits = AgentLimits(
            model_calls=settings.max_model_calls,
            tool_calls=settings.max_tool_calls,
            seconds=settings.overall_seconds,
            history_messages=settings.history_messages,
            history_chars=settings.history_chars,
            tool_bytes=settings.tool_bytes,
        )
        if mode == "llm" and model is None:
            model = build_chat_model(settings)
    except (ValidationError, ProviderConfigurationError):
        error = (
            "LLM setup needs valid CAREERPILOT_CHAT_* settings and OPENAI_API_KEY "
            "on the server. Set CAREERPILOT_CHAT_MODE=demo for offline use; restart after changes."
        )
    agent = (
        ConversationalAgent(
            model, PortalAgentTools(portal), configuration_error=error, limits=limits
        )
        if mode == "llm"
        else None
    )
    store = ConversationDatabase(portal.root / "conversations.sqlite3")
    graph_agent = (
        ChatAgentGraph(
            agent,
            store,
            lambda: local_checkpointer(
                portal.root / "agent-checkpoints.sqlite3",
                separate_from=(portal.database.path, store.path),
            ),
        )
        if agent
        else None
    )
    service = ConversationService(
        ConversationDatabase(portal.root / "conversations.sqlite3"),
        PortalChatBackend(portal),
        graph_agent,
    )
    return service, {
        "mode": mode,
        "ready": error is None,
        "error": error,
        "external_actions": "simulated",
    }
