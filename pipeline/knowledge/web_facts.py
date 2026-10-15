"""Datos extraídos de la web, reutilizables por claves exactas (fase F2).

Tras un veredicto sobre un indicador del catálogo que la base no cubría (ni la API), un
modelo barato extrae de la evidencia citada el dato de ese periodo exacto, con su cita
literal; el código comprueba que la cita está en la fuente y contiene el número. Se guarda
en evid.web_facts con claves {indicador, territorio, periodo}. En afirmaciones
posteriores con las mismas claves, el dato se usa como evidencia antes de buscar en la
web (y, si viene de una fuente oficial o un verificador, sin buscar).

Un dato de un periodo reciente caduca (puede revisarse); uno de un periodo cerrado
(anterior al año pasado) no.
"""

import datetime as dt
import json
import logging
import re
from collections.abc import Sequence

from pydantic import BaseModel

from pipeline.knowledge.catalog import IndicatorSpec
from pipeline.llm import LLMConfigError, LLMError
from pipeline.models import EvidenceItem, Source
from pipeline.neutrality import neutrality_problems
from pipeline.speakers import StructuredLLM, _fold
from pipeline.verdict import is_government_voice

logger = logging.getLogger(__name__)

PROMPT = "extraccion_dato.md"
RECENT_DAYS = 30
# Kinds of web evidence → evid.web_facts.tipo_fuente
_TIPO = {"oficial": "oficial", "factcheck": "verificador", "web": "otro"}
REUSABLE_WITHOUT_SEARCH = {"oficial", "oficial_extranjera", "verificador"}


class Extraction(BaseModel):
    encontrado: bool
    evidencia: str = ""
    cita: str = ""
    valor: float | None = None
    unidad: str = ""
    enunciado: str = ""


class WebFact(BaseModel):
    id: int | None = None
    indicador: str
    territorio: str = "ES"
    periodo: str
    enunciado: str
    valor: float
    unidad: str
    cita: str
    source_url: str
    organismo: str
    fecha_fuente: dt.date | None = None
    tipo_fuente: str

    def keys(self) -> dict[str, str]:
        return {"indicador": self.indicador, "territorio": self.territorio, "periodo": self.periodo}

    def evidence(self) -> EvidenceItem:
        kind = "oficial" if self.tipo_fuente.startswith("oficial") else (
            "factcheck" if self.tipo_fuente == "verificador" else "web"
        )  # fmt: skip
        return EvidenceItem(
            id="",
            source=Source(
                url=self.source_url,
                title=f"{self.enunciado} (dato extraído en una verificación anterior)",
                organization=self.organismo,
                date=self.fecha_fuente,
                kind=kind,  # type: ignore[arg-type]
            ),
            excerpt=f"«{self.cita}» ({self.organismo}). Dato: {self.enunciado}",
            web_fact_id=self.id,
        )


def period_label(years: Sequence[int]) -> str | None:
    """The exact period key for a claim: one year only (others are not reused)."""
    return str(years[0]) if len(set(years)) == 1 else None


_SCALES = {"mil": 1e3, "miles": 1e3, "millon": 1e6, "millones": 1e6, "billon": 1e12,
           "billones": 1e12}  # fmt: skip
# Evaluative words a reused statement must not carry into later verdicts.
_EVALUATIVE = re.compile(
    r"\b(record|se dispara|dispar[oa]|desplom|hund|histori[ck]|brutal|escandal|alarmante)",
    re.IGNORECASE,
)


def _numbers(text: str) -> set[float]:
    """Numbers written in Spanish ("1.080,5", "3,5", "1,2 millones") found in a text."""
    out = set()
    folded = _fold(text)
    for m in re.finditer(r"(\d[\d.]*(?:,\d+)?)(?:\s+(\w+))?", folded):
        try:
            value = float(m.group(1).replace(".", "").replace(",", "."))
        except ValueError:
            continue
        out.add(value)
        if m.group(2) in _SCALES:
            out.add(value * _SCALES[m.group(2)])
    return out


