"""Territorios de España (fase H): comunidades y ciudades autónomas y provincias.

Salen de la API del INE (variables 70 y 115 y su jerarquía) con `scripts/
recopilar_territorios.py` y se guardan en data/knowledge/territorios.json (versionado) y en
ref.territories. Códigos: el país es `ES`; las comunidades, su ISO 3166-2 (`ES-AN`), el
mismo que usa la base de políticos; las provincias, `ES-P` + su código INE (`ES-P28`).

Los nombres del INE van "al revés" ("Madrid, Comunidad de"): se guardan también en la
forma habitual y la corta como alias, más los nombres cooficiales. Un nombre que encaja con
más de un territorio ("Madrid": comunidad y provincia) solo se resuelve si se sabe el nivel;
ante la duda, ninguno.
"""

import json
from pathlib import Path
from typing import Literal

import httpx
import psycopg
from pydantic import BaseModel, Field

from pipeline.politicians import fold

TERRITORIES_PATH = Path(__file__).resolve().parents[2] / "data" / "knowledge" / "territorios.json"
INE_VALUES = "https://servicios.ine.es/wstempus/js/ES/VALORES_VARIABLE/{var}"
INE_CHILDREN = "https://servicios.ine.es/wstempus/js/ES/VALORES_HIJOS/70/{parent}"
INE_SOURCE = "https://www.ine.es/dyngs/INEbase/listaoperaciones.htm"

Level = Literal["pais", "ccaa", "provincia", "municipio"]

# INE code of each autonomous community or city → ISO 3166-2 subdivision.
CCAA_ISO = {
    "01": "AN", "02": "AR", "03": "AS", "04": "IB", "05": "CN", "06": "CB", "07": "CL",
    "08": "CM", "09": "CT", "10": "VC", "11": "EX", "12": "GA", "13": "MD", "14": "MC",
    "15": "NC", "16": "PV", "17": "RI", "18": "CE", "19": "ML",
}  # fmt: skip
# Names a territory is called by, beyond the INE's (criterion revised by
# neutrality-auditor, 2026-10-14): the official name in each co-official language (Statute
# or law) and the usual Spanish form, the same for every territory. Geographic names only:
# denominations with a political charge that are not the same territory ("Euskal Herria",
# "País Valencià") are left out.
EXTRA_ALIASES = {
    "ES-IB": ["Baleares", "Islas Baleares"],
    "ES-CN": ["Islas Canarias"],
    "ES-CM": ["Castilla-La Mancha", "Castilla La Mancha"],
    "ES-CT": ["Catalunya"],
    "ES-VC": ["Comunidad Valenciana", "Valencia"],
    "ES-PV": ["Euskadi", "Comunidad Autónoma del País Vasco"],
    "ES-NC": ["Navarra", "Nafarroa"],
    # Provinces the INE names only in the co-official language: their Spanish form too.
    "ES-P15": ["La Coruña"],
    "ES-P17": ["Gerona"],
    "ES-P20": ["Guipúzcoa"],
    "ES-P25": ["Lérida"],
    "ES-P32": ["Orense"],
    "ES-P48": ["Vizcaya"],
    "ES-P07": ["Baleares", "Islas Baleares"],
}


class Territory(BaseModel):
    code: str
    nivel: Level
    nombre: str
    parent_code: str | None = None
    codigo_ine: str | None = None
    alias: list[str] = Field(default_factory=list)


def usual_names(ine_name: str) -> list[str]:
    """ "Madrid, Comunidad de" → ["Comunidad de Madrid", "Madrid"]; "Alicante/Alacant" →
    both names."""
    names: list[str] = []
    for part in ine_name.split("/"):
        part = part.strip()
        if ", " in part:
            head, tail = part.split(", ", 1)
            names += [f"{tail} {head}", head]
        else:
            names.append(part)
    return list(dict.fromkeys(n for n in names if n))


