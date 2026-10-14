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

from pipeline.models import Claim, DateRange, Promise, PromiseLabel, Report, VerdictLabel

GOLD_DIR = Path(__file__).resolve().parent.parent / "eval" / "gold"
# Minimum fuzzy similarity (0-100) between a gold quote and a detected quote.
MATCH_THRESHOLD = 80
# Seconds of slack when both quotes have video timestamps.
TIME_TOLERANCE = 5.0
# Days of slack when comparing resolved periods with the expected ones.
PERIOD_TOLERANCE_DAYS = 31


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
    veredicto_esperado: VerdictLabel | None = None  # None: verdict not checked
    orador: str | None = None
    # Context resolution: expected party in the resolved actor, or "no_resuelto".
    actor_esperado: str | None = None
    periodos_esperados: list[DateRange] | None = (
        None  # expected speaker (name as written), if checked
    )
    desmentido: str | None = None  # URL of the published fact-check it comes from
    notas: str | None = None


class GoldPromise(BaseModel):
    cita: str  # literal quote as it appears in the input
    inicio: float | None = None
    fin: float | None = None
    valoracion_esperada: PromiseLabel | None = None  # None: only detection is checked
    notas: str | None = None


class GoldCase(BaseModel):
    id: str
    descripcion: str
    fecha: dt.date
    # Synthetic cases only check that the evaluation runs; they do not measure quality.
    sintetico: bool = False
    # In the quick evaluation (--rapida): the cases most prone to errors, about half the
    # web searches of the full one.
    rapida: bool = False
    entrada: GoldInput
    afirmaciones: list[GoldClaim]
    # Concrete promises that must be detected as promises (and assessed).
    promesas: list[GoldPromise] = []
    # Opinions, vague promises… present in the input that must NOT be detected.
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


def _times_compatible(gold: GoldClaim | GoldPromise, detected: Claim | Promise) -> bool:
    if None in (gold.inicio, gold.fin, detected.start, detected.end):
        return True
    return (
        detected.start <= gold.fin + TIME_TOLERANCE  # type: ignore[operator]
        and detected.end >= gold.inicio - TIME_TOLERANCE  # type: ignore[operator]
    )


def match_claims(
    gold: list[GoldClaim] | list[GoldPromise], detected: list[Claim] | list[Promise]
) -> dict[int, str]:
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
    speakers_checked: int = 0
    speakers_correct: int = 0
    wrong_speakers: list[str] = field(default_factory=list)  # "quote: expected -> got"
    verdicts_checked: int = 0
    context_checked: int = 0
    context_correct: int = 0
    wrong_context: list[str] = field(default_factory=list)
    promises_gold: int = 0
    promises_matched: int = 0
    promises_checked: int = 0
    promises_correct: int = 0
    missed_promises: list[str] = field(default_factory=list)
    wrong_promises: list[str] = field(default_factory=list)  # "quote: expected -> got"


def _periods_match(expected: list[DateRange], got: list[DateRange]) -> bool:
    if len(expected) != len(got):
        return False
    slack = dt.timedelta(days=PERIOD_TOLERANCE_DAYS)
    return all(
        abs(e.start - g.start) <= slack and abs(e.end - g.end) <= slack
        for e, g in zip(
            sorted(expected, key=lambda r: r.start), sorted(got, key=lambda r: r.start), strict=True
        )
    )


def _context_ok(gold: GoldClaim, claim: Claim) -> bool:
    if gold.actor_esperado == "no_resuelto":
        return claim.context_status == "no_resuelto"
    if claim.context_status != "resuelto" or not claim.resolved_actor:
        return False
    if gold.actor_esperado not in claim.resolved_actor:
        return False
    return gold.periodos_esperados is None or _periods_match(
        gold.periodos_esperados, claim.time_scope
    )


def _same_speaker(expected: str, got: str) -> bool:
    a, b = (frozenset(re.findall(r"\w+", x.casefold())) for x in (expected, got))
    return bool(a) and (a <= b or b <= a)


