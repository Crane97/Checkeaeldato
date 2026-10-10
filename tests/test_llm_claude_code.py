import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from pipeline import llm_claude_code
from pipeline.llm import LLMConfigError, LLMError
from pipeline.llm_claude_code import ClaudeCodeLLM

MODEL = "claude-haiku-5-5"


class Answer(BaseModel):
    label: str
    confidence: float = Field(ge=0, le=1)


def result(structured: Any = None, *, is_error: bool = False, subtype: str = "success", **extra):
    data = {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "stop_reason": "tool_use",
        "total_cost_usd": 0.0004,
        "usage": {
            "input_tokens": 2,
            "output_tokens": 266,
            "cache_creation_input_tokens": 1195,
            "cache_read_input_tokens": 10,
        },
        "result": json.dumps(structured) if structured is not None else "",
    }
    if structured is not None:
        data["structured_output"] = structured
    return json.dumps(data | extra)


class FakeRun:
    def __init__(self, *outputs: tuple[int, str, str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, **kwargs})
        code, stdout, stderr = self.outputs.pop(0)
        return subprocess.CompletedProcess(cmd, code, stdout, stderr)


@pytest.fixture
def prompts_dir(tmp_path: Path) -> Path:
    (tmp_path / "clasificar.md").write_text("Eres un clasificador neutral.")
    return tmp_path


@pytest.fixture
def fake(monkeypatch):
    def install(*outputs):
        runner = FakeRun(*outputs)
        monkeypatch.setattr(llm_claude_code.subprocess, "run", runner)
        monkeypatch.setattr(llm_claude_code.shutil, "which", lambda name: "/usr/local/bin/claude")
        return runner

    return install


def call(llm: ClaudeCodeLLM) -> Answer:
    return llm.structured(
        step="claims",
        model=MODEL,
        prompt="clasificar.md",
        user_content="<texto>hola</texto>",
        output_type=Answer,
    )


GOOD = {"label": "a", "confidence": 0.7}


def test_runs_headless_claude_isolated_and_records_cost(fake, prompts_dir, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    runner = fake((0, result(GOOD), ""))
    llm = ClaudeCodeLLM(prompts_dir)

    assert call(llm) == Answer(label="a", confidence=0.7)

    [run] = runner.calls
    cmd = run["cmd"]
    assert cmd[:2] == ["/usr/local/bin/claude", "-p"]
    assert cmd[cmd.index("--model") + 1] == MODEL
    assert cmd[cmd.index("--system-prompt") + 1] == "Eres un clasificador neutral."
    assert json.loads(cmd[cmd.index("--json-schema") + 1])["additionalProperties"] is False
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    for flag in ("--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"):
        assert flag in cmd
    assert run["input"] == "<texto>hola</texto>"
    assert "ANTHROPIC_API_KEY" not in run["env"]  # always the subscription, never the API key
    assert Path(run["cwd"]) != Path.cwd()  # no project CLAUDE.md in the model's context

    [cost] = llm.take_costs()
    assert (cost.provider, cost.model, cost.step) == ("claude-code", MODEL, "claims")
    assert (cost.input_tokens, cost.output_tokens) == (2, 266)
    assert (cost.cache_write_tokens, cost.cache_read_tokens) == (1195, 10)
    assert cost.usd == pytest.approx(0.0004)


def test_invalid_output_is_retried_once(fake, prompts_dir):
    runner = fake((0, result({"label": "a", "confidence": 3}), ""), (0, result(GOOD), ""))
    llm = ClaudeCodeLLM(prompts_dir)
    assert call(llm).confidence == 0.7
    assert len(runner.calls) == 2
    assert len(llm.take_costs()) == 2


def test_invalid_output_twice_raises(fake, prompts_dir):
    bad = result({"label": "a", "confidence": 3})
    llm = ClaudeCodeLLM(prompts_dir)
    fake((0, bad, ""), (0, bad, ""))
    with pytest.raises(LLMError, match="confidence"):
        call(llm)
    assert len(llm.take_costs()) == 2


@pytest.mark.parametrize(
    "output",
    [
        (0, result(None, is_error=True, subtype="error_max_turns"), ""),
        (0, result(None), ""),  # success but no structured output
        (0, "no es json", ""),
        (1, "", "Something broke"),
    ],
)
def test_failures_are_llm_errors(fake, prompts_dir, output):
    fake(output)
    with pytest.raises(LLMError) as info:
        call(ClaudeCodeLLM(prompts_dir))
    assert not isinstance(info.value, LLMConfigError)


@pytest.mark.parametrize(
    "output",
    [
        (1, "", "Not logged in · Please run /login"),
        (0, result(None, is_error=True, result="Claude usage limit reached. Resets at 8pm"), ""),
    ],
)
def test_login_and_usage_limit_stop_the_job(fake, prompts_dir, output):
    fake(output)
    with pytest.raises(LLMConfigError):
        call(ClaudeCodeLLM(prompts_dir))


def test_missing_claude_binary_is_a_config_error(monkeypatch, prompts_dir):
    monkeypatch.setattr(llm_claude_code.shutil, "which", lambda name: None)
    with pytest.raises(LLMConfigError, match="claude"):
        call(ClaudeCodeLLM(prompts_dir))


def test_timeout_is_an_llm_error(fake, prompts_dir, monkeypatch):
    def slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    fake()
    monkeypatch.setattr(llm_claude_code.subprocess, "run", slow)
    with pytest.raises(LLMError, match="tiempo"):
        call(ClaudeCodeLLM(prompts_dir))
