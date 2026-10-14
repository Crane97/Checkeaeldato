"""Actualiza las series automáticas de data/knowledge/indicators/ (INE, Eurostat, Seg. Social).

    uv run python scripts/actualizar_indicadores.py

Revisa el diff con git antes de hacer commit: es la única forma en que cambian los datos que
usa el verificador. SMI, pensiones y pensionistas se mantienen a mano (BOE / INSS).
"""

import datetime as dt
import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path

import httpx

from pipeline.indicator_update import (
    SPECS,
    EurostatSpec,
    IndicatorUpdateError,
    build_affiliation,
    build_eurostat,
    build_ine,
)
from pipeline.indicators import DEFAULT_INDICATORS_DIR, Indicator


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    today = dt.date.today()
    failed: list[str] = []
    with httpx.Client(timeout=60, headers={"User-Agent": "CheckeaElDato/0.1"}) as client:
        builders: list[tuple[str, Callable[[], Indicator]]] = []
        for spec in SPECS:
            build = build_eurostat if isinstance(spec, EurostatSpec) else build_ine
            builders.append((spec.id, lambda s=spec, b=build: b(s, client, today=today)))
        builders.append(("afiliacion", lambda: build_affiliation(client, today=today)))

        for ind_id, build_one in builders:
            try:
                ind = build_one()
            except (IndicatorUpdateError, httpx.HTTPError, ValueError) as exc:
                logging.error("%s: %s", ind_id, exc)
                failed.append(ind_id)
                continue
            path = Path(DEFAULT_INDICATORS_DIR) / f"{ind.id}.json"
            path.write_text(
                json.dumps(ind.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
            )
            last = ind.serie[-1]
            logging.info(
                "%s: %d datos, %s → %s (último: %s)",
                ind.id,
                len(ind.serie),
                ind.cubre_desde,
                ind.cubre_hasta,
                last.valor,
            )
    if failed:
        logging.error("No se actualizaron: %s", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