def score_case(case: GoldCase, report: Report) -> CaseResult:
    by_id = {c.id: c for c in report.claims}
    matches = match_claims(case.afirmaciones, report.claims)
    confusion: Counter[tuple[str, str]] = Counter()
    for gi, did in matches.items():
        expected_verdict = case.afirmaciones[gi].veredicto_esperado
        if expected_verdict is not None:
            confusion[(expected_verdict, by_id[did].verdict)] += 1

    context_checked = context_correct = 0
    wrong_context: list[str] = []
    for gi, did in matches.items():
        gold_claim, got_claim = case.afirmaciones[gi], by_id[did]
        if gold_claim.actor_esperado is None:
            continue
        context_checked += 1
        if _context_ok(gold_claim, got_claim):
            context_correct += 1
        else:
            periods = "; ".join(r.label() for r in got_claim.time_scope) or "—"
            wrong_context.append(
                f"{gold_claim.cita}: esperado {gold_claim.actor_esperado}, obtenido "
                f"{got_claim.context_status} {got_claim.resolved_actor or ''} [{periods}]"
            )

    checked = correct = 0
    wrong: list[str] = []
    for gi, did in matches.items():
        expected = case.afirmaciones[gi].orador
        if expected is None:
            continue
        got = report.speaker_name(by_id[did].speaker)
        checked += 1
        if _same_speaker(expected, got):
            correct += 1
        else:
            wrong.append(f"{case.afirmaciones[gi].cita}: {expected} -> {got}")

    promise_matches = match_claims(case.promesas, report.promises)
    promises = {p.id: p for p in report.promises}
    promises_checked = promises_correct = 0
    wrong_promises: list[str] = []
    for gi, pid in promise_matches.items():
        expected_assessment = case.promesas[gi].valoracion_esperada
        if expected_assessment is None:
            continue
        promises_checked += 1
        got_assessment = promises[pid].assessment
        if got_assessment == expected_assessment:
            promises_correct += 1
        else:
            wrong_promises.append(
                f"{case.promesas[gi].cita}: {expected_assessment} -> {got_assessment}"
            )

    unmatched = [c for c in report.claims if c.id not in set(matches.values())]
    unmatched_promises = [p for p in report.promises if p.id not in set(promise_matches.values())]

    def listed_as_non_claim(quote: str) -> bool:
        return any(_similarity(n, quote) >= MATCH_THRESHOLD for n in case.no_afirmaciones)

    false_positives = sum(1 for c in unmatched if listed_as_non_claim(c.text_original))
    false_positives += sum(1 for p in unmatched_promises if listed_as_non_claim(p.text_original))
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
        speakers_checked=checked,
        speakers_correct=correct,
        wrong_speakers=wrong,
        verdicts_checked=sum(confusion.values()),
        context_checked=context_checked,
        context_correct=context_correct,
        wrong_context=wrong_context,
        promises_gold=len(case.promesas),
        promises_matched=len(promise_matches),
        promises_checked=promises_checked,
        promises_correct=promises_correct,
        missed_promises=[g.cita for gi, g in enumerate(case.promesas) if gi not in promise_matches],
        wrong_promises=wrong_promises,
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
    verdicts_checked: int = 0
    context_checked: int = 0
    context_correct: int = 0
    promises_gold: int = 0
    promises_matched: int = 0
    promises_checked: int = 0
    promises_correct: int = 0

    @property
    def promise_accuracy(self) -> float | None:
        return self.promises_correct / self.promises_checked if self.promises_checked else None

    @property
    def coverage(self) -> float | None:
        return self.matched / self.gold_claims if self.gold_claims else None

    @property
    def verdict_accuracy(self) -> float | None:
        return self.correct / self.verdicts_checked if self.verdicts_checked else None

    @property
    def context_accuracy(self) -> float | None:
        return self.context_correct / self.context_checked if self.context_checked else None


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
        verdicts_checked=sum(r.verdicts_checked for r in results),
        context_checked=sum(r.context_checked for r in results),
        context_correct=sum(r.context_correct for r in results),
        promises_gold=sum(r.promises_gold for r in results),
        promises_matched=sum(r.promises_matched for r in results),
        promises_checked=sum(r.promises_checked for r in results),
        promises_correct=sum(r.promises_correct for r in results),
    )
