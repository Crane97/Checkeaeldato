import json
from pathlib import Path
from typing import Any

import pytest
from anthropic.types import Message
from pydantic import BaseModel, Field

from pipeline.llm import LLMClient, LLMError, LLMRefusal, LLMTruncated, ThinkingConfig

MODEL = "claude-haiku-5-5"


class Answer(BaseModel):
    label: str
    confidence: float = Field(ge=0, le=1)


def message(
    text: str | None,
    stop_reason: str = "end_turn",
    thinking: bool = False,
    input_tokens: int = 1000,
    output_tokens: int = 100,
    cache_write: int = 0,
    cache_read: int = 0,
) -> Message:
    content: list[dict[str, Any]] = []
    if thinking:
        content.append({"type": "thinking", "thinking": "", "signature": "sig"})
    if text is not None:
        content.append({"type": "text", "text": text})
    return Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_write,
                "cache_read_input_tokens": cache_read,
            },
        }
    )


class FakeMessages:
    def __init__(self, responses: list[Message]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Message:
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeAnthropic:
    def __init__(self, *responses: Message) -> None:
        self.messages = FakeMessages(list(responses))


@pytest.fixture
def prompts_dir(tmp_path: Path) -> Path:
    (tmp_path / "clasificar.md").write_text("Eres un clasificador neutral.", encoding="utf-8")
    return tmp_path


def call(llm: LLMClient, **overrides: Any) -> Answer:
    kwargs = {
        "step": "claims",
        "model": MODEL,
        "prompt": "clasificar.md",
        "user_content": "<texto>hola</texto>",
        "output_type": Answer,
    }
    return llm.structured(**kwargs | overrides)


VALID = json.dumps({"label": "a", "confidence": 0.7})


def test_returns_validated_output_and_sends_cached_system_prompt(prompts_dir):
    fake = FakeAnthropic(message(VALID, cache_write=800, cache_read=200))
    llm = LLMClient(fake, prompts_dir)

    assert call(llm) == Answer(label="a", confidence=0.7)

    sent = fake.messages.calls[0]
    assert sent["model"] == MODEL
    assert sent["system"][0]["text"] == "Eres un clasificador neutral."
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert sent["messages"] == [{"role": "user", "content": "<texto>hola</texto>"}]
    schema = sent["output_config"]["format"]["schema"]
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert schema["additionalProperties"] is False
    assert "effort" not in sent["output_config"]

    [cost] = llm.take_costs()
    assert (cost.step, cost.model, cost.input_tokens, cost.output_tokens) == (
        "claims",
        MODEL,
        1000,
        100,
    )
    assert (cost.cache_write_tokens, cost.cache_read_tokens) == (800, 200)
    assert cost.usd > 0
    assert llm.take_costs() == []


def test_effort_is_forwarded(prompts_dir):
    fake = FakeAnthropic(message(VALID))
    call(LLMClient(fake, prompts_dir), effort="low")
    assert fake.messages.calls[0]["output_config"]["effort"] == "low"


def test_text_after_thinking_block_is_used(prompts_dir):
    llm = LLMClient(FakeAnthropic(message(VALID, thinking=True)), prompts_dir)
    assert call(llm).label == "a"


def test_refusal_raises_and_still_records_cost(prompts_dir):
    llm = LLMClient(FakeAnthropic(message(None, stop_reason="refusal")), prompts_dir)
    with pytest.raises(LLMRefusal):
        call(llm)
    assert len(llm.take_costs()) == 1


def test_truncated_answer_raises_and_still_records_cost(prompts_dir):
    llm = LLMClient(FakeAnthropic(message('{"label": "a"', stop_reason="max_tokens")), prompts_dir)
    with pytest.raises(LLMTruncated):
        call(llm)
    assert len(llm.take_costs()) == 1


def test_invalid_output_is_retried_once(prompts_dir):
    bad = json.dumps({"label": "a", "confidence": 1.5})  # violates le=1, checked client-side
    fake = FakeAnthropic(message(bad), message(VALID))
    llm = LLMClient(fake, prompts_dir)

    assert call(llm).confidence == 0.7
    assert len(fake.messages.calls) == 2
    assert len(llm.take_costs()) == 2


def test_invalid_output_twice_raises_with_both_costs(prompts_dir):
    bad = json.dumps({"label": "a", "confidence": 1.5})
    llm = LLMClient(FakeAnthropic(message(bad), message(bad)), prompts_dir)
    with pytest.raises(LLMError, match="confidence"):
        call(llm)
    assert len(llm.take_costs()) == 2


def test_missing_text_block_raises(prompts_dir):
    llm = LLMClient(FakeAnthropic(message(None)), prompts_dir)
    with pytest.raises(LLMError, match="sin texto"):
        call(llm)


def test_missing_prompt_file_raises(prompts_dir):
    llm = LLMClient(FakeAnthropic(), prompts_dir)
    with pytest.raises(LLMError, match="no_existe.md"):
        call(llm, prompt="no_existe.md")


def _api_error(cls_name: str, status: int):
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request)
    return getattr(anthropic, cls_name)("boom", response=response, body=None)


