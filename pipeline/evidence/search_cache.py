"""Caché en disco de las búsquedas de Tavily (data/cache/search/).

Cada búsqueda cuesta créditos y los análisis se repiten mucho (cambios de prompt,
evaluación, el mismo dato en varios discursos). La clave es el hash de la consulta
normalizada y de los parámetros; se guardan solo las respuestas correctas, con la fecha,
para poder auditarlas y caducarlas.
"""

import datetime as dt
import hashlib
import json
import logging
import os
import re
import tempfile
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DIR = Path("data") / "cache" / "search"


def _normalize_query(query: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", query)).strip().casefold()


def cache_key(request: dict[str, Any]) -> str:
    """Hash of the normalized query and every parameter that changes the results."""
    canonical = dict(request)
    canonical["query"] = _normalize_query(str(request.get("query", "")))
    if "include_domains" in canonical:
        canonical["include_domains"] = sorted(canonical["include_domains"])
    text = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


class SearchCache:
    def __init__(
        self,
        root: Path = DEFAULT_DIR,
        *,
        max_age_days: int = 30,
        read: bool = True,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self.root = root
        self.max_age = dt.timedelta(days=max_age_days)
        # False (--no-cache): always search, but still store the fresh result.
        self.read = read
        self._now = now

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """The stored response, or None if missing, expired, unreadable or not reading."""
        if not self.read:
            return None
        path = self._path(cache_key(request))
        try:
            data = json.loads(path.read_text())
            saved_at = dt.datetime.fromisoformat(data["saved_at"])
            response = data["response"]
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Entrada de caché ilegible %s; se busca de nuevo: %s", path, exc)
            return None
        if self._now() - saved_at > self.max_age:
            return None
        return response if isinstance(response, dict) else None

    def put(self, request: dict[str, Any], response: dict[str, Any]) -> None:
        """Store atomically; a failed write only loses the cache entry."""
        path = self._path(cache_key(request))
        entry = {"saved_at": self._now().isoformat(), "request": request, "response": response}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(entry, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("No se pudo guardar la búsqueda en caché (%s): %s", path, exc)
