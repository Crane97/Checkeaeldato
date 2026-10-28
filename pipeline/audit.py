"""Auditoría de un análisis: qué herramientas usó, cuánto buscó y cuánto costó.

    uv run auditar <job_id | data/<job_id>> [--json]

Lee los archivos del análisis (context.json, evidence.json, verdicts.json, manifest.json) y
no llama a ninguna API. Avisa cuando un análisis gasta mucho del cupo mensual de Tavily
(`TAVILY_MONTHLY_CREDITS`, 1.000 en el plan gratuito) o cuando el mes va cerca del límite.
El total del mes suma los manifest de data/: si un paso se repitió, solo cuenta su última
ejecución, y no incluye lo gastado en otras máquinas (la cifra real está en Tavily).
"""

import datetime as dt
import json
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from pipeline.models import ClaimDetection, EvidenceRun, JobManifest, Verdict

# One analysis spending this share of the month, or the month reaching this share: warn.
ANALYSIS_SHARE_WARNING = 0.05
MONTH_SHARE_WARNING = 0.8
KIND_LABELS = {
    "oficial": "Fuentes oficiales (Tavily o API oficial)",
    "factcheck": "Verificadores (Fact Check API)",
    "web": "Web general (Tavily)",
    "indicador_local": "Base de indicadores oficiales",
}


class JobAudit(BaseModel):
    job_id: str
    speech_date: dt.date
    claims: int = 0  # factual claims (promises apart)
    duplicates: int = 0
    promises: int = 0
    context: dict[str, int] = Field(default_factory=dict)
    searched: int = 0  # claims and promises that went through the evidence step
    evidence_kinds: dict[str, int] = Field(default_factory=dict)  # claims with that kind
    reused_web_facts: int = 0
    without_evidence: int = 0
    evidence_errors: int = 0
    verdicts: dict[str, int] = Field(default_factory=dict)
    cited_kinds: dict[str, int] = Field(default_factory=dict)  # verdicts citing that kind
    basis: dict[str, int] = Field(default_factory=dict)
    follow_up: int = 0
    needs_review: int = 0
    tavily_real: int = 0
    tavily_cached: int = 0
    tavily_credits: int = 0
    tavily_by_step: dict[str, int] = Field(default_factory=dict)  # credits
    indicator_hits: int = 0
    claude_calls: dict[str, int] = Field(default_factory=dict)
    usd_by_step: dict[str, float] = Field(default_factory=dict)
    total_usd: float = 0
    month_credits: int = 0
    monthly_credits: int = 1000
    missing: list[str] = Field(default_factory=list)  # files of a step that did not run
    warnings: list[str] = Field(default_factory=list)

    @property
    def cache_hit_rate(self) -> float:
        total = self.tavily_real + self.tavily_cached
        return self.tavily_cached / total if total else 0.0


def _pct(part: float, whole: float) -> str:
    return f"{100 * part / whole:.1f}".replace(".", ",") + " %" if whole else "—"


def _read(job_dir: Path, name: str, adapter: TypeAdapter, missing: list[str]):
    path = job_dir / name
    if not path.is_file():
        missing.append(name)
        return None
    return adapter.validate_json(path.read_text())


def _manifests(data_dir: Path) -> list[JobManifest]:
    found = []
    for path in data_dir.glob("*/manifest.json"):
        try:
            found.append(JobManifest.model_validate_json(path.read_text()))
        except (OSError, ValidationError):
            continue  # not a job folder, or an old format: it cannot be counted
    return found


def month_credits(data_dir: Path, day: dt.date) -> int:
    """Tavily credits of the steps that finished in `day`'s month, in every job of data/."""
    total = 0
    for manifest in _manifests(data_dir):
        for record in manifest.steps.values():
            if (record.finished_at.year, record.finished_at.month) != (day.year, day.month):
                continue
            total += sum(e.credits for e in record.cost if e.provider == "tavily")
    return total


def audit_job(
    job_dir: Path, *, monthly_credits: int = 1000, today: dt.date | None = None
) -> JobAudit:
    manifest = JobManifest.model_validate_json((job_dir / "manifest.json").read_text())
    a = JobAudit(
        job_id=manifest.job_id, speech_date=manifest.speech_date, monthly_credits=monthly_credits
    )
    detection = _read(job_dir, "context.json", TypeAdapter(ClaimDetection), a.missing)
    evidence = _read(job_dir, "evidence.json", TypeAdapter(EvidenceRun), a.missing)
    verdicts = _read(job_dir, "verdicts.json", TypeAdapter(list[Verdict]), a.missing) or []

    if detection is not None:
        claims = [c for c in detection.claims if c.kind != "promesa"]
        a.claims, a.promises = len(claims), len(detection.claims) - len(claims)
        a.duplicates = sum(bool(c.duplicate_of) for c in claims)
        a.context = dict(Counter(c.context_status for c in claims))
    if evidence is not None:
        a.searched = len(evidence.claims)
        kinds: Counter[str] = Counter()
        for c in evidence.claims:
            kinds.update({i.source.kind for i in c.items})
            a.reused_web_facts += any(i.web_fact_id for i in c.items)
            a.without_evidence += not c.items
            a.evidence_errors += bool(c.errors)
        a.evidence_kinds = dict(kinds.most_common())
    cited: Counter[str] = Counter()
    for v in verdicts:
        cited.update({s.kind for s in v.sources})
    a.cited_kinds = dict(cited.most_common())
    a.verdicts = dict(Counter(v.verdict for v in verdicts).most_common())
    a.basis = dict(Counter(v.basis or "sin fuente" for v in verdicts).most_common())
    a.follow_up = sum(bool(v.follow_up_queries) for v in verdicts)
    a.needs_review = sum(v.needs_review for v in verdicts)

    for step, record in manifest.steps.items():
        for e in record.cost:
            a.usd_by_step[step] = round(a.usd_by_step.get(step, 0) + e.usd, 4)
            if e.provider == "tavily":
                a.tavily_real += e.searches
                a.tavily_cached += e.cached_searches
                a.tavily_credits += e.credits
                a.tavily_by_step[step] = a.tavily_by_step.get(step, 0) + e.credits
            elif e.provider == "indicadores":
                a.indicator_hits += e.indicator_hits
            else:
                a.claude_calls[step] = a.claude_calls.get(step, 0) + 1
    a.usd_by_step = {k: v for k, v in a.usd_by_step.items() if v}
    a.total_usd = round(sum(a.usd_by_step.values()), 4)

    a.month_credits = month_credits(job_dir.parent, today or dt.datetime.now(dt.UTC).date())
    if a.tavily_credits >= ANALYSIS_SHARE_WARNING * monthly_credits:
        a.warnings.append(
            f"Este análisis gastó {a.tavily_credits} créditos de Tavily: el"
            f" {_pct(a.tavily_credits, monthly_credits)} de los {monthly_credits} del mes"
        )
    if a.month_credits >= MONTH_SHARE_WARNING * monthly_credits:
        a.warnings.append(
            f"Llevas {a.month_credits} de {monthly_credits} créditos de Tavily este mes"
            f" ({_pct(a.month_credits, monthly_credits)}, según los análisis de esta máquina)"
        )
    return a


