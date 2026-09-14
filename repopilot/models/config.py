"""Model selection and API-key handling.

Keys live in environment variables only (``ANTHROPIC_API_KEY``,
``OPENAI_API_KEY``).  CLI entry points may load them from a git-ignored
``.env`` via :func:`load_env`; library code never reads files and never logs,
stores or echoes a key -- error messages say which variable is missing, not
what it contains.  The sandbox cannot see them either: containers start with
a clean environment and ``--network none``.

Two model roles follow the spec's routing ablation (§12.4): a *strong* model
for planning and patching and a *cheap* one for rewriting, summarizing and
classifying.  Both default to Anthropic's mid tier and can be overridden per
run with ``REPOPILOT_STRONG_MODEL`` / ``REPOPILOT_CHEAP_MODEL`` or a CLI flag.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from repopilot.models.pricing import is_priced


class Provider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


KEY_VARS: dict[Provider, str] = {
    Provider.ANTHROPIC: "ANTHROPIC_API_KEY",
    Provider.OPENAI: "OPENAI_API_KEY",
}

DEFAULT_STRONG_MODEL = "claude-sonnet-5"
DEFAULT_CHEAP_MODEL = "claude-haiku-4-5-20251001"

# Reasonable OpenAI counterparts, for `REPOPILOT_*_MODEL` overrides and the
# provider comparison later on.
OPENAI_STRONG_MODEL = "gpt-5.6-terra"
OPENAI_CHEAP_MODEL = "gpt-5.6-luna"

STRONG_MODEL_VAR = "REPOPILOT_STRONG_MODEL"
CHEAP_MODEL_VAR = "REPOPILOT_CHEAP_MODEL"


class ConfigError(ValueError):
    """A model id or key the runtime cannot work with; the message never contains a key."""


def provider_for(model: str) -> Provider:
    """Vendor from the model id: ``claude-*`` is Anthropic, ``gpt-*`` / ``o*`` is OpenAI."""
    name = model.lower()
    if name.startswith("claude"):
        return Provider.ANTHROPIC
    if name.startswith(("gpt", "o1", "o3", "o4", "chatgpt", "text-embedding")):
        return Provider.OPENAI
    raise ConfigError(
        f"cannot tell which provider serves model {model!r}; "
        "expected a Claude or GPT model id (see repopilot/models/config.py)"
    )


def api_key_for(provider: Provider, env: dict[str, str] | None = None) -> str:
    """The key for ``provider`` from the environment, or a ConfigError naming the variable."""
    environ = os.environ if env is None else env
    var = KEY_VARS[provider]
    key = (environ.get(var) or "").strip()
    if not key:
        raise ConfigError(
            f"{var} is not set; put it in .env (git-ignored, see .env.example) "
            f"or export it in the shell"
        )
    return key


def has_api_key(provider: Provider, env: dict[str, str] | None = None) -> bool:
    try:
        api_key_for(provider, env)
    except ConfigError:
        return False
    return True


@dataclass(frozen=True)
class ModelSettings:
    """Which model plays which role in a run."""

    strong: str = DEFAULT_STRONG_MODEL
    cheap: str = DEFAULT_CHEAP_MODEL

    def __post_init__(self) -> None:
        for role, model in (("strong", self.strong), ("cheap", self.cheap)):
            provider_for(model)  # raises for unknown vendors
            if not is_priced(model):
                raise ConfigError(
                    f"{role} model {model!r} has no entry in repopilot/models/pricing.py; "
                    "add its price so costs and budgets stay meaningful"
                )

    @property
    def providers(self) -> frozenset[Provider]:
        return frozenset({provider_for(self.strong), provider_for(self.cheap)})

    def require_keys(self, env: dict[str, str] | None = None) -> None:
        """Fail fast, before any sandbox work, if a needed key is missing."""
        for provider in sorted(self.providers):
            api_key_for(provider, env)

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        strong: str | None = None,
        cheap: str | None = None,
    ) -> ModelSettings:
        """CLI flags win over environment variables, which win over the defaults."""
        environ = os.environ if env is None else env
        return cls(
            strong=strong or environ.get(STRONG_MODEL_VAR) or DEFAULT_STRONG_MODEL,
            cheap=cheap or environ.get(CHEAP_MODEL_VAR) or DEFAULT_CHEAP_MODEL,
        )


def load_env(path: str | Path = ".env") -> bool:
    """Load ``KEY=value`` lines from a ``.env`` file into ``os.environ`` (never overriding
    variables that are already set).  Returns whether the file existed.

    Only CLI entry points call this; importing the library never touches the
    filesystem.  Uses python-dotenv when installed and a minimal parser otherwise,
    so a missing optional dependency cannot block a run.
    """
    file = Path(path)
    if not file.is_file():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is a declared dependency
        _load_env_minimal(file)
    else:
        load_dotenv(file, override=False)
    return True


def _load_env_minimal(file: Path) -> None:
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        if name and name not in os.environ:
            os.environ[name] = value
