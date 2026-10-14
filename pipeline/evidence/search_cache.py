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

from pipeline.evidence.passages import SPANISH_STOPWORDS

logger = logging.getLogger(__name__)

DEFAULT_DIR = Path("data") / "cache" / "search"


_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)?|[^\W\d_]+", re.UNICODE)
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
# Searches whose most recent year is at least this old are about a closed period: official
# figures for it are final (INE and Eurostat revise the last two years), so they never expire.
CLOSED_PERIOD_YEARS = 2


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _normalize_query(query: str) -> str:
    """Lowercase, no accents, no Spanish stopwords, decimal commas as dots, sorted tokens."""
    text = _strip_accents(query.casefold())
    tokens = {t.replace(",", ".") for t in _TOKEN_RE.findall(text)}
    return " ".join(sorted(t for t in tokens if t not in SPANISH_STOPWORDS))


def _hash(canonical: dict[str, Any]) -> str:
    if "include_domains" in canonical:
        canonical["include_domains"] = sorted(canonical["include_domains"])
    text = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def cache_key(request: dict[str, Any]) -> str:
    """Hash of the normalized query and every parameter that changes the results."""
    return _hash({**request, "query": _normalize_query(str(request.get("query", "")))})


def legacy_cache_key(request: dict[str, Any]) -> str:
    """Key of the first version of the cache (only case and spaces normalized)."""
    query = re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(request.get("query", ""))))
    return _hash({**request, "query": query.strip().casefold()})


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
        """The stored response, or None if missing, expired, unreadable or not reading.
        Entries saved with the old key are found too, and copied under the new one."""
        if not self.read:
            return None
        found = self._read(self._path(cache_key(request)), request)
        if found is None:
            found = self._read(self._path(legacy_cache_key(request)), request)
            if found is not None:
                self.put(request, found[0], saved_at=found[1])  # keeps its original age
        return found[0] if found else None

    def _expired(self, request: dict[str, Any], saved_at: dt.datetime) -> bool:
        years = [int(y) for y in _YEAR_RE.findall(str(request.get("query", "")))]
        if years and max(years) <= self._now().year - CLOSED_PERIOD_YEARS:
            return False
        return self._now() - saved_at > self.max_age

    def _read(
        self, path: Path, request: dict[str, Any]
    ) -> tuple[dict[str, Any], dt.datetime] | None:
        try:
            data = json.loads(path.read_text())
            saved_at = dt.datetime.fromisoformat(data["saved_at"])
            response = data["response"]
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Entrada de caché ilegible %s; se busca de nuevo: %s", path, exc)
            return None
        if self._expired(request, saved_at):
            return None
        return (response, saved_at) if isinstance(response, dict) else None

    def put(
        self,
        request: dict[str, Any],
        response: dict[str, Any],
        saved_at: dt.datetime | None = None,
    ) -> None:
        """Store atomically; a failed write only loses the cache entry."""
        path = self._path(cache_key(request))
        when = (saved_at or self._now()).isoformat()
        entry = {"saved_at": when, "request": request, "response": response}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(entry, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("No se pudo guardar la búsqueda en caché (%s): %s", path, exc)
