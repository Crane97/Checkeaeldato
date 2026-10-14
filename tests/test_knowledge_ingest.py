"""Ingesta automática (fase D): huella, carga validada, ejecuciones y avisos."""

import datetime as dt

import httpx
import pytest

from pipeline.indicators import Indicator, IndicatorSource, Observation
from pipeline.knowledge.catalog import load_catalog, sync_catalog
from pipeline.knowledge.ingest import FETCHERS, IngestError, fingerprint, ingest

from .test_knowledge_schema import db  # noqa: F401  (fixture)

D = dt.date
TODAY = D(2026, 10, 14)
CATALOG = load_catalog()
SPEC = CATALOG["paro_epa"]


def series(values: dict[tuple[int, int], float]) -> Indicator:
    src = IndicatorSource(
        organismo="INE", documento="EPA, serie EPA452434", url="https://www.ine.es/x"
    )
    obs = []
    for (year, q), v in sorted(values.items()):
        start = D(year, 3 * q - 2, 1)
        end = (D(year, 3 * q + 1, 1) if q < 4 else D(year + 1, 1, 1)) - dt.timedelta(days=1)
        obs.append(Observation(desde=start, hasta=end, valor=v, fuente=src))
    return Indicator(
        id="paro_epa",
        nombre="x",
        unidad=SPEC.unidad,
        periodicidad="trimestral",
        origen="ine",
        estado="automatico",
        actualizado=TODAY,
        serie=obs,
    )


def test_the_fingerprint_depends_only_on_the_values():
    a = series({(2026, 1): 10.5, (2026, 2): 10.2})
    b = a.model_copy(update={"actualizado": D(2026, 1, 1)})
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a) != fingerprint(series({(2026, 1): 10.5, (2026, 2): 10.3}))


def test_eurostat_codes_must_carry_their_filters():
    spec = CATALOG["deficit_publico_pib"].model_copy(update={"codigo_fuente": "gov_10dd_edpt1"})
    transport = httpx.MockTransport(lambda r: httpx.Response(500))
    with httpx.Client(transport=transport) as client, pytest.raises(IngestError, match="dataset"):
        FETCHERS["eurostat"](spec, client, TODAY)


def test_eurostat_series_are_read_from_the_catalog_code():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/gov_10dd_edpt1")
        assert request.url.params["na_item"] == "B9" and request.url.params["geo"] == "ES"
        return httpx.Response(
            200,
            json={
                "dimension": {"time": {"category": {"index": {"2023": 0, "2024": 1}}}},
                "value": {"0": -3.5, "1": -3.2},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        ind = FETCHERS["eurostat"](CATALOG["deficit_publico_pib"], client, TODAY)
    assert [(o.desde.year, o.valor) for o in ind.serie] == [(2023, -3.5), (2024, -3.2)]
    assert ind.unidad == CATALOG["deficit_publico_pib"].unidad


def _run(db, values, **kw):  # noqa: F811
    fetchers = {"ine": lambda spec, client, today: series(values)}
    with httpx.Client() as client:
        return ingest(db, {"paro_epa": SPEC}, client, today=TODAY, fetchers=fetchers, **kw)


@pytest.mark.supabase
def test_ingest_loads_once_skips_unchanged_data_and_versions_revisions(db):  # noqa: F811
    sync_catalog(db, CATALOG)
    first = _run(db, {(2026, 1): 10.5, (2026, 2): 10.2})
    [r] = first.results
    assert (r.status, r.summary.new) == ("cargado", 2)
    assert _run(db, {(2026, 1): 10.5, (2026, 2): 10.2}).results[0].status == "sin_cambios"

    revised = _run(db, {(2026, 1): 10.6, (2026, 2): 10.2}).results[0]
    assert (revised.summary.revisions, revised.summary.unchanged) == (1, 1)
    rows = db.execute(
        "select version, vigente, estado from ref.observations where indicator_id = 'paro_epa'"
        " and periodo = '2026T1' order by version"
    ).fetchall()
    assert rows == [(1, False, "aprobado"), (2, True, "aprobado")]
    runs = db.execute(
        "select conector, estado, filas_nuevas, revisiones from ops.ingest_runs order by id"
    ).fetchall()
    assert runs == [("ine", "ok", 2, 0), ("ine", "ok", 0, 0), ("ine", "ok", 0, 1)]


@pytest.mark.supabase
def test_suspicious_values_go_to_quarantine_and_are_reported(db):  # noqa: F811
    sync_catalog(db, CATALOG)
    report = _run(db, {(2026, 1): 10.5, (2026, 2): 30.0})  # +19.5 points in a quarter
    assert report.quarantine == ["paro_epa: 1"] and report.needs_attention()


@pytest.mark.supabase
def test_a_failing_source_is_recorded_and_the_rest_go_on(db):  # noqa: F811
    sync_catalog(db, CATALOG)

    def broken(spec, client, today):
        raise IngestError("INE no responde")

    fetchers = {
        "ine": broken,
        "eurostat": lambda spec, client, today: (_ for _ in ()).throw(IngestError("x")),
    }
    with httpx.Client() as client:
        report = ingest(db, {"paro_epa": SPEC}, client, today=TODAY, fetchers=fetchers)
    assert report.results[0].status == "error" and "INE no responde" in report.errors[0]
    assert db.execute("select estado from ops.ingest_runs").fetchone() == ("error",)


@pytest.mark.supabase
def test_stale_indicators_are_reported(db):  # noqa: F811
    sync_catalog(db, CATALOG)
    report = _run(db, {(2025, 1): 10.5})  # quarter ended 2025-03-31; max 150 days
    assert any(s.startswith("paro_epa: último dato del 2025-03-31") for s in report.stale)


@pytest.mark.supabase
def test_simulation_marks_the_run(db):  # noqa: F811
    sync_catalog(db, CATALOG)
    _run(db, {(2026, 1): 10.5}, simulate=True)
    assert db.execute("select estado from ops.ingest_runs").fetchone() == ("simulacion",)
