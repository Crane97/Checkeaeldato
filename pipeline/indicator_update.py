"""Descarga de las series automáticas de la base de indicadores (INE, Eurostat, Seguridad Social).

Se ejecuta a mano con `uv run python scripts/actualizar_indicadores.py`, no durante un
análisis: así un análisis nunca depende de que estas APIs respondan, y cada actualización
queda revisada en git. SMI, pensiones y pensionistas se mantienen a mano (BOE / INSS).
"""

import calendar
import csv
import datetime as dt
import io
import logging
from dataclasses import dataclass, field
from typing import Literal
from zoneinfo import ZoneInfo

import httpx

from pipeline.http import request_with_retries
from pipeline.indicators import Indicator, IndicatorSource, Observation

logger = logging.getLogger(__name__)

INE_URL = "https://servicios.ine.es/wstempus/js/ES/DATOS_SERIE/{cod}?nult=1000"
INE_TABLE_URL = "https://www.ine.es/jaxiT3/Tabla.htm?t={tabla}"
EUROSTAT_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
EUROSTAT_VIEW_URL = "https://ec.europa.eu/eurostat/databrowser/view/{dataset}/default/table"
SS_URL = "https://w6.seg-social.es/PXWeb/sq/{query}"
# Afiliados medios por sector: CNAE-09 (ene-2008 a dic-2025) and CNAE-25 (since ene-2021).
SS_AFFILIATION_QUERIES = (
    "3fabe408-6211-46c0-93ad-ee78351cbdc7",
    "edad3af0-7a02-4595-b991-942b90f704c2",
)
SINCE_YEAR = 2000
MADRID = ZoneInfo("Europe/Madrid")
# Both classifications must give the same total where they overlap.
MAX_OVERLAP_GAP = 0.005

Periodicity = Literal["mensual", "trimestral", "anual", "puntual"]


class IndicatorUpdateError(RuntimeError):
    """A source answered with something that cannot be turned into a series."""


@dataclass(frozen=True)
class IneSpec:
    id: str
    nombre: str
    unidad: str
    cod: str  # INE series code
    tabla: int  # INE table, for the human-readable link
    documento: str
    periodicidad: Periodicity
    notas: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EurostatSpec:
    id: str
    nombre: str
    unidad: str
    dataset: str
    filtros: dict[str, str]
    notas: list[str] = field(default_factory=list)


SPECS: list[IneSpec | EurostatSpec] = [
    IneSpec(
        id="paro_epa",
        nombre="Tasa de paro (EPA)",
        unidad="% de la población activa",
        cod="EPA452434",
        tabla=65334,
        documento="Encuesta de Población Activa (EPA)",
        periodicidad="trimestral",
        notas=[
            "Paro según la Encuesta de Población Activa del INE (definición de la OIT), no paro "
            "registrado en el SEPE, que es otra estadística con cifras distintas.",
            "Dato trimestral, sin desestacionalizar.",
        ],
    ),
    IneSpec(
        id="paro_juvenil_epa",
        nombre="Tasa de paro de los menores de 25 años (EPA)",
        unidad="% de la población activa menor de 25 años",
        cod="EPA452436",
        tabla=65334,
        documento="Encuesta de Población Activa (EPA)",
        periodicidad="trimestral",
        notas=[
            "Menores de 25 años (16 a 24). No es el porcentaje de jóvenes en paro sobre todos "
            "los jóvenes, sino sobre los jóvenes activos (que trabajan o buscan empleo).",
            "Dato trimestral, sin desestacionalizar.",
        ],
    ),
    IneSpec(
        id="ipc_interanual",
        nombre="IPC general, variación anual",
        unidad="% respecto al mismo mes del año anterior",
        cod="IPC290750",
        tabla=76125,
        documento="Índice de Precios de Consumo (IPC)",
        periodicidad="mensual",
        notas=["Inflación general. No es la inflación subyacente ni el IPCA armonizado."],
    ),
    IneSpec(
        id="poblacion",
        nombre="Población residente en España",
        unidad="personas",
        cod="ECP320",
        tabla=56934,
        documento="Estadística Continua de Población (ECP)",
        periodicidad="puntual",
        notas=["Población residente en la fecha indicada (no media anual)."],
    ),
    EurostatSpec(
        id="pib",
        nombre="Producto interior bruto (PIB) a precios corrientes",
        unidad="millones de euros",
        dataset="nama_10_gdp",
        filtros={"unit": "CP_MEUR", "na_item": "B1GQ"},
        notas=["PIB nominal anual (precios corrientes). Los últimos años pueden revisarse."],
    ),
    EurostatSpec(
        id="pib_crecimiento",
        nombre="Crecimiento del PIB en volumen",
        unidad="% de variación anual",
        dataset="nama_10_gdp",
        filtros={"unit": "CLV_PCH_PRE", "na_item": "B1GQ"},
        notas=["Crecimiento real (descontada la inflación) respecto al año anterior."],
    ),
    EurostatSpec(
        id="deuda_pib",
        nombre="Deuda pública (Protocolo de Déficit Excesivo)",
        unidad="% del PIB",
        dataset="gov_10dd_edpt1",
        filtros={"unit": "PC_GDP", "sector": "S13", "na_item": "GD"},
        notas=[
            "Deuda de las Administraciones Públicas según el PDE, la misma que publica el "
            "Banco de España. Dato a final de año."
        ],
    ),
    EurostatSpec(
        id="deuda_absoluta",
        nombre="Deuda pública (Protocolo de Déficit Excesivo)",
        unidad="millones de euros",
        dataset="gov_10dd_edpt1",
        filtros={"unit": "MIO_EUR", "sector": "S13", "na_item": "GD"},
        notas=["Saldo a final de año de la deuda de las Administraciones Públicas según el PDE."],
    ),
    EurostatSpec(
        id="deficit_pib",
        nombre="Saldo de las Administraciones Públicas (déficit o superávit)",
        unidad="% del PIB",
        dataset="gov_10dd_edpt1",
        filtros={"unit": "PC_GDP", "sector": "S13", "na_item": "B9"},
        notas=[
            "Capacidad o necesidad de financiación: un valor negativo es déficit "
            "(−3,5 = déficit del 3,5 % del PIB)."
        ],
    ),
]