def fetch_territories(client: httpx.Client) -> list[Territory]:
    def get(url: str) -> list[dict]:
        r = client.get(url, timeout=60)
        r.raise_for_status()
        return r.json()

    out = [Territory(code="ES", nivel="pais", nombre="España", codigo_ine="00")]
    ccaa = [v for v in get(INE_VALUES.format(var=70)) if v.get("Codigo") in CCAA_ISO]
    for v in ccaa:
        code = f"ES-{CCAA_ISO[v['Codigo']]}"
        names = usual_names(v["Nombre"])
        out.append(
            Territory(
                code=code,
                nivel="ccaa",
                nombre=names[0],
                parent_code="ES",
                codigo_ine=v["Codigo"],
                alias=list(dict.fromkeys(names[1:] + EXTRA_ALIASES.get(code, []))),
            )  # fmt: skip
        )
        for child in get(INE_CHILDREN.format(parent=v["Id"])):
            if child.get("FK_Variable") != 115 or not child.get("Codigo"):
                continue
            pnames = usual_names(child["Nombre"])
            out.append(
                Territory(
                    code=f"ES-P{child['Codigo']}",
                    nivel="provincia",
                    nombre=pnames[0],
                    parent_code=code,
                    codigo_ine=child["Codigo"],
                    alias=list(
                        dict.fromkeys(pnames[1:] + EXTRA_ALIASES.get(f"ES-P{child['Codigo']}", []))
                    ),
                )  # fmt: skip
            )
    codes = [t.code for t in out]
    if dupes := {c for c in codes if codes.count(c) > 1}:
        raise ValueError(f"Territorios repetidos en la respuesta del INE: {sorted(dupes)}")
    return out


def load_territories(path: Path = TERRITORIES_PATH) -> list[Territory]:
    data = json.loads(path.read_text("utf-8"))
    return [Territory.model_validate(t) for t in data["territorios"]]


_LEVEL_PREFIXES = (("la provincia de ", "provincia"), ("provincia de ", "provincia"))


def candidates(name: str, territories: list[Territory], nivel: Level | None = None) -> set[str]:
    """Codes of every territory that `name` can mean (at `nivel` if given). Accepts the
    INE word order ("Asturias, Principado de") and "la provincia de X"."""
    raw = name.strip()
    folded = fold(raw)
    for prefix, level in _LEVEL_PREFIXES:
        if folded.startswith(prefix):
            raw, nivel = raw[len(prefix) :], nivel or level  # type: ignore[assignment]
            break
    variants = {fold(raw), *(fold(n) for n in usual_names(raw))}
    return {
        t.code
        for t in territories
        if (nivel is None or t.nivel == nivel)
        and variants & {fold(n) for n in [t.nombre, *t.alias]}
    }


def resolve(name: str, territories: list[Territory], nivel: Level | None = None) -> str | None:
    """The code of the only territory called `name` (at `nivel` if given); else None."""
    hits = candidates(name, territories, nivel)
    return hits.pop() if len(hits) == 1 else None


def sync_territories(conn: psycopg.Connection, territories: list[Territory]) -> int:
    """Upsert into ref.territories (parents first, in the caller's transaction). The
    hierarchy is updated too; a code that would change level is an error."""
    order = {"pais": 0, "ccaa": 1, "provincia": 2, "municipio": 3}
    n = 0
    for t in sorted(territories, key=lambda t: order[t.nivel]):
        row = conn.execute(
            "insert into ref.territories (code, nivel, nombre, parent_code, codigo_ine, alias)"
            " values (%s, %s, %s, %s, %s, %s) on conflict (code) do update set"
            " nombre = excluded.nombre, codigo_ine = excluded.codigo_ine, alias = excluded.alias,"
            " parent_code = excluded.parent_code where ref.territories.nivel = excluded.nivel"
            " returning code",
            (t.code, t.nivel, t.nombre, t.parent_code, t.codigo_ine, t.alias),
        ).fetchone()
        if row is None:
            raise ValueError(f"El territorio {t.code} ya existe con otro nivel (no es {t.nivel})")
        n += 1
    return n
