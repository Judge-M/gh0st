from types import SimpleNamespace

import pytest

from gh0st.model_runtime import AisuiteModelAdapter, UnpricedModelError
from gh0st.models import Ticket


class StaticSecrets:
    def get_secret(self, name):
        return f"secret-for-{name}"


def response():
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="done", tool_calls=[]))],
        usage=SimpleNamespace(prompt_tokens=20, completion_tokens=5, total_tokens=25),
    )


def test_direct_model_call_resolves_provider_secret_and_accounts_usage():
    calls = []

    class Client:
        class Chat:
            class Completions:
                def create(self, **kwargs):
                    calls.append(kwargs)
                    return response()
            completions = Completions()
        chat = Chat()

    adapter = AisuiteModelAdapter(
        client_factory=lambda configs: (calls.append(configs) or Client()),
        secret_resolver=StaticSecrets(),
        cost_calculator=lambda count, model, token_type: count * (0.001 if token_type == "input" else 0.002),
    )
    result = adapter.complete_turn([], None, 100, "openai:gpt-test",
                                   {"pricing_model": "gpt-test"})
    assert result["content"] == "done"
    assert result["total_tokens"] == 25
    assert result["cost_usd"] == pytest.approx(0.03)
    assert calls[0] == {"openai": {"api_key": "secret-for-OPENAI_API_KEY"}}
    assert calls[1]["model"] == "openai:gpt-test"


def test_unknown_price_blocks_call_before_provider_request():
    calls = []
    adapter = AisuiteModelAdapter(
        client_factory=lambda config: calls.append(config),
        secret_resolver=StaticSecrets(),
        cost_calculator=lambda *_: (_ for _ in ()).throw(KeyError("unknown model")),
    )
    with pytest.raises(UnpricedModelError, match="No price is available"):
        adapter.complete_turn([], None, 100, "openai:unknown", {})
    assert calls == []


def test_fallback_model_uses_its_own_configured_price():
    calls = []

    class Client:
        class Chat:
            class Completions:
                def create(self, **kwargs):
                    calls.append(kwargs["model"])
                    if kwargs["model"] == "openai:primary":
                        raise TimeoutError("provider timed out")
                    return response()
            completions = Completions()
        chat = Chat()

    adapter = AisuiteModelAdapter(
        client_factory=lambda _: Client(), secret_resolver=StaticSecrets(),
        cost_calculator=lambda *_: 0.001, max_attempts=1,
    )
    ticket = Ticket(task_id="task", capability="unit_testing", objective="run tests",
                    workspace_path="/tmp/task", assigned_model="openai:primary",
                    gateway_allowance={
                        "pricing_model": "primary-price", "fallback_models": ["openai:backup"],
                        "fallback_pricing": {"openai:backup": {"pricing_model": "backup-price"}},
                    })
    result = adapter.complete_for_ticket([], None, 100, ticket)
    assert calls == ["openai:primary", "openai:backup"]
    assert result["fallback_from"] == "openai:primary"
    assert result["selected_model"] == "openai:backup"


def test_streaming_assembles_text_tool_calls_and_usage():
    calls = []

    class Client:
        class Chat:
            class Completions:
                def create(self, **kwargs):
                    calls.append(kwargs)
                    return iter([
                        {"choices": [{"delta": {"content": "Hello "}}]},
                        {"choices": [{"delta": {"content": "world"}}]},
                        {"choices": [{"delta": {"tool_calls": [{
                            "index": 0, "id": "call_1", "function": {"name": "local_", "arguments": "{\"a\":"}
                        }]}}]},
                        {"choices": [{"delta": {"tool_calls": [{
                            "index": 0, "function": {"name": "workbench", "arguments": "1}"}
                        }]}}]},
                        {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}},
                    ])
            completions = Completions()
        chat = Chat()

    deltas = []
    adapter = AisuiteModelAdapter(
        client_factory=lambda _: Client(), secret_resolver=StaticSecrets(),
        cost_calculator=lambda *_: 0.01,
    )
    result = adapter.complete_turn([], None, 100, "openai:gpt-test",
                                   {"pricing_model": "gpt-test"}, on_text_delta=deltas.append)
    assert result["content"] == "Hello world"
    assert result["tool_calls"][0]["function"] == {"name": "local_workbench", "arguments": "{\"a\":1}"}
    assert result["total_tokens"] == 25
    assert deltas == ["Hello ", "world"]
    assert calls[0]["stream"] is True
    assert calls[0]["stream_options"] == {"include_usage": True}