def _end(start: dt.date, periodicity: Periodicity) -> dt.date:
    if periodicity == "puntual":
        return start
    if periodicity == "anual":
        return dt.date(start.year, 12, 31)
    months = 3 if periodicity == "trimestral" else 1
    last_month = start.month + months - 1
    return dt.date(start.year, last_month, calendar.monthrange(start.year, last_month)[1])


def _get(client: httpx.Client, url: str, **kwargs) -> httpx.Response:
    response = request_with_retries(
        client, "GET", url, max_attempts=3, backoff=1.0, error_cls=IndicatorUpdateError, **kwargs
    )
    if response.status_code != 200:
        raise IndicatorUpdateError(f"{url}: HTTP {response.status_code}")
    return response


def _ine_start(point: dict) -> dt.date | None:
    """First day of the period: from `Fecha`, or from year and period code (the API omits
    `Fecha` before 1970). Period codes: 1-12 months, 19-22 quarters."""
    if "Fecha" in point:
        return dt.datetime.fromtimestamp(point["Fecha"] / 1000, MADRID).date()
    period, year = point.get("FK_Periodo"), point.get("Anyo")
    if not isinstance(year, int) or not isinstance(period, int):
        return None
    if 1 <= period <= 12:
        return dt.date(year, period, 1)
    if 19 <= period <= 22:
        return dt.date(year, 1 + 3 * (period - 19), 1)
    return None


def build_ine(spec: IneSpec, client: httpx.Client, *, today: dt.date) -> Indicator:
    data = _get(client, INE_URL.format(cod=spec.cod)).json()
    points = data.get("Data") if isinstance(data, dict) else None
    if not points:
        raise IndicatorUpdateError(f"INE {spec.cod}: sin datos")
    source_doc = f"{spec.documento}, serie {spec.cod}"
    url = INE_TABLE_URL.format(tabla=spec.tabla)
    serie = []
    for p in points:
        if p.get("Valor") is None:
            continue
        start = _ine_start(p)
        if start is None or start.year < SINCE_YEAR:
            continue
        serie.append(
            Observation(
                desde=start,
                hasta=_end(start, spec.periodicidad),
                valor=float(p["Valor"]),
                fuente=IndicatorSource(organismo="INE", documento=source_doc, url=url),
            )
        )
    serie.sort(key=lambda o: o.desde)
    return Indicator(
        id=spec.id,
        nombre=spec.nombre,
        unidad=spec.unidad,
        periodicidad=spec.periodicidad,
        origen="ine",
        estado="automatico",
        actualizado=today,
        notas=spec.notas,
        serie=serie,
    )


