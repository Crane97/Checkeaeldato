"""Genera data/knowledge/territorios.json desde la API del INE (comunidades y provincias).

uv run python scripts/recopilar_territorios.py
uv run conocimiento cargar-territorios
"""

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.knowledge.territories import (  # noqa: E402
    INE_SOURCE,
    TERRITORIES_PATH,
    fetch_territories,
)


def main() -> None:
    with httpx.Client(follow_redirects=True) as client:
        territories = fetch_territories(client)
    payload = {
        "fuente": f"INE, API Tempus3 (variables 70 y 115 y su jerarquía); {INE_SOURCE}",
        "territorios": [t.model_dump() for t in territories],
    }
    TERRITORIES_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    levels = {}
    for t in territories:
        levels[t.nivel] = levels.get(t.nivel, 0) + 1
    print(f"{len(territories)} territorios en {TERRITORIES_PATH.name}: {levels}")


if __name__ == "__main__":
    main()
