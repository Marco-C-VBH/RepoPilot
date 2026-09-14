"""Unit tests for repopilot.models: types, pricing, config, ledger, fake client.

No network, no SDKs, no keys: the adapters are exercised in
``test_model_clients.py`` against stand-in SDK objects.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from repopilot.models import (
    PRICES,
    BudgetExceeded,
    ConfigError,
    FakeClient,
    Ledger,
    Message,
    ModelError,
    ModelResponse,
    ModelSettings,
    Provider,
    Role,
    StopReason,
    ToolCall,
    ToolSpec,
    UnknownModelError,
    Usage,
    api_key_for,
    assistant,
    estimate_cost,
    has_api_key,
    load_env,
    provider_for,
    system,
    tool_result,
    user,
)
from repopilot.models.config import (
    DEFAULT_CHEAP_MODEL,
    DEFAULT_STRONG_MODEL,
    OPENAI_CHEAP_MODEL,
    OPENAI_STRONG_MODEL,
)
from repopilot.models.pricing import price_for

# ----------------------------------------------------------------------------- types


def test_tool_call_parses_json_arguments() -> None:
    call = ToolCall.from_json_arguments("c1", "read_file", '{"path": "a.py", "start": 1}')
    assert call.arguments == {"path": "a.py", "start": 1}
    assert call.parse_error is None


@pytest.mark.parametrize(
    "raw, fragment",
    [('{"path": "a.py"', "not valid JSON"), ("[1, 2]", "must be a JSON object")],
)
def test_tool_call_keeps_invalid_arguments_as_an_error(raw: str, fragment: str) -> None:
    call = ToolCall.from_json_arguments("c1", "read_file", raw)
    assert call.arguments == {}
    assert call.parse_error is not None and fragment in call.parse_error


def test_tool_call_without_arguments_is_empty_not_an_error() -> None:
    assert ToolCall.from_json_arguments("c1", "run_tests", None).parse_error is None
    assert ToolCall.from_json_arguments("c1", "run_tests", "  ").arguments == {}


def test_message_invariants() -> None:
    with pytest.raises(ValueError, match="only assistant"):
        Message(Role.USER, "x", tool_calls=(ToolCall("1", "t"),))
    with pytest.raises(ValueError, match="tool_call_id"):
        Message(Role.TOOL, "result")
    with pytest.raises(ValueError, match="only valid on tool"):
        Message(Role.USER, "x", tool_call_id="1")
    with pytest.raises(ValueError, match="only assistant messages can carry raw_items"):
        Message(Role.USER, "x", raw_items=({"type": "reasoning"},))
    assert tool_result("1", "ok").tool_call_id == "1"
    assert assistant("hi").role is Role.ASSISTANT


def test_usage_adds_and_totals() -> None:
    total = Usage(10, 2, 4, 1, reasoning_tokens=1) + Usage(5, 3)
    assert total == Usage(15, 5, 4, 1, reasoning_tokens=1)
    assert total.total_tokens == 20


def test_response_record_and_message() -> None:
    call = ToolCall("c1", "read_file", {"path": "a.py"})
    bad = ToolCall("c2", "read_file", parse_error="nope")
    response = ModelResponse(
        model="m",
        provider="fake",
        text="looking",
        tool_calls=(call, bad),
        stop_reason=StopReason.TOOL_USE,
        usage=Usage(100, 20, cache_read_tokens=50),
        latency_ms=12,
        cost_usd=0.0001234567,
        response_id="r1",
    )
    assert response.wants_tools
    assert response.as_message() == Message(Role.ASSISTANT, "looking", tool_calls=(call, bad))
    record = response.to_record()
    assert record["tool_calls"] == ["read_file", "read_file"]
    assert record["invalid_tool_calls"] == 1
    assert record["cost_usd"] == 0.000123
    assert record["stop_reason"] == "tool_use"
    assert record["cache_read_tokens"] == 50
    assert record["reasoning_tokens"] == 0


# --------------------------------------------------------------------------- pricing


def test_every_default_model_is_priced() -> None:
    for model in (
        DEFAULT_STRONG_MODEL,
        DEFAULT_CHEAP_MODEL,
        OPENAI_STRONG_MODEL,
        OPENAI_CHEAP_MODEL,
    ):
        assert price_for(model) is not None


def test_price_lookup_uses_longest_prefix_and_dated_ids() -> None:
    assert price_for("claude-haiku-4-5-20251001") is PRICES["claude-haiku-4-5"]
    assert price_for("claude-sonnet-5") is PRICES["claude-sonnet-5"]
    with pytest.raises(UnknownModelError, match="no price known"):
        price_for("claude-sonnet")  # a prefix of a key is not a match
    with pytest.raises(UnknownModelError):
        price_for("llama-3")


def test_estimate_cost_bills_cache_reads_and_writes_separately() -> None:
    # claude-sonnet-5: $2 in / $10 out; reads at 10% of input, writes +25%.
    plain = estimate_cost("claude-sonnet-5", Usage(input_tokens=1_000_000, output_tokens=100_000))
    assert plain == pytest.approx(2.0 + 1.0)
    cached = estimate_cost(
        "claude-sonnet-5", Usage(input_tokens=1_000_000, output_tokens=0, cache_read_tokens=500_000)
    )
    assert cached == pytest.approx(0.5 * 2.0 + 0.5 * 0.2)
    written = estimate_cost(
        "claude-sonnet-5",
        Usage(input_tokens=1_000_000, output_tokens=0, cache_write_tokens=1_000_000),
    )
    assert written == pytest.approx(2.0 * 1.25)
    # gpt-5.6-luna: $0.20 in / $1.20 out; cached input at 10%, no write surcharge.
    assert estimate_cost("gpt-5.6-luna", Usage(1_000_000, 1_000_000)) == pytest.approx(1.4)
    assert estimate_cost("gpt-5.6-luna", Usage(0, 0)) == 0.0


# ---------------------------------------------------------------------------- config


def test_provider_inference() -> None:
    assert provider_for("claude-sonnet-5") is Provider.ANTHROPIC
    assert provider_for("gpt-5.6-terra") is Provider.OPENAI
    assert provider_for("o4-mini") is Provider.OPENAI
    with pytest.raises(ConfigError, match="cannot tell which provider"):
        provider_for("mistral-large")


def test_api_key_errors_name_the_variable_but_never_the_value() -> None:
    env = {"ANTHROPIC_API_KEY": "  ", "OPENAI_API_KEY": "sk-secret-value"}
    with pytest.raises(ConfigError) as excinfo:
        api_key_for(Provider.ANTHROPIC, env)
    assert "ANTHROPIC_API_KEY is not set" in str(excinfo.value)
    assert "sk-secret" not in str(excinfo.value)
    assert api_key_for(Provider.OPENAI, env) == "sk-secret-value"
    assert has_api_key(Provider.OPENAI, env) and not has_api_key(Provider.ANTHROPIC, env)


def test_settings_defaults_env_overrides_and_flags() -> None:
    assert ModelSettings.from_env({}) == ModelSettings(DEFAULT_STRONG_MODEL, DEFAULT_CHEAP_MODEL)
    from_env = ModelSettings.from_env({"REPOPILOT_STRONG_MODEL": "gpt-5.6-terra"})
    assert from_env.strong == "gpt-5.6-terra" and from_env.cheap == DEFAULT_CHEAP_MODEL
    flagged = ModelSettings.from_env(
        {"REPOPILOT_STRONG_MODEL": "gpt-5.6-terra"}, strong="claude-opus-5", cheap="gpt-5.6-luna"
    )
    assert (flagged.strong, flagged.cheap) == ("claude-opus-5", "gpt-5.6-luna")
    assert flagged.providers == {Provider.ANTHROPIC, Provider.OPENAI}


def test_settings_reject_unpriced_or_unknown_models() -> None:
    with pytest.raises(ConfigError, match="no entry in repopilot/models/pricing.py"):
        ModelSettings(strong="claude-future-9")
    with pytest.raises(ConfigError, match="cannot tell which provider"):
        ModelSettings(cheap="gemini-pro")


def test_require_keys_checks_every_provider_in_use() -> None:
    settings = ModelSettings(strong="claude-sonnet-5", cheap="gpt-5.6-luna")
    settings.require_keys({"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "b"})
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        settings.require_keys({"ANTHROPIC_API_KEY": "a"})


def test_load_env_reads_dotenv_without_overriding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "# keys\nANTHROPIC_API_KEY='from-file'\nexport OPENAI_API_KEY=also-from-file\n\nBROKEN\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "from-shell")
    assert load_env(dotenv) is True
    assert os.environ["ANTHROPIC_API_KEY"] == "from-file"
    assert os.environ["OPENAI_API_KEY"] == "from-shell"  # shell wins over the file
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert load_env(tmp_path / "missing.env") is False


# ---------------------------------------------------------------------------- ledger


def _response(cost: float, tokens: int = 10, model: str = "m") -> ModelResponse:
    return ModelResponse(
        model=model,
        provider="fake",
        text="",
        tool_calls=(),
        stop_reason=StopReason.END_TURN,
        usage=Usage(tokens, tokens),
        latency_ms=5,
        cost_usd=cost,
    )


def test_ledger_accumulates_and_reports_per_model() -> None:
    ledger = Ledger()
    ledger.record(_response(0.01, model="strong"))
    ledger.record(_response(0.002, tokens=3, model="cheap"))
    ledger.record(_response(0.01, model="strong"))
    assert ledger.calls == 3
    assert ledger.usage == Usage(23, 23)
    assert ledger.cost_usd == pytest.approx(0.022)
    assert ledger.latency_ms == 15
    record = ledger.to_record()
    assert record["by_model"]["strong"] == {
        "calls": 2,
        "input_tokens": 20,
        "output_tokens": 20,
        "cost_usd": 0.02,
    }
    assert ledger.remaining_cost_usd is None


def test_ledger_enforces_cost_and_token_caps() -> None:
    ledger = Ledger(max_cost_usd=0.05)
    ledger.record(_response(0.04))
    assert ledger.remaining_cost_usd == pytest.approx(0.01)
    with pytest.raises(BudgetExceeded, match="cost"):
        ledger.record(_response(0.02))
    assert ledger.cost_usd == pytest.approx(0.06)  # the call that crossed the line still counts

    tokens = Ledger(max_tokens=25)
    tokens.record(_response(0.0, tokens=10))
    with pytest.raises(BudgetExceeded, match="tokens"):
        tokens.record(_response(0.0, tokens=10))


# ------------------------------------------------------------------------ fake client


def test_fake_client_plays_scripts_and_records_calls() -> None:
    call = ToolCall("c1", "read_file", {"path": "x.py"})
    fake = FakeClient(script=[(call,), "done"], price_per_token=0.001)
    tools = [ToolSpec("read_file", "read a file")]

    first = fake.complete([system("sys"), user("fix it")], tools=tools)
    assert first.stop_reason is StopReason.TOOL_USE and first.tool_calls == (call,)
    assert first.cost_usd > 0

    second = fake.complete([user("fix it"), first.as_message(), tool_result("c1", "contents")])
    assert second.stop_reason is StopReason.END_TURN and second.text == "done"

    assert [c.tools for c in fake.calls] == [tuple(tools), ()]
    assert fake.calls[0].messages[0].role is Role.SYSTEM
    with pytest.raises(ModelError, match="fake script exhausted"):
        fake.complete([user("again")])


def test_clients_validate_message_order() -> None:
    fake = FakeClient(script=["x", "y"])
    with pytest.raises(ValueError, match="at least one message"):
        fake.complete([])
    with pytest.raises(ValueError, match="only one system"):
        fake.complete([system("a"), user("b"), system("c")])
    with pytest.raises(ValueError, match="user message after"):
        fake.complete([system("a")])
