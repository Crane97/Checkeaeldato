import datetime as dt
from typing import Any

import pytest
from pydantic import ValidationError

from pipeline.llm import LLMConfigError, LLMError
from pipeline.models import ClaimEvidence, DetectedClaim, EvidenceItem, Source
from pipeline.verdict import (
    PROMPT,
    WEB_ONLY_MAX_CONFIDENCE,
    VerdictAnswer,
    VerdictError,
    decide_verdicts,
    format_claim,
)

DATE = dt.date(2026, 11, 20)
MODEL = "claude-haiku-5-5"


def claim(cid: str = "c001") -> DetectedClaim:
    return DetectedClaim(
        id=cid,
        speaker="speaker_1",
        text_original="el paro ha bajado un 20 %",
        text_normalized="El paro en España ha bajado un 20 % desde 2024.",
        claim_type="estadistica",
        start=10.0,
        end=15.0,
    )


def item(eid: str, kind: str, url: str, **extra: Any) -> EvidenceItem:
    return EvidenceItem(
        id=eid,
        source=Source(
            url=url,
            title=f"Título {eid}",
            organization={"factcheck": "Maldita.es", "oficial": "INE", "web": "periodico.es"}[kind],
            date=dt.date(2026, 10, 24),
            kind=kind,
        ),
        excerpt=f"Extracto {eid}",
        **extra,
    )


FC = item(
    "E1", "factcheck", "https://maldita.es/a", claim_reviewed="El paro bajó un 20 %", rating="Falso"
)
OF = item("E2", "oficial", "https://www.ine.es/epa")
WEB = item("E3", "web", "https://periodico.es/n")


def evidence(*items: EvidenceItem, cid: str = "c001", errors: list[str] | None = None):
    return ClaimEvidence(claim_id=cid, items=list(items), errors=errors or [])


def answer(**overrides: Any) -> VerdictAnswer:
    data = {
        "verdict": "falso",
        "confidence": 0.9,
        "explanation": "La EPA muestra que el paro bajó un 8 % en ese periodo.",
        "cited_evidence": ["E2"],
        "matched_factcheck": None,
    }
    return VerdictAnswer(**data | overrides)


class FakeLLM:
    def __init__(self, *results: VerdictAnswer | Exception) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def structured(self, **kwargs: Any) -> VerdictAnswer:
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def run(claims, evidences, llm):
    return decide_verdicts(claims, evidences, speech_date=DATE, llm=llm, model=MODEL)


def test_no_evidence_is_no_verificable_without_calling_claude():
    llm = FakeLLM()
    [v] = run([claim()], [evidence()], llm)
    assert llm.calls == []
    assert (v.verdict, v.confidence, v.sources) == ("no_verificable", 0.0, [])
    assert "No se ha encontrado evidencia" in v.explanation
    assert v.analysis_error is None


def test_failed_search_is_not_reported_as_no_evidence():
    [v] = run([claim()], [evidence(errors=["factcheck: HTTP 500"])], FakeLLM())
    assert v.verdict == "no_verificable"
    assert "No se ha encontrado evidencia" not in v.explanation
    assert "búsqueda de evidencia falló" in v.explanation
    assert v.evidence_errors == ["factcheck: HTTP 500"]


def test_partial_search_errors_travel_with_the_verdict():
    [v] = run([claim()], [evidence(OF, errors=["web: HTTP 500"])], FakeLLM(answer()))
    assert v.verdict == "falso"
    assert v.evidence_errors == ["web: HTTP 500"]


def test_verdict_maps_cited_ids_to_sources():
    llm = FakeLLM(answer(cited_evidence=["E2", "E1"]))
    [v] = run([claim()], [evidence(FC, OF)], llm)

    assert (v.claim_id, v.verdict, v.confidence) == ("c001", "falso", 0.9)
    assert [s.url for s in v.sources] == ["https://www.ine.es/epa", "https://maldita.es/a"]
    assert v.basis == "factcheck"
    assert llm.calls[0]["prompt"] == PROMPT
    assert llm.calls[0]["step"] == "verdicts"


def test_unknown_ids_are_dropped_and_no_valid_source_downgrades():
    [v] = run([claim()], [evidence(OF)], FakeLLM(answer(cited_evidence=["E9"])))
    assert v.verdict == "no_verificable"
    assert v.confidence == 0.0
    assert v.sources == []
    assert "sin citar evidencia válida" in v.explanation


def test_matched_factcheck_requires_a_cited_factcheck_item():
    ok = answer(cited_evidence=["E1"], matched_factcheck="E1")
    [v] = run([claim()], [evidence(FC, OF)], FakeLLM(ok))
    assert v.matched_factcheck == "https://maldita.es/a"

    not_fc = answer(cited_evidence=["E2"], matched_factcheck="E2")
    [v] = run([claim()], [evidence(FC, OF)], FakeLLM(not_fc))
    assert v.matched_factcheck is None

    uncited = answer(cited_evidence=["E2"], matched_factcheck="E1")
    [v] = run([claim()], [evidence(FC, OF)], FakeLLM(uncited))
    assert v.matched_factcheck is None


def test_web_only_verdicts_have_capped_confidence():
    [v] = run([claim()], [evidence(WEB)], FakeLLM(answer(cited_evidence=["E3"], confidence=0.95)))
    assert v.confidence == WEB_ONLY_MAX_CONFIDENCE
    assert v.basis == "web"


def test_no_verificable_from_claude_keeps_sources_with_zero_confidence():
    llm = FakeLLM(answer(verdict="no_verificable", confidence=0.8, cited_evidence=["E2"]))
    [v] = run([claim()], [evidence(OF)], llm)
    assert (v.verdict, v.confidence) == ("no_verificable", 0.0)
    assert len(v.sources) == 1


def test_llm_error_on_one_claim_is_recorded_and_others_continue():
    llm = FakeLLM(LLMError("respuesta inválida"), answer())
    v1, v2 = run([claim("c001"), claim("c002")], [evidence(OF), evidence(OF, cid="c002")], llm)
    assert v1.verdict == "no_verificable"
    assert "respuesta inválida" in (v1.analysis_error or "")
    assert v2.verdict == "falso"


def test_all_analyses_failing_stops_the_step():
    llm = FakeLLM(LLMError("a"), LLMError("b"))
    with pytest.raises(VerdictError, match="Todos los veredictos"):
        run([claim("c001"), claim("c002")], [evidence(OF), evidence(OF, cid="c002")], llm)


def test_config_error_stops_immediately():
    with pytest.raises(LLMConfigError):
        run([claim()], [evidence(OF)], FakeLLM(LLMConfigError("clave")))


def test_missing_evidence_entry_is_an_error():
    with pytest.raises(VerdictError, match="c001"):
        run([claim()], [], FakeLLM())


def test_user_content_has_claim_date_and_evidence_but_not_the_speaker():
    content = format_claim(claim(), evidence(FC, OF), DATE)
    assert "<fecha>2026-11-20</fecha>" in content
    assert "El paro en España ha bajado un 20 % desde 2024." in content
    assert "el paro ha bajado un 20 %" in content
    assert 'id="E1"' in content and 'tipo="factcheck"' in content
    assert 'organismo="INE"' in content and 'fecha="2026-10-24"' in content
    assert "Falso" in content and "El paro bajó un 20 %" in content
    assert "speaker_1" not in content


def test_answer_rejects_forbidden_wording():
    with pytest.raises(ValidationError):
        answer(explanation="Es una mentira evidente.")
