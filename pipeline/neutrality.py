"""Reglas de neutralidad compartidas por modelos, tests y hooks del harness."""

import re

# Vocabulary that judges intent instead of facts; banned from outputs, prompts and UI.
FORBIDDEN_WORDS_RE = re.compile(
    r"\b(mentira|mentiras|miente|mienten|mintió|mintieron|mentiroso|mentirosa|mentirosos|mentirosas)\b",
    re.IGNORECASE,
)

# Prompts must not name concrete parties (same method for everyone). Case-sensitive on
# purpose: acronyms and proper names, so "podemos" or "sumar" as verbs do not match.
PARTY_NAMES_RE = re.compile(
    r"\b(PSOE|Partido Socialista|PP|Partido Popular|Vox|VOX|Sumar|Podemos|ERC|Esquerra"
    r"|Junts|PNV|EH Bildu|Bildu|BNG|Coalición Canaria|UPN|CUP|Ciudadanos|Cs|Más Madrid"
    r"|Compromís|SALF|Se Acabó La Fiesta|Teruel Existe)\b"
)


def neutrality_problems(text: str) -> list[str]:
    """Human-readable problems found in `text`; empty if it is neutral."""
    problems = [f"palabra prohibida «{m}»" for m in FORBIDDEN_WORDS_RE.findall(text)]
    problems += [f"nombre de partido «{m}»" for m in PARTY_NAMES_RE.findall(text)]
    return problems