def build_eurostat(spec: EurostatSpec, client: httpx.Client, *, today: dt.date) -> Indicator:
    params = {"geo": "ES", "sinceTimePeriod": str(SINCE_YEAR), **spec.filtros}
    data = _get(client, EUROSTAT_URL + spec.dataset, params=params).json()
    try:
        years = data["dimension"]["time"]["category"]["index"]
        values = data["value"]
    except (KeyError, TypeError) as exc:
        raise IndicatorUpdateError(f"Eurostat {spec.dataset}: respuesta inesperada") from exc
    if len(years) < len(values):
        raise IndicatorUpdateError(f"Eurostat {spec.dataset}: los filtros no fijan una sola serie")
    url = EUROSTAT_VIEW_URL.format(dataset=spec.dataset)
    filters = ", ".join(f"{k}={v}" for k, v in spec.filtros.items())
    serie = [
        Observation(
            desde=dt.date(int(year), 1, 1),
            hasta=dt.date(int(year), 12, 31),
            valor=float(values[str(pos)]),
            fuente=IndicatorSource(
                organismo="Eurostat",
                documento=f"Eurostat {spec.dataset} ({filters}, España)",
                url=url,
            ),
        )
        for year, pos in sorted(years.items(), key=lambda kv: kv[1])
        if str(pos) in values
    ]
    return Indicator(
        id=spec.id,
        nombre=spec.nombre,
        unidad=spec.unidad,
        periodicidad="anual",
        origen="eurostat",
        estado="automatico",
        actualizado=today,
        notas=spec.notas,
        serie=serie,
    )


def parse_ss_csv(raw: bytes) -> dict[tuple[int, int], float]:
    """Monthly totals ((year, month) -> afiliados medios) from a Seguridad Social PxWeb CSV."""
    text = raw.decode("cp1252")
    lines = [line for line in text.splitlines() if line.startswith('"') and ";" in line]
    totals: dict[tuple[int, int], float] = {}
    for row in csv.DictReader(io.StringIO("\n".join(lines)), delimiter=";"):
        if row.get("Sector") != "Total":
            continue
        period = row["Periodo"]
        totals[(int(period[:4]), int(period[4:6]))] = float(row["Total"])
    if not totals:
        raise IndicatorUpdateError("CSV de la Seguridad Social sin filas «Total»")
    return totals


def build_affiliation(
    client: httpx.Client, *, today: dt.date, queries: tuple[str, str] = SS_AFFILIATION_QUERIES
) -> Indicator:
    old, new = (parse_ss_csv(_get(client, SS_URL.format(query=q)).content) for q in queries)
    for month in sorted(set(old) & set(new)):
        gap = abs(old[month] - new[month]) / old[month]
        if gap > MAX_OVERLAP_GAP:
            raise IndicatorUpdateError(
                f"Afiliación {month}: las dos clasificaciones difieren un {gap:.1%}"
            )
    merged = {**old, **new}  # the current classification wins where both exist
    source = {
        q: IndicatorSource(
            organismo="Tesorería General de la Seguridad Social",
            documento="Afiliados medios por dependencia laboral y sector de actividad "
            + ("(CNAE-09)" if i == 0 else "(CNAE-25)"),
            url=SS_URL.format(query=q),
        )
        for i, q in enumerate(queries)
    }
    serie = []
    for (year, month), value in sorted(merged.items()):
        start = dt.date(year, month, 1)
        serie.append(
            Observation(
                desde=start,
                hasta=_end(start, "mensual"),
                valor=round(value),
                fuente=source[queries[1]] if (year, month) in new else source[queries[0]],
            )
        )
    return Indicator(
        id="afiliacion",
        nombre="Afiliados medios a la Seguridad Social",
        unidad="personas (media mensual)",
        periodicidad="mensual",
        origen="seguridad_social",
        estado="automatico",
        actualizado=today,
        notas=[
            "Media mensual de afiliados en alta (todos los regímenes). Una persona con dos "
            "empleos cuenta dos veces; no es el número de ocupados de la EPA.",
            "Sin desestacionalizar.",
        ],
        serie=serie,
    )
