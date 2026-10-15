"""Genera data/knowledge/municipios/padron_municipal.parquet desde los CSV oficiales del INE
(cifras oficiales del Padrón municipal, una tabla por provincia).

    uv run python scripts/recopilar_padron_municipal.py
"""

import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.knowledge.ingest import INE_OPERATION_TABLES, PADRON_PROVINCE_TABLE  # noqa: E402
from pipeline.knowledge.municipal import (  # noqa: E402
    CSV_URL,
    MUNICIPAL_PATH,
    parse_padron_csv,
    write_parquet,
)

PAUSE_SECONDS = 0.5


def main() -> None:
    rows: list[dict] = []
    with httpx.Client(timeout=180, follow_redirects=True) as client:
        tables = [
            t["Id"]
            for t in client.get(INE_OPERATION_TABLES.format(op="DPOP")).json()
            if t["Nombre"].strip().endswith(PADRON_PROVINCE_TABLE)
        ]
        for n, table in enumerate(tables, 1):
            time.sleep(PAUSE_SECONDS)
            response = client.get(CSV_URL.format(tabla=table))
            response.raise_for_status()
            found = parse_padron_csv(response.content.decode("utf-8-sig"), table)
            print(f"[{n}/{len(tables)}] tabla {table}: {len(found)} filas")
            rows += found
    write_parquet(rows)
    municipalities = len({r["municipio_ine"] for r in rows})
    size_kb = MUNICIPAL_PATH.stat().st_size // 1024
    print(f"{municipalities} municipios, {len(rows)} filas → {MUNICIPAL_PATH.name} ({size_kb} KB)")


if __name__ == "__main__":
    main()
