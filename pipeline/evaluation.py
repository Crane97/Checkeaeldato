"""Evaluación contra el gold set: formato de casos, emparejamiento y métricas."""

import datetime as dt
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ValidationError, model_validator
from rapidfuzz import fuzz

from pipeline.models import Claim, Report, VerdictLabel

GOLD_DIR = Path(__file__).resolve().parent.parent / "eval" / "gold"
# Minimum fuzzy similarity (0-100) between a gold quote and a detected quote.
MATCH_THRESHOLD = 80
# Seconds of slack when both quotes have video timestamps.
TIME_TOLERANCE = 5.0


class GoldInput(BaseModel):
    tipo: Literal["texto", "video"]
    texto: str | None = None  # inline article for tipo "texto"
    ruta: str | None = None  # video path (relative to the repo root) for tipo "video"

    @model_validator(mode="after")
    def _has_content(self) -> Self:
        if self.tipo == "texto" and not self.texto:
            raise ValueError("una entrada de tipo texto necesita 'texto'")
        if self.tipo == "video" and not self.ruta:
            raise ValueError("una entrada de tipo video necesita 'ruta'")
        return self


class GoldClaim(BaseModel):
    cita: str  # literal quote as it appears in the input
    texto_normalizado: str | None = None  # used by the verdict-only mode
    inicio: float | None = None  # seconds, video only
    fin: float | None = None
    veredicto_esperado: VerdictLabel
    desmentido: str | None = None  # URL of the published fact-check it comes from
    notas: str | None = None


class GoldCase(BaseModel):
    id: str
    descripcion: str
    fecha: dt.date
    # Synthetic cases only check that the evaluation runs; they do not measure quality.
    sintetico: bool = False
    entrada: GoldInput
    afirmaciones: list[GoldClaim]
    # Opinions, promises… present in the input that must NOT be detected.
    no_afirmaciones: list[str] = []


def load_gold_cases(directory: Path = GOLD_DIR) -> list[GoldCase]:
    cases = []
    for path in sorted(directory.glob("*.json")):
        try:
            cases.append(GoldCase.model_validate(json.loads(path.read_text())))
        except (ValidationError, ValueError) as exc:
            raise ValueError(f"Caso gold inválido en {path.name}: {exc}") from exc
    return cases


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _similarity(a: str, b: str) -> float:
    return fuzz.partial_ratio(_norm(a), _norm(b))


def _times_compatible(gold: GoldClaim, detected: Claim) -> bool:
    if None in (gold.inicio, gold.fin, detected.start, detected.end):
        return True
    return (
        detected.start <= gold.fin + TIME_TOLERANCE  # type: ignore[operator]
        and detected.end >= gold.inicio - TIME_TOLERANCE  # type: ignore[operator]
    )


def match_claims(gold: list[GoldClaim], detected: list[Claim]) -> dict[int, str]:
    """Gold index -> detected claim id, one-to-one, best similarity first."""
    candidates = sorted(
        (
            (_similarity(g.cita, d.text_original), gi, d.id)
            for gi, g in enumerate(gold)
            for d in detected
            if _times_compatible(g, d)
        ),
        reverse=True,
    )
    matches: dict[int, str] = {}
    used: set[str] = set()
    for score, gi, did in candidates:
        if score < MATCH_THRESHOLD or gi in matches or did in used:
            continue
        matches[gi] = did
        used.add(did)
    return matches


@dataclass
class CaseResult:
    case_id: str
    synthetic: bool
    gold_claims: int
    matched: int
    correct: int
    false_positives: int  # detected opinions/promises listed in no_afirmaciones
    extra_detected: int  # detected claims not in the gold set (gold may be incomplete)
    confusion: dict[tuple[str, str], int] = field(default_factory=dict)  # (expected, got)
    missed: list[str] = field(default_factory=list)


def score_case(case: GoldCase, report: Report) -> CaseResult:
    by_id = {c.id: c for c in report.claims}
    matches = match_claims(case.afirmaciones, report.claims)
    confusion: Counter[tuple[str, str]] = Counter()
    for gi, did in matches.items():
        confusion[(case.afirmaciones[gi].veredicto_esperado, by_id[did].verdict)] += 1

    unmatched = [c for c in report.claims if c.id not in set(matches.values())]
    false_positives = sum(
        1
        for c in unmatched
        if any(_similarity(n, c.text_original) >= MATCH_THRESHOLD for n in case.no_afirmaciones)
    )
    return CaseResult(
        case_id=case.id,
        synthetic=case.sintetico,
        gold_claims=len(case.afirmaciones),
        matched=len(matches),
        correct=sum(n for (expected, got), n in confusion.items() if expected == got),
        false_positives=false_positives,
        extra_detected=len(unmatched) - false_positives,
        confusion=dict(confusion),
        missed=[g.cita for gi, g in enumerate(case.afirmaciones) if gi not in matches],
    )


@dataclass
class Summary:
    cases: int
    gold_claims: int
    matched: int
    correct: int
    false_positives: int
    extra_detected: int
    confusion: dict[tuple[str, str], int]

    @property
    def coverage(self) -> float | None:
        return self.matched / self.gold_claims if self.gold_claims else None

    @property
    def verdict_accuracy(self) -> float | None:
        return self.correct / self.matched if self.matched else None


def summarize(results: list[CaseResult]) -> Summary:
    confusion: Counter[tuple[str, str]] = Counter()
    for r in results:
        confusion.update(r.confusion)
    return Summary(
        cases=len(results),
        gold_claims=sum(r.gold_claims for r in results),
        matched=sum(r.matched for r in results),
        correct=sum(r.correct for r in results),
        false_positives=sum(r.false_positives for r in results),
        extra_detected=sum(r.extra_detected for r in results),
        confusion=dict(confusion),
    )
