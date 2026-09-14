"""Model access for the agent: neutral types, provider adapters, prices, budgets.

    from repopilot.models import ModelSettings, client_for, user, system

    settings = ModelSettings.from_env()          # strong / cheap model ids
    settings.require_keys()                      # fail fast if a key is missing
    llm = client_for(settings.strong)            # AnthropicClient or OpenAIClient
    reply = llm.complete([system("..."), user("...")], tools=[...])
    reply.text, reply.tool_calls, reply.usage, reply.cost_usd

Keys come from the environment only (``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY``);
``load_env()`` reads a git-ignored ``.env`` in CLI entry points.
"""

from repopilot.models.client import (
    AnthropicClient,
    FakeClient,
    ModelClient,
    ModelError,
    OpenAIClient,
    client_for,
)
from repopilot.models.config import (
    ConfigError,
    ModelSettings,
    Provider,
    api_key_for,
    has_api_key,
    load_env,
    provider_for,
)
from repopilot.models.ledger import BudgetExceeded, Ledger
from repopilot.models.pricing import PRICES, PRICES_AS_OF, UnknownModelError, estimate_cost
from repopilot.models.types import (
    Message,
    ModelResponse,
    Role,
    StopReason,
    ToolCall,
    ToolSpec,
    Usage,
    assistant,
    system,
    tool_result,
    user,
)

__all__ = [
    "PRICES",
    "PRICES_AS_OF",
    "AnthropicClient",
    "BudgetExceeded",
    "ConfigError",
    "FakeClient",
    "Ledger",
    "Message",
    "ModelClient",
    "ModelError",
    "ModelResponse",
    "ModelSettings",
    "OpenAIClient",
    "Provider",
    "Role",
    "StopReason",
    "ToolCall",
    "ToolSpec",
    "UnknownModelError",
    "Usage",
    "api_key_for",
    "assistant",
    "client_for",
    "estimate_cost",
    "has_api_key",
    "load_env",
    "provider_for",
    "system",
    "tool_result",
    "user",
]
