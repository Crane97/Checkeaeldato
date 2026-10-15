"""Datos extraídos de la web reutilizables (F2)."""

import datetime as dt
from typing import Any

import psycopg
import pytest

from pipeline.knowledge.catalog import load_catalog
from pipeline.knowledge.web_facts import (
    Extraction,
    WebFact,
    WebFactStore,
    extract,
    facts_evidence,
    period_label,
    validated,
)
from pipeline.models import EvidenceItem, Source

from .test_knowledge_schema import db  # noqa: F401  (fixture)

SPEC = load_catalog()["pension_media"]
ITEM = EvidenceItem(
    id="E2",
    source=Source(url="https://www.seg-social.es/x", title="Nota", organization="Seguridad Social",
                  kind="oficial"),
    excerpt="En diciembre de 2023 la pensión media del sistema se situó en 1.200,5 euros al mes.",
)  # fmt: skip
GOOD = Extraction(
    encontrado=True, evidencia="E2", valor=1200.5, unidad="euros/mes",
    cita="la pensión media del sistema se situó en 1.200,5 euros al mes",
    enunciado="Pensión media del sistema en 2023: 1.200,5 euros/mes",
)  # fmt: skip


def test_a_fact_needs_its_literal_quote_with_the_value():
    fact = validated(GOOD, [ITEM], SPEC, "2023")
    assert (fact.valor, fact.tipo_fuente, fact.periodo) == (1200.5, "oficial", "2023")
    invented = GOOD.model_copy(update={"cita": "la pensión media fue de 1.200,5 euros"})
    assert validated(invented, [ITEM], SPEC, "2023") is None
    wrong_value = GOOD.model_copy(update={"valor": 1250.0})
    assert validated(wrong_value, [ITEM], SPEC, "2023") is None
    assert validated(GOOD.model_copy(update={"evidencia": "E9"}), [ITEM], SPEC, "2023") is None
    assert validated(Extraction(encontrado=False), [ITEM], SPEC, "2023") is None


def test_only_single_year_claims_get_a_period_key():
    assert period_label([2023]) == "2023" and period_label([2023, 2023]) == "2023"
    assert period_label([2022, 2023]) is None and period_label([]) is None


class FakeLLM:
    def __init__(self, answer: Extraction) -> None:
        self.answer, self.prompts = answer, []

    def structured(self, *, user_content: str, **kwargs: Any) -> Extraction:
        self.prompts.append((kwargs["prompt"], user_content))
        return self.answer


def test_extraction_sends_the_indicator_period_and_evidence():
    llm = FakeLLM(GOOD)
    fact = extract(SPEC, "2023", [ITEM], llm=llm, model="m")
    assert fact is not None
    prompt, content = llm.prompts[0]
    assert prompt == "extraccion_dato.md"
    assert "<periodo>2023</periodo>" in content and "pension_media" in content
    assert "1.200,5 euros" in content


@pytest.mark.supabase
def test_facts_are_stored_once_found_by_exact_keys_and_settle_official_claims(db):  # noqa: F811
    class Conn:  # reuse the test transaction (commit/rollback are no-ops here)
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *a):
            return db.execute(*a)

        def commit(self):
            pass

        def rollback(self):
            pass

    store = WebFactStore(Conn, dt.date(2026, 10, 14))
    fact = validated(GOOD, [ITEM], SPEC, "2023")
    first = store.save(fact, model="m")
    assert first is not None and store.save(fact, model="m") is None  # no duplicates
    [found] = store.find("pension_media", "2023")
    assert (found.id, found.valor, found.cita) == (first, 1200.5, fact.cita)
    assert store.find("pension_media", "2022") == []
    items, settled = facts_evidence(store, "pension_media", [2023])
    assert settled and items[0].web_fact_id == first and items[0].source.kind == "oficial"
    expires = db.execute("select caduca_en from evid.web_facts where id = %s", (first,)).fetchone()
    assert expires == (None,)  # 2023 is a closed period by 2026
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "insert into evid.web_facts (enunciado, claves, source_url, tipo_fuente, valor, cita)"
            " values ('x', %s::jsonb, 'https://x.es', 'blog', 1, 'cita')",
            ('{"indicador": "x", "territorio": "ES", "periodo": "2023"}',),
        )


def test_web_fact_evidence_keeps_its_kind():
    fact = WebFact(
        indicador="x", periodo="2023", enunciado="e", valor=1, unidad="u", cita="c",
        source_url="https://maldita.es/x", organismo="Maldita", tipo_fuente="verificador",
    )  # fmt: skip
    assert fact.evidence().source.kind == "factcheck"


def test_government_press_figures_are_kept_but_never_spare_the_search():
    moncloa = ITEM.model_copy(
        update={"source": ITEM.source.model_copy(update={"url": "https://www.lamoncloa.gob.es/x"})}
    )
    fact = validated(GOOD, [moncloa], SPEC, "2023")
    assert fact.tipo_fuente == "oficial_gobierno"

    class Store:
        def find(self, indicador, periodo):
            return [fact]

    items, settled = facts_evidence(Store(), "pension_media", [2023])
    assert items and not settled  # the web search still runs, for every speaker alike


def test_evaluative_statements_are_replaced_by_a_plain_one():
    loaded = GOOD.model_copy(update={"enunciado": "La pensión media marca un récord histórico"})
    fact = validated(loaded, [ITEM], SPEC, "2023")
    assert fact.enunciado == f"{SPEC.nombre}, 2023: 1200.5"


def test_figures_written_with_a_scale_match_their_value():
    item = ITEM.model_copy(
        update={"excerpt": "El número de pensiones alcanzó 10,3 millones en 2023."}
    )
    answer = Extraction(encontrado=True, evidencia="E2", valor=10300000,
                        cita="alcanzó 10,3 millones en 2023", unidad="pensiones")  # fmt: skip
    assert validated(answer, [item], load_catalog()["num_pensiones"], "2023") is not None