def _table(title: str, counts: dict[str, int], whole: int, labels: dict[str, str]) -> None:
    typer.secho(f"\n{title}", bold=True)
    if not counts:
        typer.echo("   (ninguno)")
    for key, n in counts.items():
        typer.echo(f"   {labels.get(key, key):42} {n:4}   {_pct(n, whole):>8}")


def render(a: JobAudit) -> None:
    typer.secho(f"Auditoría del análisis {a.job_id} (discurso del {a.speech_date})", bold=True)
    typer.echo(
        f"Afirmaciones: {a.claims} ({a.duplicates} repetidas) · promesas: {a.promises} · "
        f"contexto: {', '.join(f'{k} {v}' for k, v in a.context.items()) or '—'}"
    )
    if a.missing:
        typer.secho(f"Sin datos de: {', '.join(a.missing)} (paso sin ejecutar)", fg="yellow")
    extra = {"(dato web reutilizado)": a.reused_web_facts, "(sin evidencia)": a.without_evidence}
    _table(
        f"Evidencia recogida (afirmaciones y promesas con cada tipo, de {a.searched})",
        {**a.evidence_kinds, **{k: v for k, v in extra.items() if v}},
        a.searched,
        KIND_LABELS,
    )
    total = sum(a.verdicts.values())
    _table(f"Fuentes citadas en el veredicto (de {total})", a.cited_kinds, total, KIND_LABELS)
    _table("Veredictos", a.verdicts, total, {})
    _table("Base del veredicto", a.basis, total, {})
    typer.echo(
        f"\nRonda de búsqueda adicional: {a.follow_up} de {total} ({_pct(a.follow_up, total)})"
        f" · para revisar: {a.needs_review} · errores de evidencia: {a.evidence_errors}"
    )
    typer.secho("\nBúsquedas", bold=True)
    typer.echo(
        f"   Tavily: {a.tavily_real} reales ({a.tavily_credits} créditos), {a.tavily_cached}"
        f" desde caché ({_pct(a.tavily_cached, a.tavily_real + a.tavily_cached)} de aciertos)"
    )
    if a.tavily_by_step:
        typer.echo(
            "   Créditos por paso: " + ", ".join(f"{k} {v}" for k, v in a.tavily_by_step.items())
        )
    typer.echo(f"   Base de indicadores: {a.indicator_hits} afirmaciones sin buscar en la web")
    typer.echo(
        f"   Este mes (análisis de esta máquina): {a.month_credits} de {a.monthly_credits}"
        f" créditos ({_pct(a.month_credits, a.monthly_credits)})"
    )
    typer.secho("\nClaude", bold=True)
    typer.echo(
        "   Llamadas por paso: " + (", ".join(f"{k} {v}" for k, v in a.claude_calls.items()) or "—")
    )
    typer.echo(
        f"   Coste: {a.total_usd:.2f} $ ("
        + ", ".join(f"{k} {v:.2f}" for k, v in a.usd_by_step.items())
        + ")"
    )
    for w in a.warnings:
        typer.secho(f"\nAviso: {w}", fg="yellow", bold=True)


app = typer.Typer(add_completion=False)


@app.command()
def main(
    trabajo: Annotated[str, typer.Argument(help="job_id o carpeta data/<job_id>")],
    datos: Annotated[Path | None, typer.Option("--datos", help="Carpeta de datos")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Salida en JSON")] = False,
) -> None:
    """Qué herramientas usó un análisis, cuánto buscó en Tavily y cuánto costó."""
    from pipeline.config import get_settings

    settings = get_settings()
    job_dir = Path(trabajo)
    if not (job_dir / "manifest.json").is_file():
        job_dir = (datos or settings.data_dir) / trabajo
    if not (job_dir / "manifest.json").is_file():
        typer.secho(f"No existe el análisis {trabajo} (falta manifest.json)", fg="red", err=True)
        raise typer.Exit(code=1)
    try:
        a = audit_job(job_dir, monthly_credits=settings.tavily_monthly_credits)
    except (OSError, ValidationError) as exc:
        typer.secho(f"No se pudo leer el análisis: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc
    if as_json:
        typer.echo(json.dumps(a.model_dump(mode="json"), ensure_ascii=False, indent=2))
    else:
        render(a)
