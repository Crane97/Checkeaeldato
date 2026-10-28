"""Auditoría de un análisis: herramientas usadas, búsquedas, coste y aviso de créditos."""

import datetime as dt
import json

from typer.testing import CliRunner

from pipeline.audit import app, audit_job, month_credits
from pipeline.costs import search_cost

from .test_run import ARTICLE, FakeDeps, run


class TavilyDeps(FakeDeps):
    """Fake run that spent `credits` Tavily credits in the evidence step."""

    def __init__(self, credits: int) -> None:
        super().__init__()
        self.credits = credits

    def take_search_cost(self, step: str):
        if step != "evidence":
            return []
        return [search_cost(step, real=self.credits, cached=2, credits=self.credits)]


def _job(tmp_path, credits=5):
    tmp_path.mkdir(parents=True, exist_ok=True)
    article = tmp_path / "articulo.txt"
    article.write_text(ARTICLE)
    result = run(article, tmp_path / "data", TavilyDeps(credits))
    return tmp_path / "data" / result.report.job_id


def test_the_audit_counts_tools_verdicts_and_searches(tmp_path):
    a = audit_job(_job(tmp_path))
    assert a.claims == 1 and a.searched == 1
    assert sum(a.evidence_kinds.values()) >= 1 and set(a.evidence_kinds) <= {
        "factcheck",
        "oficial",
        "web",
        "indicador_local",
    }
    assert sum(a.verdicts.values()) == 1
    assert (a.tavily_real, a.tavily_cached, a.tavily_credits) == (5, 2, 5)
    assert a.cache_hit_rate == 2 / 7
    assert a.claude_calls and a.total_usd >= 0


def test_a_costly_analysis_gets_a_credits_warning(tmp_path):
    cheap = audit_job(_job(tmp_path / "a", credits=5), monthly_credits=1000)
    assert cheap.warnings == []
    costly = audit_job(_job(tmp_path / "b", credits=196), monthly_credits=1000)
    assert any("196 créditos" in w and "19,6 %" in w for w in costly.warnings)


def test_the_month_adds_up_every_analysis_in_data(tmp_path):
    _job(tmp_path, credits=30)
    other = tmp_path / "otro.txt"
    other.write_text("Otro texto.\n\nEl paro ha bajado un 20 % este año.\n")
    run(other, tmp_path / "data", TavilyDeps(50))
    today = dt.datetime.now(dt.UTC).date()
    assert month_credits(tmp_path / "data", today) == 80
    assert month_credits(tmp_path / "data", today.replace(year=today.year - 1)) == 0


def test_the_monthly_allowance_warning(tmp_path):
    job = _job(tmp_path, credits=90)
    a = audit_job(job, monthly_credits=100)
    assert any("este mes" in w and "90 de 100" in w for w in a.warnings)


def test_the_command_prints_the_audit_and_accepts_an_id_or_a_folder(tmp_path):
    job = _job(tmp_path, credits=196)
    for target in (str(job), job.name):
        result = CliRunner().invoke(app, [target, "--datos", str(tmp_path / "data")])
        assert result.exit_code == 0, result.output
        assert "Tavily" in result.output and "196" in result.output
    result = CliRunner().invoke(app, [str(job), "--json"])
    assert json.loads(result.output)["tavily_credits"] == 196
    result = CliRunner().invoke(app, ["no-existe", "--datos", str(tmp_path / "data")])
    assert result.exit_code == 1
