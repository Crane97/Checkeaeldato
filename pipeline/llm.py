"""Llamadas a Claude con salida JSON validada por pydantic y registro de coste."""

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from pydantic import BaseModel, ValidationError

from pipeline.config import Settings
from pipeline.costs import llm_cost
from pipeline.models import CostEntry

if TYPE_CHECKING:
    import anthropic

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# The SDK already retries 408/409/429/5xx and connection errors with backoff.
API_MAX_RETRIES = 4


class LLMError(RuntimeError):
    """A Claude call did not produce a usable answer."""


class LLMRefusal(LLMError):
    """Claude declined the request (stop_reason "refusal")."""


class LLMTruncated(LLMError):
    """The answer hit max_tokens and is incomplete."""


class LLMConfigError(LLMError):
    """Invalid credentials or permissions: every call will fail, so the job must stop."""


Effort = Literal["low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True)
class ThinkingConfig:
    """Claude's internal thinking for one step (only the verdict uses it).

    `enabled=False` asks for the least thinking the model allows. On Claude 4.6+ models
    thinking is adaptive and steered with `effort`; a fixed `budget_tokens` is only accepted
    by models up to Claude 4.5 (platform.claude.com/docs/en/build-with-claude/extended-thinking,
    checked 2026-10-13)."""

    enabled: bool
    effort: Effort | None = None
    budget_tokens: int | None = None

    def label(self) -> str:
        if not self.enabled:
            return "off"
        return f"on:{self.effort or 'default'}:{self.budget_tokens or '-'}"


# Model id prefix -> (thinking when enabled, thinking when disabled; None = send nothing).
_ADAPTIVE: dict[str, Any] = {"type": "adaptive", "display": "summarized"}
_THINKING_MODES: tuple[tuple[str, dict[str, Any] | str, dict[str, Any] | None], ...] = (
    # Thinking on by default; Sonnet 5.5 rejects "disabled", its lowest is "between_tools".
    ("claude-sonnet-5-5", _ADAPTIVE, {"type": "between_tools"}),
    # Opus 5.5 cannot turn thinking off at any effort level.
    ("claude-opus-5-5", _ADAPTIVE, None),
    ("claude-haiku-5-5", _ADAPTIVE, {"type": "disabled"}),
    ("claude-sonnet-5", _ADAPTIVE, {"type": "disabled"}),
    ("claude-opus-5", _ADAPTIVE, {"type": "disabled"}),
    ("claude-fable-5", _ADAPTIVE, None),
    # Adaptive but off until requested.
    ("claude-sonnet-4-6", _ADAPTIVE, None),
    ("claude-opus-4-", _ADAPTIVE, None),
    # Manual budget only (Claude 4.5 and earlier).
    ("claude-haiku-4-5", "manual", None),
    ("claude-sonnet-4-5", "manual", None),
)
MIN_THINKING_BUDGET = 1024


def thinking_params(model: str, config: ThinkingConfig) -> dict[str, Any]:
    """Request fields (`thinking`, `output_config.effort`) for `config` on `model`."""
    on, off = next(
        ((on, off) for prefix, on, off in _THINKING_MODES if model.startswith(prefix)),
        (_ADAPTIVE, None),
    )
    params: dict[str, Any] = {}
    if not config.enabled:
        if off is None and model.startswith("claude-opus-5-5"):
            logger.warning("%s no permite desactivar el razonamiento interno", model)
        if off is not None:
            params["thinking"] = off
        return params
    if on == "manual":
        budget = max(config.budget_tokens or MIN_THINKING_BUDGET * 4, MIN_THINKING_BUDGET)
        params["thinking"] = {"type": "enabled", "budget_tokens": budget}
    else:
        if config.budget_tokens:
            logger.warning(
                "%s no admite presupuesto fijo de tokens (budget_tokens): se usa el esfuerzo", model
            )
        params["thinking"] = dict(on)
    if config.effort:
        params["effort"] = config.effort
    return params


def make_anthropic_client(settings: Settings) -> "anthropic.Anthropic":
    import anthropic  # deferred: importing the SDK takes seconds on slow machines

    # Without an explicit key the SDK resolves credentials itself (env, `ant auth login`).
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    return anthropic.Anthropic(api_key=key, max_retries=API_MAX_RETRIES)


class LLMClient:
    """Structured Claude calls for pipeline steps.

    Every API call appends its cost to an internal log, including calls whose answer
    is then rejected, so a failing step never hides what it spent. Steps collect the
    log with `take_costs()`.
    """

    def __init__(self, client: Any, prompts_dir: Path, validation_retries: int = 1) -> None:
        self._client = client
        self._prompts_dir = prompts_dir
        self._validation_retries = validation_retries
        self._costs: list[CostEntry] = []
        # Summary of Claude's thinking in the last call (API, display "summarized").
        self.last_thinking: str | None = None

    def take_costs(self) -> list[CostEntry]:
        costs, self._costs = self._costs, []
        return costs

    def _load_prompt(self, name: str) -> str:
        path = self._prompts_dir / name
        if not path.is_file():
            raise LLMError(f"No existe el prompt {path}")
        return path.read_text(encoding="utf-8")

    def structured(
        self,
        *,
        step: str,
        model: str,
        prompt: str,
        user_content: str,
        output_type: type[T],
        max_tokens: int = 16000,
        effort: str | None = None,
        thinking: ThinkingConfig | None = None,
    ) -> T:
        """Ask Claude for an `output_type` instance.

        `prompt` is a file in prompts/ sent as a cached system prompt; per-call data
        goes in `user_content` so the cache prefix stays stable. With `thinking`, the
        summary of Claude's thinking is left in `last_thinking` for auditing.
        """
        from anthropic import transform_schema  # deferred, see make_anthropic_client

        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": transform_schema(output_type)}
        }
        extra = thinking_params(model, thinking) if thinking is not None else {}
        if "effort" in extra:
            effort = extra.pop("effort")
        if effort is not None:
            output_config["effort"] = effort
        budget = extra.get("thinking", {}).get("budget_tokens")
        if budget and max_tokens <= budget:
            max_tokens = budget + 8000  # room for the answer after thinking
        request = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": self._load_prompt(prompt),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user_content}],
            "output_config": output_config,
            **extra,
        }
        self.last_thinking = None

        for attempt in range(self._validation_retries + 1):
            started = time.monotonic()
            response = self._create(request)
            self._record_cost(step, model, response, time.monotonic() - started)
            text = self._checked_text(response, prompt)
            summaries = [
                b.thinking for b in response.content if b.type == "thinking" and b.thinking
            ]
            self.last_thinking = "\n\n".join(summaries) or None
            try:
                return output_type.model_validate_json(text)
            except ValidationError as exc:
                if attempt == self._validation_retries:
                    raise LLMError(f"Respuesta inválida de {model} para {prompt}: {exc}") from exc
                logger.warning(
                    "Respuesta inválida para %s (intento %d); se reintenta: %s",
                    prompt,
                    attempt + 1,
                    exc,
                )
        raise AssertionError("unreachable")

    def _create(self, request: dict[str, Any]) -> Any:
        """Call the API, mapping SDK errors (already retried by the SDK) to LLMError."""
        import anthropic

        try:
            return self._client.messages.create(**request)
        except (
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.NotFoundError,
        ) as exc:
            raise LLMConfigError(
                f"Credenciales o modelo de Anthropic inválidos ({exc.status_code}); "
                "revisa ANTHROPIC_API_KEY y ANTHROPIC_MODEL"
            ) from exc
        except anthropic.BadRequestError as exc:
            if "credit balance" in str(exc).lower():
                raise LLMConfigError(
                    "El saldo de créditos de la cuenta de Anthropic está agotado"
                ) from exc
            raise LLMError(f"Error de la API de Anthropic: {exc}") from exc
        except anthropic.APIError as exc:
            raise LLMError(f"Error de la API de Anthropic: {exc}") from exc
        except TypeError as exc:
            # The SDK raises TypeError (not an APIError) when it finds no credentials.
            if "authentication method" not in str(exc):
                raise
            raise LLMConfigError(
                "No hay credenciales de Anthropic: define ANTHROPIC_API_KEY en .env, o usa "
                "LLM_BACKEND=claude-code para probar en local con tu suscripción de Claude"
            ) from exc

    def _record_cost(self, step: str, model: str, response: Any, seconds: float) -> None:
        usage = response.usage
        details = getattr(usage, "output_tokens_details", None)
        entry = llm_cost(
            step,
            model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_write_tokens=usage.cache_creation_input_tokens or 0,
            cache_read_tokens=usage.cache_read_input_tokens or 0,
        )
        self._costs.append(
            entry.model_copy(
                update={
                    "seconds": round(seconds, 3),
                    "thinking_tokens": int(getattr(details, "thinking_tokens", 0) or 0),
                }
            )
        )

    @staticmethod
    def _checked_text(response: Any, prompt: str) -> str:
        request_id = getattr(response, "_request_id", None)
        if response.stop_reason == "refusal":
            raise LLMRefusal(f"Claude rechazó la petición ({prompt}, request {request_id})")
        if response.stop_reason == "max_tokens":
            raise LLMTruncated(f"Respuesta cortada por max_tokens ({prompt}, request {request_id})")
        if response.stop_reason != "end_turn":
            raise LLMError(
                f"stop_reason inesperado: {response.stop_reason} ({prompt}, request {request_id})"
            )
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMError(f"Respuesta sin texto ({prompt}, request {request_id})")
        return text
