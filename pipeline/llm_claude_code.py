"""Claude vía Claude Code en modo no interactivo (`claude -p`), con la suscripción local.

Solo para pruebas en local: usa el plan de quien ejecuta el pipeline. La beta pública
debe usar la API (LLM_BACKEND=api). Misma interfaz que LLMClient.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from pipeline.llm import LLMConfigError, LLMError, ThinkingConfig
from pipeline.models import CostEntry

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

TIMEOUT_SECONDS = 600
# Failures that will repeat on every call, so the job must stop.
_FATAL_RE = re.compile(
    r"not logged in|/login|usage limit|session limit|rate limit|invalid api key", re.IGNORECASE
)


class ClaudeCodeLLM:
    def __init__(self, prompts_dir: Path, validation_retries: int = 1) -> None:
        self._prompts_dir = prompts_dir
        self._validation_retries = validation_retries
        self._costs: list[CostEntry] = []
        self.last_thinking: str | None = None  # `claude -p` does not return thinking
        # Empty working directory: no project CLAUDE.md or settings reach the model.
        self._workdir = tempfile.TemporaryDirectory(prefix="verificar-claude-")

    def take_costs(self) -> list[CostEntry]:
        costs, self._costs = self._costs, []
        return costs

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
        from anthropic import transform_schema  # same schema rules as the API backend

        path = self._prompts_dir / prompt
        if not path.is_file():
            raise LLMError(f"No existe el prompt {path}")
        claude = shutil.which("claude")
        if claude is None:
            raise LLMConfigError(
                "No se encuentra el comando `claude` (Claude Code); instálalo o usa LLM_BACKEND=api"
            )
        cmd = [
            claude,
            "-p",
            "--model",
            model,
            "--system-prompt",
            path.read_text(),
            "--json-schema",
            json.dumps(transform_schema(output_type)),
            "--output-format",
            "json",
            "--tools",
            "",
            "--setting-sources",
            "",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--disable-slash-commands",
        ]
        # `claude -p` has no thinking switch: thinking depth follows the effort level.
        if thinking is not None:
            level = (thinking.effort or "high") if thinking.enabled else "low"
            cmd += ["--effort", level]

        for attempt in range(self._validation_retries + 1):
            started = time.monotonic()
            data = self._run(cmd, user_content, prompt)
            self._record_cost(step, model, data, time.monotonic() - started)
            if not isinstance(data.get("structured_output"), dict):
                raise LLMError(f"Claude Code no devolvió salida estructurada para {prompt}")
            try:
                return output_type.model_validate(data["structured_output"])
            except ValidationError as exc:
                if attempt == self._validation_retries:
                    raise LLMError(f"Respuesta inválida de {model} para {prompt}: {exc}") from exc
                logger.warning("Respuesta inválida para %s; se reintenta: %s", prompt, exc)
        raise AssertionError("unreachable")

    def _run(self, cmd: list[str], user_content: str, prompt: str) -> dict[str, Any]:
        # Never the API key: this backend exists to use the local subscription.
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        try:
            proc = subprocess.run(
                cmd,
                input=user_content,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
                cwd=self._workdir.name,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"Claude Code superó el tiempo máximo ({prompt})") from exc

        output = f"{proc.stdout}\n{proc.stderr}"
        if _FATAL_RE.search(output):
            raise LLMConfigError(
                "Claude Code no puede responder (sesión no iniciada o límite de uso del plan): "
                f"{output.strip()[:300]}"
            )
        if proc.returncode != 0:
            raise LLMError(
                f"Claude Code terminó con código {proc.returncode}: {output.strip()[:300]}"
            )
        try:
            data = json.loads(proc.stdout)
        except ValueError as exc:
            raise LLMError(f"Salida de Claude Code no es JSON ({prompt})") from exc
        if not isinstance(data, dict) or data.get("is_error") or data.get("subtype") != "success":
            detail = data.get("subtype") if isinstance(data, dict) else "desconocido"
            raise LLMError(f"Claude Code devolvió un error ({detail}) para {prompt}")
        return data

    def _record_cost(self, step: str, model: str, data: dict[str, Any], seconds: float) -> None:
        usage = data.get("usage") or {}
        self._costs.append(
            CostEntry(
                step=step,
                provider="claude-code",
                model=model,
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
                cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
                # List-price equivalent reported by Claude Code; covered by the plan.
                usd=float(data.get("total_cost_usd") or 0),
                seconds=round(seconds, 3),
            )
        )
