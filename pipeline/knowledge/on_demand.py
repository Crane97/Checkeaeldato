"""Consulta a la API oficial bajo demanda (fase F1).

Si la base local no cubre el periodo de una afirmación (p. ej. datos de los años 90) y el
indicador tiene conector automático (INE, Eurostat), se descarga la serie completa en ese
momento. Antes de usarla se valida con las mismas reglas que la ingesta: los valores que
irían a cuarentena (fuera de rango o con saltos anómalos) se descartan. Es evidencia
oficial pero no revisada ni fechada: el extracto dice que son los valores ACTUALES de la
API (pueden incluir revisiones posteriores al discurso). No se guarda en la base: eso lo
hace la ingesta, con su control.
"""

import datetime as dt
import logging
from collections.abc import Callable

import httpx

from pipeline.indicator_update import IndicatorUpdateError
from pipeline.indicators import Indicator, IndicatorBase
from pipeline.knowledge.catalog import IndicatorSpec
from pipeline.knowledge.ingest import FETCHERS, Fetcher, IngestError
from pipeline.knowledge.observations_load import candidates_from_json
from pipeline.knowledge.validate import validate_batch
from pipeline.models import EvidenceItem

logger = logging.getLogger(__name__)

FIRST_YEAR = 1900  # everything the API has
API_TITLE = "(consulta directa a la API oficial)"


class OnDemandSeries:
    """Fetches and validates a catalog series once per run, on first need."""

    def __init__(
        self,
        catalog: dict[str, IndicatorSpec],
        client_factory: Callable[[], httpx.Client],
        *,
        today: dt.date,
        fetchers: dict[str, Fetcher] = FETCHERS,
    ) -> None:
        self.catalog = catalog
        self.client_factory = client_factory
        self.today = today
        self.fetchers = fetchers
        self._series: dict[str, Indicator | None] = {}
        self.errors: list[str] = []
        self.fetched: list[str] = []

    def _load(self, indicator_id: str) -> Indicator | None:
        if indicator_id in self._series:
            return self._series[indicator_id]
        spec = self.catalog.get(indicator_id)
        fetch = self.fetchers.get(spec.conector) if spec else None
        ind: Indicator | None = None
        if spec is not None and fetch is not None:
            try:
                with self.client_factory() as client:
                    raw = fetch(spec, client, self.today, FIRST_YEAR)
                ind = self._validated(raw, spec)
                self.fetched.append(indicator_id)
            except (IngestError, IndicatorUpdateError, httpx.HTTPError, ValueError) as exc:
                message = f"API de {spec.conector} para {indicator_id}: {exc}"
                logger.warning(message)
                self.errors.append(message)
        self._series[indicator_id] = ind
        return ind

    def _validated(self, raw: Indicator, spec: IndicatorSpec) -> Indicator | None:
        """Keep only the values the ingestion would approve."""
        decisions = validate_batch(
            [c for c, _ in candidates_from_json(raw, spec)], spec, {}, today=self.today
        )
        ok = {(d.candidate.periodo_inicio, d.candidate.periodo_fin) for d in decisions
              if d.action == "nueva" and d.estado == "aprobado"}  # fmt: skip
        serie = [o for o in raw.serie if (o.desde, o.hasta) in ok]
        if not serie:
            return None
        dropped = len(raw.serie) - len(serie)
        notes = [
            spec.definicion,
            "Valores ACTUALES consultados en la API oficial durante el análisis: no están"
            " revisados por el equipo y pueden incluir revisiones posteriores al discurso.",
        ]
        if dropped:
            notes.append(f"Se descartaron {dropped} valores que no pasaron la validación.")
        return raw.model_copy(
            update={"serie": serie, "notas": notes, "estado": "automatico",
                    "nombre": spec.nombre}
        )  # fmt: skip

    def evidence(self, indicator_id: str, start: dt.date, end: dt.date) -> EvidenceItem | None:
        ind = self._load(indicator_id)
        if ind is None:
            return None
        item = IndicatorBase({indicator_id: ind}).evidence(indicator_id, start, end)
        if item is None:
            return None
        source = item.source.model_copy(
            update={
                "kind": "oficial",
                "title": f"{ind.nombre} {API_TITLE}",
                "date": self.today,
            }
        )
        return item.model_copy(update={"source": source})
