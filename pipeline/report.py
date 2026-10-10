"""Agregación de resultados en report.json y resumen para la terminal."""

import datetime as dt

from pipeline.models import (
    VERDICT_LABELS,
    Claim,
    ClaimDetection,
    InputKind,
    Report,
    RunCost,
    Verdict,
)

VERDICT_NAMES = {
    "verdadero": "verdadero",
    "enganoso": "engañoso",
    "falso": "falso",
    "no_verificable": "no verificable",
}
BASIS_NAMES = {
    "factcheck": "desmentido publicado",
    "oficial": "oficial",
    "web": "solo web",
    "sin_fuente": "sin fuente",
}


class ReportError(RuntimeError):
    """Step outputs do not fit together."""


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _warnings(detection: ClaimDetection, verdicts: list[Verdict]) -> list[str]:
    warnings = [
        f"{_plural(1, 'bloque', 'bloques')} del texto sin analizar "
        f"(segmentos {f.segments[0]}-{f.segments[-1]}): {f.error}"
        for f in detection.failed_blocks
    ]
    if detection.rejected:
        n = len(detection.rejected)
        warnings.append(
            f"{_plural(n, 'cita descartada', 'citas descartadas')} por no ser literal "
            "o no localizarse (ver claims.json)"
        )
    failed = sum(1 for v in verdicts if v.analysis_error)
    if failed:
        warnings.append(
            f"{_plural(failed, 'afirmación sin analizar', 'afirmaciones sin analizar')} "
            "por un error del análisis (marcadas con analysis_error)"
        )
    return warnings


def build_report(
    *,
    job_id: str,
    source: str,
    input_kind: InputKind,
    speech_date: dt.date,
    speakers: dict[str, str],
    detection: ClaimDetection,
    verdicts: list[Verdict],
    cost: RunCost,
    generated_at: dt.datetime,
) -> Report:
    by_claim = {v.claim_id: v for v in verdicts}
    claims: list[Claim] = []
    for detected in detection.claims:
        verdict = by_claim.get(detected.id)
        if verdict is None:
            raise ReportError(f"Falta el veredicto de la afirmación {detected.id}")
        claims.append(
            Claim(
                **detected.model_dump(exclude={"search_queries"}),
                **verdict.model_dump(exclude={"claim_id", "basis"}),
            )
        )
    return Report(
        job_id=job_id,
        source=source,
        input_kind=input_kind,
        speech_date=speech_date,
        generated_at=generated_at,
        speakers=speakers,
        claims=claims,
        cost=cost,
        warnings=_warnings(detection, verdicts),
    )


def format_summary(report: Report, report_path: str) -> str:
    """Plain-text summary for the terminal."""
    lines = [
        f"Informe: {report_path}",
        f"Fuente: {report.source} ({report.input_kind}, {report.speech_date.isoformat()})",
        "",
        f"Afirmaciones verificables: {len(report.claims)}",
    ]
    lines += [f"  {VERDICT_NAMES[k]:<16}{report.by_verdict[k]:>4}" for k in VERDICT_LABELS]

    if report.by_speaker:
        lines += ["", "Por orador:"]
        for speaker, counts in report.by_speaker.items():
            parts = [f"{VERDICT_NAMES[k]} {n}" for k, n in counts.items() if n]
            lines.append(f"  {speaker}: {', '.join(parts)}")

    if report.claims:
        basis = ", ".join(f"{BASIS_NAMES[k]}: {n}" for k, n in report.by_basis.items() if n)
        lines += ["", f"Base de los veredictos: {basis}"]

    if report.warnings:
        lines += ["", "Avisos:"] + [f"  - {w}" for w in report.warnings]

    llm = sum(e.usd for e in report.cost.entries if e.provider == "anthropic")
    calls = sum(1 for e in report.cost.entries if e.provider == "anthropic")
    audio = sum(e.usd for e in report.cost.entries if e.provider != "anthropic")
    lines += [
        "",
        f"Coste estimado: {report.cost.total_usd:.4f} $ "
        f"(Claude: {llm:.4f} $ en {_plural(calls, 'llamada', 'llamadas')}; "
        f"transcripción: {audio:.4f} $)",
    ]
    return "\n".join(lines)
