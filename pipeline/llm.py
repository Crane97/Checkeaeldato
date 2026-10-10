"""Llamadas a Claude con salida JSON validada por pydantic y registro de coste."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

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

    def take_costs(self) -> list[CostEntry]:
        costs, self._costs = self._costs, []
        return costs

    def _load_prompt(self, name: str) -> str:
        path = self._prompts_dir / name
        if not path.is_file():
            raise LLMError(f"No existe el prompt {path}")
        return path.read_text()

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
    ) -> T:
        """Ask Claude for an `output_type` instance.

        `prompt` is a file in prompts/ sent as a cached system prompt; per-call data
        goes in `user_content` so the cache prefix stays stable.
        """
        from anthropic import transform_schema  # deferred, see make_anthropic_client

        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": transform_schema(output_type)}
        }
        if effort is not None:
            output_config["effort"] = effort
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
        }

        for attempt in range(self._validation_retries + 1):
            response = self._create(request)
            self._record_cost(step, model, response)
            text = self._checked_text(response, prompt)
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

    def _record_cost(self, step: str, model: str, response: Any) -> None:
        usage = response.usage
        self._costs.append(
            llm_cost(
                step,
                model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_write_tokens=usage.cache_creation_input_tokens or 0,
                cache_read_tokens=usage.cache_read_input_tokens or 0,
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
