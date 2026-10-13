"""Evalúa el pipeline contra el gold set (eval/gold/*.json).

    uv run python eval/run_eval.py                    # modo completo (detección + veredicto)
    uv run python eval/run_eval.py --modo veredicto   # solo veredicto, desde las citas gold
    uv run python eval/run_eval.py --caso ejemplo-sintetico-001

El modo completo reutiliza los trabajos ya calculados en data/eval/jobs (solo vuelve a
llamar a las APIs si cambian los prompts o la entrada). El modo veredicto llama siempre a
las fuentes y a Claude. Los resultados se guardan en data/eval/resultados-<fecha>.json
junto con el hash de los prompts, para comparar antes/después de un cambio.
"""

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Annotated

import typer

from pipeline.config import get_settings
from pipeline.evaluation import (
    GOLD_DIR,
    CaseResult,
    GoldCase,
    load_gold_cases,
    score_case,
    summarize,
)
from pipeline.evidence.base import collect_evidence
from pipeline.jobs import prompt_hash
from pipeline.models import Claim, DetectedClaim, Promise, Report, RunCost, Verdict
from pipeline.promises import assess_promises
from pipeline.report import format_summary
from pipeline.run import PROMPTS_DIR, LiveDeps, follow_up_search, run_job
from pipeline.verdict import decide_verdicts

EVAL_DIR = Path(__file__).resolve().parent.parent / "data" / "eval"

app = typer.Typer(add_completion=False)


def _input_file(case: GoldCase) -> Path:
    if case.entrada.tipo == "video":
        return Path(__file__).resolve().parent.parent / str(case.entrada.ruta)
    path = EVAL_DIR / "entradas" / f"{case.id}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(case.entrada.texto or "")
    return path


def _run_full(case: GoldCase, deps: LiveDeps) -> Report:
    result = run_job(
        _input_file(case),
        speech_date=case.fecha,
        data_dir=EVAL_DIR / "jobs",
        deps=deps,
    )
    typer.echo(format_summary(result.report, report_path=str(result.report_path)))
    return result.report


def _run_verdict_only(case: GoldCase, deps: LiveDeps) -> Report:
    detected = [
        DetectedClaim(
            id=f"g{n:03d}",
            speaker="gold",
            text_original=g.cita,
            text_normalized=g.texto_normalizado or g.cita,
            claim_type="otro",
            start=g.inicio,
            end=g.fin,
            search_queries=[g.texto_normalizado or g.cita],
        )
        for n, g in enumerate(case.afirmaciones, start=1)
    ]
    evidence = collect_evidence(detected, deps.evidence_sources, case.fecha)
    verdicts = decide_verdicts(
        detected,
        evidence,
        speech_date=case.fecha,
        llm=deps.llm,
        model=deps.model_for("verdict"),
        follow_up=follow_up_search(deps, case.fecha),
    )
    by_id = {v.claim_id: v for v in verdicts}
    claims = [
        Claim(
            **d.model_dump(exclude={"search_queries", "needs_context"}),
            **by_id[d.id].model_dump(
                exclude={"claim_id", "basis", "category", *Verdict.AUDIT_FIELDS}
            ),
        )
        for d in detected
    ]
    gold_promises = [
        DetectedClaim(
            id=f"p{n:03d}",
            speaker="gold",
            kind="promesa",
            text_original=g.cita,
            text_normalized=g.cita,
            claim_type="otro",
            start=g.inicio,
            end=g.fin,
            search_queries=[g.cita],
        )
        for n, g in enumerate(case.promesas, start=1)
    ]
    assessed = assess_promises(
        gold_promises,
        collect_evidence(gold_promises, deps.evidence_sources, case.fecha),
        speech_date=case.fecha,
        llm=deps.llm,
        model=deps.model_for("verdict"),
    )
    by_promise = {a.claim_id: a for a in assessed}
    promises = [
        Promise(
            **d.model_dump(exclude={"search_queries", "needs_context"}),
            **by_promise[d.id].model_dump(exclude={"claim_id", "basis"}),
        )
        for d in gold_promises
    ]
    return Report(
        job_id=f"eval-{case.id}",
        source=case.id,
        input_kind=case.entrada.tipo,
        speech_date=case.fecha,
        generated_at=dt.datetime.now(dt.UTC),
        claims=claims,
        promises=promises,
        cost=RunCost(entries=[*deps.llm.take_costs(), *deps.take_search_cost("evidence")]),
    )


