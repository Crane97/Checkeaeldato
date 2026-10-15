"""Población de los municipios según el Padrón (fase H4), en un Parquet local.

Unos 8.100 municipios × ~30 años: guardarlos en Supabase gastaría espacio del plan Free
para datos que se consultan poco, así que van en data/knowledge/municipios/
padron_municipal.parquet (versionado), generado con scripts/recopilar_padron_municipal.py
desde los CSV oficiales del INE (operación DPOP, una tabla por provincia).

Un municipio se reconoce por su nombre, solo si es único en España o si se da su
provincia ("Mérida (Badajoz)"); ante la duda, ninguno. Su código es `ES-M` + el código
INE de 5 cifras. El Padrón se refiere al 1 de enero y se publica hacia diciembre: un año
solo se usa si estaba publicado en la fecha del discurso (fecha estimada, se dice).
"""

import csv
import datetime as dt
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.models import EvidenceItem, Source
from pipeline.politicians import fold

MUNICIPAL_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "knowledge"
    / "municipios"
    / "padron_municipal.parquet"
)
CSV_URL = "https://www.ine.es/jaxiT3/files/t/csv_bdsc/{tabla}.csv?nocab=1"
TABLE_URL = "https://www.ine.es/jaxiT3/Tabla.htm?t={tabla}"
PUBLICATION_LAG_DAYS = 340  # 1 January figures, published around December
INDICATOR = "poblacion_padron"
_CODE_NAME = re.compile(r"^(\d{5}) (.+)$")


def parse_padron_csv(text: str, tabla: int) -> list[dict]:
    """Municipal totals of one province table ("02001 Abengibre;Total;2025;753")."""
    rows = []
    reader = csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";")
    next(reader, None)  # header
    for record in reader:
        if len(record) < 4 or record[1].strip() != "Total":
            continue
        m = _CODE_NAME.match(record[0].strip())
        value = record[3].strip().replace(".", "")
        if m is None or not value.isdigit() or not record[2].strip().isdigit():
            continue
        rows.append(
            {
                "municipio_ine": m.group(1),
                "municipio": m.group(2).strip(),
                "provincia_ine": m.group(1)[:2],
                "anyo": int(record[2]),
                "valor": int(value),
                "tabla": tabla,
            }
        )
    return rows


def write_parquet(rows: list[dict], path: Path = MUNICIPAL_PATH) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(sorted(rows, key=lambda r: (r["municipio_ine"], r["anyo"])))
    pq.write_table(table, path, compression="zstd")


def _names(name: str) -> set[str]:
    """ "Alicante/Alacant" and "Palmas de Gran Canaria, Las" → every usual form, folded."""
    out = set()
    for part in name.split("/"):
        part = part.strip()
        if ", " in part:
            head, tail = part.split(", ", 1)
            out |= {fold(f"{tail} {head}"), fold(head)}
        out.add(fold(part))
    return out


@dataclass
class MunicipalBase:
    rows: list[dict]
    province_names: dict[str, set[str]] = field(default_factory=dict)  # INE 2 digits → names
    # folded name → INE codes of the municipalities with that name (built once)
    _index: dict[str, set[str]] = field(default_factory=dict, repr=False)
    _by_code: dict[str, list[dict]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for r in self.rows:
            self._by_code.setdefault(r["municipio_ine"], []).append(r)
        for code, rs in self._by_code.items():
            for n in _names(rs[-1]["municipio"]):
                self._index.setdefault(n, set()).add(code)

    @classmethod
    def load(cls, path: Path = MUNICIPAL_PATH, territories=None) -> "MunicipalBase | None":
        if not path.is_file():
            return None
        import pyarrow.parquet as pq

        provinces: dict[str, set[str]] = {}
        for t in territories or []:
            if t.nivel == "provincia" and t.codigo_ine:
                provinces[t.codigo_ine] = {fold(n) for n in [t.nombre, *t.alias]}
        return cls(pq.read_table(path).to_pylist(), provinces)

    def resolve(self, name: str) -> str | None:
        """`ES-M` + INE code of the only municipality called `name`, else None.
        "Mérida (Badajoz)" narrows by province."""
        m = re.match(r"^(.*?)\s*\((.+)\)\s*$", name.strip())
        wanted, province = (m.group(1), m.group(2)) if m else (name.strip(), None)
        for prefix in ("el municipio de ", "municipio de ", "la ciudad de ", "el ayuntamiento de "):
            if fold(wanted).startswith(prefix):
                wanted = wanted[len(prefix) :]
        target = fold(wanted)
        prov_codes = None
        if province:
            prov_codes = {c for c, names in self.province_names.items() if fold(province) in names}
        hits = {
            code
            for code in self._index.get(target, set())
            if prov_codes is None or code[:2] in prov_codes
        }
        return f"ES-M{hits.pop()}" if len(hits) == 1 else None

    def evidence(self, code: str, years: list[int], speech_date: dt.date) -> EvidenceItem | None:
        """The municipality's population for the claim's years (published by the speech
        date), with the previous year for comparisons."""
        ine = code.removeprefix("ES-M")
        known = [
            r
            for r in self._by_code.get(ine, [])
            if dt.date(r["anyo"], 1, 1) + dt.timedelta(days=PUBLICATION_LAG_DAYS) <= speech_date
        ]
        if not known:
            return None
        wanted = set(years) or {max(r["anyo"] for r in known)}
        chosen = [r for r in known if r["anyo"] in wanted or r["anyo"] + 1 in wanted]
        if not any(r["anyo"] in wanted for r in chosen):
            return None
        chosen.sort(key=lambda r: r["anyo"])
        name = chosen[0]["municipio"]
        values = " | ".join(f"1/1/{r['anyo']} {r['valor']:,}".replace(",", ".") for r in chosen)
        table = chosen[-1]["tabla"]
        return EvidenceItem(
            id="",
            source=Source(
                url=TABLE_URL.format(tabla=table),
                title=f"Población de {name} según el Padrón (cifras oficiales del INE)",
                organization="INE",
                kind="indicador_local",
            ),
            excerpt=(
                f"Población empadronada en {name} (municipio, código INE {ine}) a 1 de enero."
                f" Fuente: INE, cifras oficiales del Padrón municipal, tabla {table}."
                f" Valores: {values}. Notas: cada cifra se publica hacia diciembre del mismo"
                f" año (fecha estimada); no es la Estadística Continua de Población."
            ),
        )


_default: "MunicipalBase | None | bool" = False


def default_municipal() -> "MunicipalBase | None":
    """The bundled Parquet with the bundled territories, loaded once (None if absent)."""
    global _default
    if _default is False:
        from pipeline.knowledge.territories import load_territories

        _default = MunicipalBase.load(territories=load_territories())
    return _default  # type: ignore[return-value]
