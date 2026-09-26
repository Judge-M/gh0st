"""In-process provider calls with explicit pricing and bounded retries."""

from decimal import Decimal
import os
from typing import Any, Callable, Optional

import httpx
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_random_exponential

from .models import Ticket, ToolSchema
from .secrets import SecretResolver, get_secret_resolver


class UnpricedModelError(ValueError):
    """Raised before dispatch when a model has no usable pricing data."""


def is_transient_provider_error(error: BaseException) -> bool:
    if isinstance(error, (httpx.TimeoutException, httpx.NetworkError, TimeoutError, ConnectionError)):
        return True
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None) or getattr(response, "status_code", None)
    return status == 429 or status in {500, 502, 503, 504}


class AisuiteModelAdapter:
    """Single-turn model adapter; tool execution remains in EphemeralWorker."""

    def __init__(self, *, client_factory: Optional[Callable[[dict[str, dict[str, str]]], Any]] = None,
                 secret_resolver: Optional[SecretResolver] = None,
                 cost_calculator: Optional[Callable[[int, str, str], Any]] = None,
                 max_attempts: int = 3):
        self.client_factory = client_factory or self._create_client
        self.secret_resolver = secret_resolver or get_secret_resolver()
        self.cost_calculator = cost_calculator
        self.max_attempts = max_attempts

    @staticmethod
    def _create_client(provider_configs: dict[str, dict[str, str]]) -> Any:
        try:
            import aisuite as ai
        except ImportError as exc:
            raise RuntimeError("Install gh0st-core with the selected aisuite provider extra") from exc
        return ai.Client(provider_configs=provider_configs)

    @staticmethod
    def _pricing_function() -> Callable[[int, str, str], Any]:
        try:
            from tokencost import calculate_cost_by_tokens
        except ImportError as exc:
            raise RuntimeError("tokencost is required for model budget enforcement") from exc
        return calculate_cost_by_tokens

    @staticmethod
    def _provider_key(provider: str) -> Optional[str]:
        if provider in {"ollama", "lmstudio"}:
            return None
        aliases = {"openrouter": "OPENROUTER_API_KEY", "gemini": "GOOGLE_API_KEY"}
        return aliases.get(provider, f"{provider.upper()}_API_KEY")

    def _provider_config(self, provider: str) -> tuple[str, dict[str, str]]:
        implementation = "openai" if provider == "openrouter" else provider
        config: dict[str, str] = {}
        secret_name = self._provider_key(provider)
        if secret_name:
            config["api_key"] = self.secret_resolver.get_secret(secret_name)
        if provider == "openrouter":
            config["base_url"] = os.getenv("GH0ST_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        elif provider == "openai" and os.getenv("GH0ST_OPENAI_BASE_URL"):
            config["base_url"] = os.environ["GH0ST_OPENAI_BASE_URL"]
        elif provider in {"ollama", "lmstudio"}:
            base_url = os.getenv(f"GH0ST_{provider.upper()}_BASE_URL")
            if base_url:
                config["base_url"] = base_url
        return implementation, config

    @staticmethod
    def _pricing_model(model: str, allowance: dict[str, Any]) -> str:
        return str(allowance.get("pricing_model") or model.split(":", 1)[-1])

    def _custom_price(self, allowance: dict[str, Any]) -> Optional[tuple[float, float]]:
        input_rate = allowance.get("input_cost_per_million_usd")
        output_rate = allowance.get("output_cost_per_million_usd")
        if input_rate is None and output_rate is None:
            return None
        if input_rate is None or output_rate is None:
            raise UnpricedModelError("Both input and output prices must be configured")
        return float(input_rate), float(output_rate)

    def _estimate_cost(self, model: str, allowance: dict[str, Any],
                       input_tokens: int, output_tokens: int) -> float:
        custom = self._custom_price(allowance)
        if custom is not None:
            return (input_tokens * custom[0] + output_tokens * custom[1]) / 1_000_000
        calculator = self.cost_calculator or self._pricing_function()
        pricing_model = self._pricing_model(model, allowance)
        try:
            input_cost = calculator(input_tokens, pricing_model, "input")
            output_cost = calculator(output_tokens, pricing_model, "output")
            return float(Decimal(str(input_cost)) + Decimal(str(output_cost)))
        except Exception as exc:
            raise UnpricedModelError(
                f"No price is available for {model}; add a pricing_model or per-million input/output rates to the model catalog"
            ) from exc

    def _ensure_priced(self, model: str, allowance: dict[str, Any]) -> None:
        self._estimate_cost(model, allowance, 1, 1)

    @staticmethod
    def _plain(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json", exclude_none=True)
        if isinstance(value, dict):
            return {key: AisuiteModelAdapter._plain(item) for key, item in value.items() if item is not None}
        if isinstance(value, (list, tuple)):
            return [AisuiteModelAdapter._plain(item) for item in value]
        if hasattr(value, "__dict__"):
            return AisuiteModelAdapter._plain(vars(value))
        return value

    def _one_attempt(self, messages: list[dict[str, Any]], attached_tool: Optional[ToolSchema],
                     max_tokens: int, model: str, *, stream: bool = False) -> Any:
        provider, model_name = model.split(":", 1) if ":" in model else ("", "")
        if not provider or not model_name:
            raise ValueError("Model target must use provider:model syntax")
        provider, config = self._provider_config(provider)
        client = self.client_factory({provider: config})
        kwargs: dict[str, Any] = {"model": f"{provider}:{model_name}", "messages": messages,
                                  "max_tokens": max_tokens, "temperature": 0.2}
        if stream:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
        if attached_tool:
            kwargs["tools"] = [{"type": "function", "function": attached_tool.model_dump()}]
            kwargs["tool_choice"] = "auto"
        return client.chat.completions.create(**kwargs)

    def complete_turn(self, messages: list[dict[str, Any]], attached_tool: Optional[ToolSchema],
                      max_tokens: int, model: str,
                      allowance: Optional[dict[str, Any]] = None,
                      on_text_delta: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
        allowance = allowance or {}
        self._ensure_priced(model, allowance)
        retrying = Retrying(
            retry=retry_if_exception(is_transient_provider_error) if on_text_delta is None else retry_if_exception(
                lambda error: is_transient_provider_error(error) and not streamed_text
            ),
            wait=wait_random_exponential(multiplier=0.01, max=0.05),
            stop=stop_after_attempt(self.max_attempts), reraise=True,
        )
        streamed_text = False

        def emit_text_delta(text: str) -> None:
            nonlocal streamed_text
            streamed_text = streamed_text or bool(text)
            if on_text_delta:
                on_text_delta(text)

        for attempt in retrying:
            with attempt:
                if on_text_delta:
                    response = self._one_attempt(messages, attached_tool, max_tokens, model, stream=True)
                    content_parts: list[str] = []
                    tool_calls_by_index: dict[int, dict[str, Any]] = {}
                    usage = None
                    for chunk in response:
                        chunk_data = self._plain(chunk)
                        choices = chunk_data.get("choices", []) if isinstance(chunk_data, dict) else []
                        if not choices:
                            usage = chunk_data.get("usage", usage)
                            continue
                        delta = choices[0].get("delta") or {}
                        content = delta.get("content")
                        if content:
                            content_parts.append(content)
                            emit_text_delta(content)
                        for partial in delta.get("tool_calls") or []:
                            index = int(partial.get("index", 0))
                            saved = tool_calls_by_index.setdefault(index, {
                                "id": "", "type": "function", "function": {"name": "", "arguments": ""}
                            })
                            if partial.get("id"):
                                saved["id"] = partial["id"]
                            function = partial.get("function") or {}
                            saved["function"]["name"] += function.get("name") or ""
                            saved["function"]["arguments"] += function.get("arguments") or ""
                        if chunk_data.get("usage") is not None:
                            usage = chunk_data["usage"]
                    response = {"choices": [{"message": {
                        "content": "".join(content_parts),
                        "tool_calls": [tool_calls_by_index[index] for index in sorted(tool_calls_by_index)],
                    }}], "usage": usage}
                else:
                    response = self._one_attempt(messages, attached_tool, max_tokens, model)
        if isinstance(response, dict):
            choice = response["choices"][0]
            message = self._plain(choice.get("message") if isinstance(choice, dict) else choice.message)
        else:
            choice = response.choices[0]
            message = self._plain(choice.message)
        tool_calls = message.get("tool_calls") or []
        usage = getattr(response, "usage", None)
        if usage is None and isinstance(response, dict):
            usage = response.get("usage")
        usage = self._plain(usage) if usage is not None else {}
        if not isinstance(usage, dict):
            usage = {}
        input_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
        output_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
        total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens) or 0)
        if input_tokens < 0 or output_tokens < 0 or total_tokens < 0:
            raise ValueError("Provider returned invalid token usage")
        if total_tokens == 0:
            raise ValueError("Provider did not return billable token usage")
        cost_usd = self._estimate_cost(model, allowance, input_tokens, output_tokens)
        return {"content": message.get("content") or "", "tool_calls": tool_calls,
                "total_tokens": total_tokens, "input_tokens": input_tokens,
                "output_tokens": output_tokens, "cost_usd": cost_usd}

    def complete_for_ticket(self, messages: list[dict[str, Any]], attached_tool: Optional[ToolSchema],
                            max_tokens: int, ticket: Ticket,
                            on_text_delta: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
        primary = ticket.assigned_model
        candidates = [primary, *ticket.gateway_allowance.get("fallback_models", [])]
        failures: list[str] = []
        streamed_text = False

        def forward_delta(text: str) -> None:
            nonlocal streamed_text
            streamed_text = streamed_text or bool(text)
            if on_text_delta:
                on_text_delta(text)

        for index, model in enumerate(candidates):
            allowance = dict(ticket.gateway_allowance)
            if model != primary:
                allowance.update(ticket.gateway_allowance.get("fallback_pricing", {}).get(model, {}))
            try:
                result = self.complete_turn(messages, attached_tool, max_tokens, model, allowance,
                                            on_text_delta=forward_delta if on_text_delta else None)
                result["selected_model"] = model
                if index:
                    result["fallback_from"] = primary
                return result
            except UnpricedModelError:
                raise
            except Exception as exc:
                if streamed_text:
                    raise
                if not is_transient_provider_error(exc) or index == len(candidates) - 1:
                    raise
                failures.append(f"{model}: {type(exc).__name__}")
        raise RuntimeError("Provider fallback chain exhausted: " + "; ".join(failures))
