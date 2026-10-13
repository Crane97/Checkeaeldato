import datetime as dt
from typing import Any

import pytest
from pydantic import ValidationError

from pipeline.llm import LLMError
from pipeline.models import (
    ClaimEvidence,
    DetectedClaim,
    EvidenceItem,
    PromiseVerdict,
    Source,
)
from pipeline.promises import (
    NO_EVIDENCE,
    REASONING_PREFIX,
    PromiseAnswer,
    assess_promises,
    format_promise,
)

DATE = dt.date(2026, 11, 20)
OFFICIAL = Source(url="https://www.ine.es/epa", title="EPA", organization="INE", kind="oficial")
WEB = Source(url="https://example.org/paro", title="Paro", organization="example", kind="web")


def promise(id: str = "c001", **kwargs: Any) -> DetectedClaim:
    base = {
        "id": id,
        "speaker": "speaker_1",
        "kind": "promesa",
        "text_original": "bajaremos el paro al 0 %",
        "text_normalized": "Se promete reducir la tasa de paro de España al 0 %.",
        "claim_type": "estadistica",
    }
    return DetectedClaim(**{**base, **kwargs})


def evidence(claim_id: str = "c001", *sources: Source) -> ClaimEvidence:
    items = [
        EvidenceItem(id=f"E{n}", source=s, excerpt="La tasa de paro mínima fue del 7,9 % en 2007.")
        for n, s in enumerate(sources or (OFFICIAL,), 1)
    ]
    return ClaimEvidence(claim_id=claim_id, items=items)


def answer(**kwargs: Any) -> PromiseAnswer:
    base = {
        "assessment": "inviable",
        "confidence": 0.85,
        "explanation": "La tasa de paro de España nunca ha bajado del 7,9 % (EPA).",
        "obstacles": ["datos"],
        "consequences_sourced": "",
        "consequences_reasoning": "Un paro del 0 % no existe en ninguna economía.",
        "cited_evidence": ["E1"],
    }
    return PromiseAnswer(**{**base, **kwargs})


class FakeLLM:
    def __init__(self, *answers: PromiseAnswer | Exception) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def structured(self, *, user_content: str, **kwargs: Any) -> PromiseAnswer:
        self.prompts.append(user_content)
        result = self.answers.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def assess(claims, evidences, llm):
    return assess_promises(claims, evidences, speech_date=DATE, llm=llm, model="m")


def test_promise_is_assessed_with_its_sources():
    [result] = assess([promise()], [evidence()], FakeLLM(answer()))
    assert result.assessment == "inviable"
    assert result.sources == [OFFICIAL]
    assert result.basis == "oficial"
    assert result.consequences_reasoning.startswith(REASONING_PREFIX)


def test_without_evidence_it_is_not_evaluable_and_the_model_is_not_called():
    llm = FakeLLM()
    [result] = assess([promise()], [ClaimEvidence(claim_id="c001")], llm)
    assert result.assessment == "no_evaluable"
    assert result.explanation == NO_EVIDENCE
    assert llm.prompts == []


def test_assessment_without_valid_citation_becomes_not_evaluable():
    [result] = assess([promise()], [evidence()], FakeLLM(answer(cited_evidence=["E9"])))
    assert result.assessment == "no_evaluable"
    assert result.sources == []


def test_web_only_assessment_has_limited_confidence():
    [result] = assess([promise()], [evidence("c001", WEB)], FakeLLM(answer()))
    assert result.confidence == 0.6


def test_empty_reasoning_stays_empty():
    [result] = assess([promise()], [evidence()], FakeLLM(answer(consequences_reasoning=" ")))
    assert result.consequences_reasoning == ""


def test_prompt_has_date_promise_and_evidence_but_not_the_speaker():
    llm = FakeLLM(answer())
    assess([promise()], [evidence()], llm)
    text = llm.prompts[0]
    assert "2026-11-20" in text and "0 %" in text and 'id="E1"' in text
    assert "speaker_1" not in text
    assert "<promesa>" in format_promise(promise(), evidence(), DATE)


def test_one_failure_is_recorded_and_others_continue():
    claims = [promise("c001"), promise("c002")]
    llm = FakeLLM(LLMError("límite"), answer())
    first, second = assess(claims, [evidence("c001"), evidence("c002")], llm)
    assert first.analysis_error and first.assessment == "no_evaluable"
    assert second.assessment == "inviable"


def test_every_failure_stops_the_step():
    with pytest.raises(RuntimeError):
        assess([promise()], [evidence()], FakeLLM(LLMError("límite")))


def test_verdict_other_than_not_evaluable_needs_sources():
    with pytest.raises(ValidationError):
        PromiseVerdict(
            claim_id="c001", assessment="viable", confidence=0.8, explanation="Cabe en la ley."
        )


def test_forbidden_wording_is_rejected():
    with pytest.raises(ValidationError):
        answer(consequences_reasoning="El orador miente.")