def validated(
    answer: Extraction, items: Sequence[EvidenceItem], spec: IndicatorSpec, periodo: str
) -> WebFact | None:
    """The fact if its quote is literally in the cited item and contains the value."""
    if not answer.encontrado or answer.valor is None or not answer.cita.strip():
        return None
    item = next((i for i in items if i.id == answer.evidencia), None)
    if item is None or _fold(answer.cita) not in _fold(item.excerpt):
        return None
    if not any(abs(n - abs(answer.valor)) < 1e-9 for n in _numbers(answer.cita)):
        return None
    tipo = _TIPO.get(item.source.kind)
    if tipo is None:  # local series are not web facts
        return None
    if tipo == "oficial" and is_government_voice(item.source.url):
        # Reusable, but it never spares the search: for a speaker of the governing party
        # it counts as neutral, so other sources (INE, BOE…) must still be found.
        tipo = "oficial_gobierno"
    default = f"{spec.nombre}, {periodo}: {answer.valor:g}"
    statement = answer.enunciado.strip()
    if not statement or neutrality_problems(statement) or _EVALUATIVE.search(_fold(statement)):
        statement = default
    return WebFact(
        indicador=spec.id,
        periodo=periodo,
        enunciado=statement,
        valor=answer.valor,
        unidad=answer.unidad.strip() or spec.unidad,
        cita=answer.cita.strip(),
        source_url=item.source.url,
        organismo=item.source.organization,
        fecha_fuente=item.source.date,
        tipo_fuente=tipo,
    )


def extract(
    spec: IndicatorSpec,
    periodo: str,
    items: Sequence[EvidenceItem],
    *,
    llm: StructuredLLM,
    model: str,
) -> WebFact | None:
    """Ask the (cheap) model for the fact; None if absent, unverifiable or the call fails."""
    evidence = "\n".join(
        f'<fragmento id="{i.id}" organismo="{i.source.organization}" url="{i.source.url}"'
        f' fecha="{i.source.date or "sin fecha"}">\n{i.excerpt}\n</fragmento>'
        for i in items
    )
    content = (
        f"<indicador>{spec.id}: {spec.nombre}. {spec.definicion} Unidad: {spec.unidad}."
        f"</indicador>\n<periodo>{periodo}</periodo>\n<evidencia>\n{evidence}\n</evidencia>"
    )
    try:
        answer = llm.structured(
            step="web_facts", model=model, prompt=PROMPT, user_content=content,
            output_type=Extraction,
        )  # fmt: skip
    except LLMConfigError:
        raise
    except LLMError as exc:
        logger.warning("No se pudo extraer el dato de %s %s: %s", spec.id, periodo, exc)
        return None
    return validated(answer, items, spec, periodo)


class WebFactStore:
    """evid.web_facts in Supabase; one short connection per call."""

    def __init__(self, connect, today: dt.date) -> None:
        self._connect = connect
        self.today = today

    def find(self, indicador: str, periodo: str, territorio: str = "ES") -> list[WebFact]:
        keys = json.dumps({"indicador": indicador, "territorio": territorio, "periodo": periodo})
        with self._connect() as conn:
            rows = conn.execute(
                "select id, enunciado, valor::float8, unidad, cita, source_url, organismo,"
                " fecha_fuente, tipo_fuente from evid.web_facts where claves = %s::jsonb"
                " and (caduca_en is null or caduca_en > now()) order by id",
                (keys,),
            ).fetchall()
            conn.rollback()
        return [
            WebFact(
                id=r[0],
                indicador=indicador,
                territorio=territorio,
                periodo=periodo,
                enunciado=r[1],
                valor=r[2],
                unidad=r[3] or "",
                cita=r[4],
                source_url=r[5],
                organismo=r[6] or "",
                fecha_fuente=r[7],
                tipo_fuente=r[8],
            )  # fmt: skip
            for r in rows
        ]

    def save(self, fact: WebFact, *, model: str) -> int | None:
        closed = fact.periodo[:4].isdigit() and int(fact.periodo[:4]) <= self.today.year - 2
        expires = None if closed else dt.datetime.now(dt.UTC) + dt.timedelta(days=RECENT_DAYS)
        with self._connect() as conn:
            row = conn.execute(
                "insert into evid.web_facts (enunciado, claves, valor, unidad, source_url,"
                " organismo, fecha_fuente, tipo_fuente, caduca_en, cita, modelo)"
                " values (%s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict do nothing returning id",
                (
                    fact.enunciado, json.dumps(fact.keys()), fact.valor, fact.unidad,
                    fact.source_url, fact.organismo, fact.fecha_fuente, fact.tipo_fuente,
                    expires, fact.cita, model,
                ),
            ).fetchone()  # fmt: skip
            conn.commit()
        return row[0] if row else None


def facts_evidence(
    store: WebFactStore, indicador: str, years: Sequence[int]
) -> tuple[list[EvidenceItem], bool]:
    """(evidence from stored facts, whether they make the web search unnecessary)."""
    periodo = period_label(years)
    if periodo is None:
        return [], False
    facts = store.find(indicador, periodo)
    items = [f.evidence() for f in facts]
    settled = any(f.tipo_fuente in REUSABLE_WITHOUT_SEARCH for f in facts)
    return items, settled
