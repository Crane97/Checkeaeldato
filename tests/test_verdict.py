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
    is_government_voice,
    own_source_checker,
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
    """A model answer; `cited_evidence` fills evidence_used, and each used item gets the
    stance that matches the verdict unless `stances` says otherwise."""
    used = overrides.pop("cited_evidence", ["E2"])
    stances = overrides.pop("stances", {})
    verdict = overrides.get("verdict", "falso")
    default = {"falso": "contradice", "verdadero": "apoya"}.get(verdict, "neutral")
    data = {
        "evidence_used": used,
        "analysis": {
            "claim_parsed": "Cifra: 20 %; indicador: paro; periodo: desde 2024; actor: —",
            "evidence_assessment": [
                {"id": i, "says": f"Lo que dice {i}", "stance": stances.get(i, default)}
                for i in used
            ],
            "numeric_comparison": "20 % afirmado frente a 8 % en la EPA",
            "nuances": "Ninguno que cambie la conclusión.",
            "conclusion": overrides.pop("conclusion", "Conclusión basada en la evidencia."),
        },
        "numeric_check": "20 ≠ 8",
        "verdict": verdict,
        "confidence": 0.9,
        "explanation": "La EPA muestra que el paro bajó un 8 % en ese periodo.",
        "matched_factcheck": None,
        "follow_up_queries": [],
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


def run(claims, evidences, llm, follow_up=None):
    return decide_verdicts(
        claims, evidences, speech_date=DATE, llm=llm, model=MODEL, follow_up=follow_up
    )


class FollowUp:
    """Fake second-round search returning prepared items."""

    def __init__(self, items=(), errors=()) -> None:
        self.items, self.errors = list(items), list(errors)
        self.calls: list[tuple[str, list[str], int]] = []

    def __call__(self, claim, queries, first_id):
        self.calls.append((claim.id, queries, first_id))
        renumbered = [
            i.model_copy(update={"id": f"E{first_id + n}"}) for n, i in enumerate(self.items)
        ]
        return renumbered, self.errors


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


def test_citing_evidence_that_does_not_exist_needs_review():
    llm = FakeLLM(answer(cited_evidence=["E9"]))
    [v] = run([claim()], [evidence(OF)], llm)
    assert (v.verdict, v.confidence, v.needs_review) == ("no_verificable", 0.0, True)
    assert any("E9" in r for r in v.review_reasons)
    assert len(llm.calls) == 1  # an invented source is not retried


def test_inexistent_id_mentioned_only_in_the_explanation_also_needs_review():
    bad = answer(explanation="Según E7, el paro bajó un 8 %.")
    [v] = run([claim()], [evidence(OF)], FakeLLM(bad))
    assert v.needs_review and v.verdict == "no_verificable"
    assert any("E7" in r for r in v.review_reasons)


def test_existing_ids_mentioned_in_the_text_join_the_sources():
    text = answer(explanation="La EPA (E2) y Maldita (E1) muestran que bajó un 8 %.")
    [v] = run([claim()], [evidence(FC, OF)], FakeLLM(text))
    assert {s.url for s in v.sources} == {"https://www.ine.es/epa", "https://maldita.es/a"}
    assert not v.needs_review


def test_sources_come_from_evidence_used_primary_sources_first():
    secondary = item("E4", "oficial", "https://www.dsca.gob.es/informe.pdf")
    boe = item("E5", "oficial", "https://www.boe.es/eli/es/rd/2023/02/14/99")
    used = ["E3", "E1", "E4", "E2", "E5"]
    llm = FakeLLM(answer(cited_evidence=used, stances={"E3": "neutral", "E1": "neutral"}))
    [v] = run([claim()], [evidence(FC, OF, WEB, secondary, boe)], llm)
    assert [s.url for s in v.sources] == [
        "https://www.ine.es/epa",
        "https://www.boe.es/eli/es/rd/2023/02/14/99",
        "https://www.dsca.gob.es/informe.pdf",
        "https://maldita.es/a",
        "https://periodico.es/n",
    ]


def test_false_needs_a_contradicting_source_or_is_asked_again():
    unsupported = answer(stances={"E2": "apoya"})
    fixed = answer(verdict="no_verificable", cited_evidence=[], explanation="No basta.")
    llm = FakeLLM(unsupported, fixed)
    [v] = run([claim()], [evidence(OF)], llm)
    assert len(llm.calls) == 2
    assert "contradice" in llm.calls[1]["user_content"]  # the retry says what was wrong
    assert v.verdict == "no_verificable" and not v.needs_review


def test_false_without_contradicting_source_twice_needs_review():
    unsupported = answer(stances={"E2": "neutral"})
    llm = FakeLLM(unsupported, unsupported)
    [v] = run([claim()], [evidence(OF)], llm)
    assert (v.verdict, v.needs_review) == ("no_verificable", True)
    assert any("contradice" in r for r in v.review_reasons)


def test_true_needs_a_supporting_source_too():
    unsupported = answer(verdict="verdadero", stances={"E2": "neutral"})
    llm = FakeLLM(unsupported, unsupported)
    [v] = run([claim()], [evidence(OF)], llm)
    assert v.needs_review and v.verdict == "no_verificable"


def test_explanation_that_supports_a_false_label_is_asked_again():
    smi = answer(explanation="La subida es de 344,10 € y coincide con los 344 € afirmados.")
    coherent = answer(verdict="verdadero", explanation="La subida es de 344,10 €, como se afirma.")
    llm = FakeLLM(smi, coherent)
    [v] = run([claim()], [evidence(OF)], llm)
    assert len(llm.calls) == 2
    assert v.verdict == "verdadero" and not v.needs_review


def test_incoherent_twice_needs_review():
    smi = answer(explanation="El cálculo respalda la cifra: es correcta.")
    [v] = run([claim()], [evidence(OF)], FakeLLM(smi, smi))
    assert (v.verdict, v.needs_review) == ("no_verificable", True)


@pytest.mark.parametrize(
    "explanation",
    [
        "La cifra no coincide con la EPA, que da un 8 %.",
        "El dato del paro coincide, pero el periodo no se ajusta y la EPA lo contradice.",
    ],
)
def test_negated_or_mixed_wording_is_not_incoherent(explanation):
    llm = FakeLLM(answer(explanation=explanation))
    [v] = run([claim()], [evidence(OF)], llm)
    assert len(llm.calls) == 1 and v.verdict == "falso"


def test_true_with_an_explanation_that_contradicts_is_asked_again():
    wrong = answer(verdict="verdadero", explanation="La EPA contradice la cifra: es falsa.")
    llm = FakeLLM(wrong, answer())
    run([claim()], [evidence(OF)], llm)
    assert len(llm.calls) == 2


def test_sources_from_the_speakers_own_government_are_neutral():
    moncloa = item("E4", "oficial", "https://www.lamoncloa.gob.es/presidente/x.aspx")
    only_own = answer(verdict="verdadero", cited_evidence=["E4"])
    llm = FakeLLM(only_own, only_own)
    [v] = decide_verdicts(
        [claim()],
        [evidence(moncloa)],
        speech_date=DATE,
        llm=llm,
        model=MODEL,
        own_source=lambda c, source: "lamoncloa.gob.es" in source.url,
    )
    assert 'del_gobierno_del_orador="sí"' in llm.calls[0]["user_content"]
    assert v.needs_review and v.verdict == "no_verificable"  # it did not count as support


def test_analysis_is_kept_for_auditing():
    [v] = run([claim()], [evidence(OF)], FakeLLM(answer()))
    assert v.evidence_used == ["E2"]
    assert v.analysis is not None
    assert v.analysis.evidence_assessment[0].stance == "contradice"
    assert v.numeric_check == "20 ≠ 8"


def test_answer_fields_are_ordered_evidence_first_and_verdict_after_the_analysis():
    assert list(VerdictAnswer.model_fields)[:6] == [
        "evidence_used",
        "analysis",
        "numeric_check",
        "verdict",
        "confidence",
        "explanation",
    ]


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


POP = item("X", "oficial", "https://www.ine.es/pere")


def test_undecided_claim_gets_one_targeted_follow_up_round():
    first = answer(
        verdict="no_verificable",
        cited_evidence=[],
        follow_up_queries=["PERE por edad", "población joven"],
    )
    final = answer(verdict="falso", cited_evidence=["E3"], confidence=0.9)
    llm = FakeLLM(first, final)
    follow_up = FollowUp(items=[POP])

    [v] = run([claim()], [evidence(FC, OF)], llm, follow_up)

    assert follow_up.calls == [("c001", ["PERE por edad", "población joven"], 3)]
    assert len(llm.calls) == 2
    second_content = llm.calls[1]["user_content"]
    assert 'id="E3"' in second_content and "<ronda_final>" in second_content
    assert "<ronda_final>" not in llm.calls[0]["user_content"]
    assert v.verdict == "falso"
    assert [s.url for s in v.sources] == ["https://www.ine.es/pere"]
    assert v.follow_up_queries == ["PERE por edad", "población joven"]


def test_decided_claims_do_not_search_again():
    follow_up = FollowUp(items=[POP])
    run([claim()], [evidence(OF)], FakeLLM(answer(follow_up_queries=["x"])), follow_up)
    assert follow_up.calls == []


def test_follow_up_without_new_evidence_keeps_the_first_answer():
    first = answer(verdict="no_verificable", cited_evidence=[], follow_up_queries=["x"])
    llm = FakeLLM(first)
    follow_up = FollowUp(errors=["oficial: HTTP 500"])
    [v] = run([claim()], [evidence(OF)], llm, follow_up)
    assert len(llm.calls) == 1
    assert v.verdict == "no_verificable"
    assert v.evidence_errors == ["oficial: HTTP 500"]


def test_follow_up_queries_are_capped_and_need_a_searcher():
    first = answer(
        verdict="no_verificable", cited_evidence=[], follow_up_queries=["a", "b", "c", "d"]
    )
    follow_up = FollowUp()
    run([claim()], [evidence(OF)], FakeLLM(first), follow_up)
    assert follow_up.calls[0][1] == ["a", "b", "c"]

    [v] = run([claim()], [evidence(OF)], FakeLLM(first))  # no searcher: single round
    assert v.verdict == "no_verificable"


def test_unresolved_context_is_no_verificable_with_the_reason_and_no_call():
    unresolved = claim().model_copy(
        update={
            "context_status": "no_resuelto",
            "resolution_notes": (
                "No se puede saber a quién se refiere: no se ha identificado al interlocutor."
            ),
        }
    )
    llm = FakeLLM()
    [v] = run([unresolved], [evidence(OF)], llm)
    assert llm.calls == []
    assert v.verdict == "no_verificable"
    assert "interlocutor" in v.explanation


def test_resolved_periods_and_actor_reach_the_model():
    from pipeline.models import DateRange

    resolved = claim().model_copy(
        update={
            "context_status": "resuelto",
            "resolved_actor": "PP (gobiernos de José María Aznar y Mariano Rajoy)",
            "time_scope": [
                DateRange(start=dt.date(1996, 5, 5), end=dt.date(2004, 4, 17)),
                DateRange(start=dt.date(2011, 12, 21), end=dt.date(2018, 6, 2)),
            ],
        }
    )
    content = format_claim(resolved, evidence(OF), DATE)
    assert (
        "<actor_resuelto>PP (gobiernos de José María Aznar y Mariano Rajoy)</actor_resuelto>"
        in content
    )
    assert "<periodos>05/05/1996–17/04/2004; 21/12/2011–02/06/2018</periodos>" in content


def test_verdict_input_is_capped_reliable_sources_first():
    from pipeline.verdict import MAX_EVIDENCE_ITEMS, VERDICT_EXCERPT_CHARS

    webs = [item(f"E{i}", "web", f"https://web{i}.es/x") for i in range(1, 9)]
    officials = [item(f"E{i}", "oficial", f"https://www.ine.es/{i}") for i in range(9, 13)]
    long = officials[0].model_copy(update={"excerpt": "x" * 9000})
    content = format_claim(claim(), evidence(*webs, long, *officials[1:]), DATE)

    assert content.count("<evidencia ") == MAX_EVIDENCE_ITEMS
    assert all(f'id="E{i}"' in content for i in range(9, 13))  # every official kept
    assert 'id="E5"' not in content  # lower-priority web items dropped first
    assert "x" * (VERDICT_EXCERPT_CHARS + 1) not in content


def test_follow_up_items_are_always_sent():
    officials = [item(f"E{i}", "oficial", f"https://www.ine.es/{i}") for i in range(1, 12)]
    content = format_claim(claim(), evidence(*officials), DATE, final=True, prefer=["E11"])
    assert 'id="E11"' in content


def test_no_follow_up_search_when_a_published_check_settles_the_claim():
    matching = item(
        "E1",
        "factcheck",
        "https://maldita.es/a",
        claim_reviewed="El paro en España ha bajado un 20 % desde 2024",
    )
    first = answer(verdict="no_verificable", cited_evidence=[], follow_up_queries=["x"])
    follow_up = FollowUp(items=[POP])
    run([claim()], [evidence(matching)], FakeLLM(first), follow_up)
    assert follow_up.calls == []


@pytest.mark.parametrize(
    ("url", "voice"),
    [
        ("https://www.lamoncloa.gob.es/presidente/actividades/x.aspx", True),
        ("https://www.hacienda.gob.es/es-ES/Prensa/Notas%20de%20prensa/x.aspx", True),
        ("https://www.mites.gob.es/es/prensa/notas/x.htm", True),
        ("https://www.hacienda.gob.es/es-ES/Estadisticas/x.aspx", False),
        ("https://www.ine.es/prensa/epa.pdf", False),  # statistics office, not the Government
        ("https://www.boe.es/x", False),
    ],
)
def test_government_voice_is_la_moncloa_and_ministry_press_rooms(url, voice):
    assert is_government_voice(url) is voice


def test_own_source_only_for_speakers_of_a_governing_party():
    from pipeline.governments import default_governments
    from pipeline.models import Speaker, SpeakerMap

    speakers = SpeakerMap(
        speakers=[
            Speaker(label="Presidente", name="Pedro Sánchez", party="PSOE"),
            Speaker(label="Feijóo", name="Alberto Núñez Feijóo", party="PP"),
        ]
    )
    check = own_source_checker(speakers, default_governments(), dt.date(2023, 2, 21))
    moncloa = item("E1", "oficial", "https://www.lamoncloa.gob.es/x").source
    gov = claim().model_copy(update={"speaker": "Presidente"})
    opp = claim().model_copy(update={"speaker": "Feijóo"})
    assert check(gov, moncloa) is True
    assert check(opp, moncloa) is False
    assert check(gov, OF.source) is False
