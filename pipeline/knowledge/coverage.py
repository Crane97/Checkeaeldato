"""Cobertura de la base de conocimiento por partido y tema (fase G).

Comprueba que la base ayuda por igual a todos: compara entre partidos el porcentaje de
veredictos apoyados en series oficiales, el de `no_verificable` y el de afirmaciones sin
tema reconocido ("otros"), y señala los temas frecuentes sin indicadores para ampliar el
catálogo. Solo cuenta y describe; no valora a nadie. Una diferencia puede deberse al tipo
de afirmaciones (vagas, sobre el futuro…) y no a la base: las alertas lo dicen.
"""

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import yaml

from pipeline.politicians import fold

TEMAS_PATH = Path(__file__).resolve().parent / "temas.yaml"
# A party is flagged when it differs from the rest by more than this many points…
GAP_POINTS = 15.0
# …and it has at least this many verdicts (fewer would be noise).
MIN_CLAIMS = 10
NO_PARTY = "sin partido identificado"
CAVEAT = "La diferencia puede deberse al tipo de afirmaciones y no a la base."


def load_topics(path: Path = TEMAS_PATH) -> dict[str, list[str]]:
    data = yaml.safe_load(path.read_text("utf-8"))
    return {tema: [fold(k) for k in keywords] for tema, keywords in data.items()}


def topic_of(text: str, indicator_topic: str | None, topics: dict[str, list[str]]) -> str:
    """The indicator's topic if any; else the topic with most keywords in the text (ties:
    the first one listed); "otros" if none. Unknown topics also become "otros"."""
    if indicator_topic:
        return indicator_topic if indicator_topic in topics else "otros"
    folded = f" {fold(text)} "
    best, hits = "otros", 0
    for tema, keywords in topics.items():
        n = sum(1 for k in keywords if re.search(rf"\b{re.escape(k)}\b", folded))
        if n > hits:
            best, hits = tema, n
    return best


@dataclass
class Group:
    name: str
    total: int = 0
    official_data: int = 0  # verdicts resting on a series (local base or official API)
    unverifiable: int = 0
    other_topic: int = 0  # claims with no recognised topic

    def pct(self, n: int) -> float:
        return 100 * n / self.total if self.total else 0.0

    @property
    def pct_data(self) -> float:
        return self.pct(self.official_data)

    @property
    def pct_unverifiable(self) -> float:
        return self.pct(self.unverifiable)

    @property
    def pct_other(self) -> float:
        return self.pct(self.other_topic)


@dataclass
class CoverageReport:
    parties: list[Group] = field(default_factory=list)
    topics: list[Group] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    # Frequent topics whose claims rarely rest on series data: candidates for new indicators.
    gaps: list[str] = field(default_factory=list)
    # Groups left out of the comparison, and why.
    excluded: list[str] = field(default_factory=list)


def _rest(groups: list[Group], me: Group, attr: str) -> float:
    others = [g for g in groups if g is not me]
    total = sum(g.total for g in others)
    return 100 * sum(getattr(g, attr) for g in others) / total if total else 0.0


def coverage(conn: psycopg.Connection, since: dt.datetime | None = None) -> CoverageReport:
    rows = conn.execute(
        "select coalesce(partido, %s), coalesce(tema, 'otros'), count(*),"
        " count(*) filter (where con_datos),"
        " count(*) filter (where veredicto = 'no_verificable')"
        " from verif.claims where %s::timestamptz is null or fecha_verificacion >= %s::timestamptz"
        " group by 1, 2",
        (NO_PARTY, since, since),
    ).fetchall()
    parties: dict[str, Group] = {}
    topics: dict[str, Group] = {}
    for partido, tema, total, data, unverifiable in rows:
        for groups, key in ((parties, partido), (topics, tema)):
            g = groups.setdefault(key, Group(key))
            g.total += total
            g.official_data += data
            g.unverifiable += unverifiable
            g.other_topic += total if tema == "otros" else 0
    report = CoverageReport(
        parties=sorted(parties.values(), key=lambda g: -g.total),
        topics=sorted(topics.values(), key=lambda g: -g.total),
    )
    compared = []
    for g in report.parties:
        if g.name == NO_PARTY:
            report.excluded.append(f"{g.name} ({g.total}): no se puede atribuir a un partido")
        elif g.total < MIN_CLAIMS:
            report.excluded.append(f"{g.name} ({g.total}): menos de {MIN_CLAIMS} veredictos")
        else:
            compared.append(g)
    for g in compared:
        if len(compared) < 2:
            break
        checks = (
            ("unverifiable", g.pct_unverifiable, "no verificable", 1),
            ("official_data", g.pct_data, "apoyado en series oficiales", -1),
            ("other_topic", g.pct_other, "sin tema reconocido", 1),
        )
        for attr, mine, label, sign in checks:
            rest = _rest(compared, g, attr)
            if sign * (mine - rest) > GAP_POINTS:
                report.alerts.append(
                    f"{g.name}: {mine:.0f} % {label} frente al {rest:.0f} % del resto"
                    f" ({g.total} veredictos). Revisa si faltan indicadores (o temas) en los"
                    f" temas de esas afirmaciones. {CAVEAT}"
                )
    for t in report.topics:
        if t.name != "otros" and t.total >= MIN_CLAIMS and t.pct_data < 20:
            report.gaps.append(
                f"{t.name}: {t.total} afirmaciones, solo el {t.pct_data:.0f} % con datos de"
                f" series oficiales ({t.pct_unverifiable:.0f} % no verificable)"
            )
    return report