class RaisingMessages:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def create(self, **kwargs: Any) -> Message:
        raise self.exc


class RaisingAnthropic:
    def __init__(self, exc: Exception) -> None:
        self.messages = RaisingMessages(exc)


@pytest.mark.parametrize(
    ("cls_name", "status"),
    [("AuthenticationError", 401), ("PermissionDeniedError", 403), ("NotFoundError", 404)],
)
def test_auth_errors_become_config_errors(prompts_dir, cls_name, status):
    from pipeline.llm import LLMConfigError

    llm = LLMClient(RaisingAnthropic(_api_error(cls_name, status)), prompts_dir)
    with pytest.raises(LLMConfigError, match="ANTHROPIC_API_KEY"):
        call(llm)


def test_other_api_errors_become_llm_errors(prompts_dir):
    from pipeline.llm import LLMConfigError

    llm = LLMClient(RaisingAnthropic(_api_error("InternalServerError", 500)), prompts_dir)
    with pytest.raises(LLMError) as info:
        call(llm)
    assert not isinstance(info.value, LLMConfigError)


@pytest.mark.parametrize("stop_reason", ["pause_turn", "tool_use", "stop_sequence"])
def test_unexpected_stop_reasons_are_errors(prompts_dir, stop_reason):
    llm = LLMClient(FakeAnthropic(message(VALID, stop_reason=stop_reason)), prompts_dir)
    with pytest.raises(LLMError, match=stop_reason):
        call(llm)
    assert len(llm.take_costs()) == 1


def test_low_credit_balance_is_a_config_error(prompts_dir):
    import anthropic
    import httpx2

    from pipeline.llm import LLMConfigError

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    exc = anthropic.BadRequestError(
        "Your credit balance is too low to access the Anthropic API.",
        response=httpx2.Response(400, request=request),
        body=None,
    )
    with pytest.raises(LLMConfigError, match="saldo"):
        call(LLMClient(RaisingAnthropic(exc), prompts_dir))


def test_missing_credentials_are_a_config_error(prompts_dir):
    from pipeline.llm import LLMConfigError

    exc = TypeError('"Could not resolve authentication method. Expected one of api_key, ..."')
    with pytest.raises(LLMConfigError, match="LLM_BACKEND=claude-code"):
        call(LLMClient(RaisingAnthropic(exc), prompts_dir))


def test_other_type_errors_are_not_hidden(prompts_dir):
    with pytest.raises(TypeError, match="otra cosa"):
        call(LLMClient(RaisingAnthropic(TypeError("otra cosa")), prompts_dir))


def thinking_message(summary: str) -> Message:
    msg = message(VALID)
    block = {"type": "thinking", "thinking": summary, "signature": "sig"}
    return Message.model_validate(
        msg.model_dump() | {"content": [block, *msg.model_dump()["content"]]}
    )


@pytest.mark.parametrize(
    ("model", "config", "expected"),
    [
        # Claude 5.5: thinking is adaptive and on by default; "off" is the lowest setting.
        (
            "claude-sonnet-5-5",
            ThinkingConfig(True, "high"),
            {"type": "adaptive", "display": "summarized"},
        ),
        ("claude-sonnet-5-5", ThinkingConfig(False), {"type": "between_tools"}),
        ("claude-haiku-5-5", ThinkingConfig(False), {"type": "disabled"}),
        ("claude-opus-5-5", ThinkingConfig(False), None),  # cannot be turned off
        # Manual budget only on models that still accept it.
        (
            "claude-haiku-4-5",
            ThinkingConfig(True, budget_tokens=4000),
            {"type": "enabled", "budget_tokens": 4000},
        ),
    ],
)
def test_thinking_request_per_model(prompts_dir, model, config, expected):
    fake = FakeAnthropic(message(VALID))
    call(LLMClient(fake, prompts_dir), model=model, thinking=config)
    sent = fake.messages.calls[0]
    assert sent.get("thinking") == expected
    if config.enabled and config.effort:
        assert sent["output_config"]["effort"] == config.effort
    if expected and expected.get("budget_tokens"):
        assert sent["max_tokens"] > expected["budget_tokens"]


def test_without_thinking_config_the_model_default_is_kept(prompts_dir):
    fake = FakeAnthropic(message(VALID))
    call(LLMClient(fake, prompts_dir))
    assert "thinking" not in fake.messages.calls[0]


def test_thinking_summary_and_duration_are_kept(prompts_dir):
    llm = LLMClient(FakeAnthropic(thinking_message("Comparé 344 con 344,10.")), prompts_dir)
    call(llm, model="claude-sonnet-5-5", thinking=ThinkingConfig(True))
    assert llm.last_thinking == "Comparé 344 con 344,10."
    [cost] = llm.take_costs()
    assert cost.seconds >= 0