def _rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _print_case(result: CaseResult) -> None:
    tag = " (sintético)" if result.synthetic else ""
    typer.echo(
        f"\n[{result.case_id}{tag}] detectadas {result.matched}/{result.gold_claims}, "
        f"veredictos correctos {result.correct}/{result.verdicts_checked}, "
        f"opiniones detectadas por error {result.false_positives}, "
        f"extra {result.extra_detected}"
    )
    if result.context_checked:
        typer.echo(f"  contexto correcto {result.context_correct}/{result.context_checked}")
    for wrong in result.wrong_context:
        typer.echo(f"  contexto incorrecto: {wrong}")
    if result.speakers_checked:
        typer.echo(f"  oradores correctos {result.speakers_correct}/{result.speakers_checked}")
    for wrong in result.wrong_speakers:
        typer.echo(f"  orador incorrecto: {wrong}")
    for quote in result.missed:
        typer.echo(f"  no detectada: «{quote}»")
    if result.promises_gold:
        typer.echo(
            f"  promesas detectadas {result.promises_matched}/{result.promises_gold}, "
            f"valoración correcta {result.promises_correct}/{result.promises_checked}"
        )
    for quote in result.missed_promises:
        typer.echo(f"  promesa no detectada: «{quote}»")
    for wrong in result.wrong_promises:
        typer.echo(f"  promesa mal valorada: {wrong}")
    for (expected, got), n in sorted(result.confusion.items()):
        if expected != got:
            typer.echo(f"  esperado {expected}, obtenido {got}: {n}")


@app.command()
def main(
    modo: Annotated[str, typer.Option(help="completo | veredicto")] = "completo",
    caso: Annotated[list[str] | None, typer.Option(help="Solo estos ids de caso")] = None,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="No reutiliza búsquedas guardadas")
    ] = False,
) -> None:
    if modo not in ("completo", "veredicto"):
        raise typer.BadParameter("modo debe ser 'completo' o 'veredicto'")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for noisy in ("httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cases = [c for c in load_gold_cases(GOLD_DIR) if not caso or c.id in caso]
    if not cases:
        raise typer.BadParameter("no hay casos que evaluar")
    deps = LiveDeps(get_settings(), use_cache=not no_cache)
    runner = _run_full if modo == "completo" else _run_verdict_only

    results = [score_case(c, runner(c, deps)) for c in cases]
    for r in results:
        _print_case(r)

    summary = summarize(results)
    typer.echo(
        f"\nRESUMEN ({modo}, {summary.cases} casos): cobertura {_rate(summary.coverage)} "
        f"({summary.matched}/{summary.gold_claims}), precisión de veredictos "
        f"{_rate(summary.verdict_accuracy)} ({summary.correct}/{summary.verdicts_checked}), "
        f"contexto {_rate(summary.context_accuracy)} "
        f"({summary.context_correct}/{summary.context_checked}), "
        f"opiniones detectadas por error {summary.false_positives}"
    )
    if summary.promises_gold:
        typer.echo(
            f"Promesas: detectadas {summary.promises_matched}/{summary.promises_gold}, "
            f"valoración {_rate(summary.promise_accuracy)} "
            f"({summary.promises_correct}/{summary.promises_checked})"
        )
    typer.echo(
        f"Búsquedas Tavily: {deps.searches.real} reales ({deps.searches.credits} créditos), "
        f"{deps.searches.cached} desde caché"
    )
    if any(r.synthetic for r in results):
        typer.echo("Aviso: hay casos sintéticos; sus métricas no miden calidad real.")

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    out = EVAL_DIR / f"resultados-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(
        json.dumps(
            {
                "modo": modo,
                "prompts": {
                    p.name: prompt_hash(PROMPTS_DIR, p.name) for p in PROMPTS_DIR.glob("*.md")
                },
                "modelos": {
                    "deteccion": deps.model_for("detection"),
                    "veredicto": deps.model_for("verdict"),
                },
                "resumen": {
                    "cobertura": summary.coverage,
                    "precision_veredictos": summary.verdict_accuracy,
                    "precision_contexto": summary.context_accuracy,
                    "afirmaciones_gold": summary.gold_claims,
                    "emparejadas": summary.matched,
                    "correctas": summary.correct,
                    "opiniones_detectadas": summary.false_positives,
                    "extra": summary.extra_detected,
                    "promesas_gold": summary.promises_gold,
                    "promesas_detectadas": summary.promises_matched,
                    "precision_promesas": summary.promise_accuracy,
                },
                "casos": [
                    {
                        "id": r.case_id,
                        "sintetico": r.synthetic,
                        "emparejadas": r.matched,
                        "afirmaciones_gold": r.gold_claims,
                        "correctas": r.correct,
                        "no_detectadas": r.missed,
                        "confusion": {f"{e}->{g}": n for (e, g), n in r.confusion.items()},
                    }
                    for r in results
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    typer.echo(f"Resultados guardados en {out}")


if __name__ == "__main__":
    app()
