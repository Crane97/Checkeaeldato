"""Recopilación de políticos: BOE (primaria) y Wikidata (secundaria), sin red."""

import datetime as dt

from pipeline.governments import default_governments
from pipeline.knowledge.collect_sources import (
    BoeHit,
    WdFacts,
    WdStatement,
    dismissal,
    latest_appointment,
    parse_boe_results,
    parse_entity,
)
from pipeline.knowledge.politicians_collect import (
    SeedOffice,
    SeedPerson,
    SeedSource,
    build_politician,
    from_wikidata,
    roles_from_boe,
)

D = dt.date
TODAY = D(2026, 10, 14)

PAGE = """
<ul><li class="resultado-busqueda"><h4>Presidencia del Gobierno</h4>
<p>Real Decreto 380/2024, de 11 de abril, por el que se nombra Presidente de la Junta de
Galicia a don Alfonso Rueda Valenzuela.</p>
<div>BOE 90 de 12/04/2024 - II. Autoridades y personal</div>
<a href="/diario_boe/txt.php?id=BOE-A-2024-7204">Ir al documento</a></li>
<li class="resultado-busqueda"><p>Real Decreto 1/2025, de 30 de diciembre, por el que se nombra
Ministra de Hacienda a do&ntilde;a Ana Gil Ruiz.</p><div>BOE 1 de 02/01/2025</div>
<a href="/diario_boe/txt.php?id=BOE-A-2025-1">Ir</a></li></ul>
"""


def hit(ident: str, published: dt.date, title: str) -> BoeHit:
    return BoeHit(ident, published, title)


def test_boe_results_are_parsed_with_appointment_and_decree_date():
    galicia, gil = parse_boe_results(PAGE)
    assert galicia.ident == "BOE-A-2024-7204" and galicia.published == D(2024, 4, 12)
    assert galicia.url == "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2024-7204"
    assert galicia.appointment() == (
        "Presidente de la Junta de Galicia",
        "Alfonso Rueda Valenzuela",
    )
    assert galicia.decree_date() == D(2024, 4, 11)
    assert gil.appointment() == ("Ministra de Hacienda", "Ana Gil Ruiz")
    assert gil.decree_date() == D(2024, 12, 30)  # decree in December, published in January


def test_dismissals_and_latest_appointment():
    cese = hit(
        "BOE-A-2026-2",
        D(2026, 3, 27),
        "Real Decreto 257/2026, de 26 de marzo, por el que se dispone el cese de doña Ana Gil"
        " Ruiz como Ministra de Hacienda.",
    )
    assert dismissal(cese) == ("Ministra de Hacienda", "Ana Gil Ruiz")
    hits = parse_boe_results(PAGE)
    assert latest_appointment(hits, "presidente de la junta de galicia").ident == "BOE-A-2024-7204"
    assert latest_appointment(hits, "Presidente de Canarias") is None


def _appoint(ident, published, decree_day, cargo, nombre, verb="nombra", art="doña"):
    months = [
        "enero",
        "febrero",
        "marzo",
        "abril",
        "mayo",
        "junio",
        "julio",
        "agosto",
        "septiembre",
        "octubre",
        "noviembre",
        "diciembre",
    ]
    return hit(
        ident,
        published,
        f"Real Decreto 1/{published.year}, de {decree_day.day} de {months[decree_day.month - 1]},"
        f" por el que se {verb} {cargo} a {art} {nombre}.",
    )


def test_offices_end_at_the_dismissal_or_the_next_appointment():
    first = _appoint("A1", D(2018, 6, 7), D(2018, 6, 6), "Ministra de Hacienda", "Ana Gil Ruiz")
    # Every minister is appointed again after each investiture (here 2020 and 2023).
    again_2020 = _appoint(
        "A0", D(2020, 1, 13), D(2020, 1, 12), "Ministra de Hacienda", "Ana Gil Ruiz"
    )
    again_2023 = _appoint(
        "A1b", D(2023, 11, 21), D(2023, 11, 20), "Ministra de Hacienda", "Ana Gil Ruiz"
    )
    renewed = _appoint("A2", D(2024, 1, 3), D(2024, 1, 2), "Ministra de Hacienda", "Ana Gil Ruiz")
    other = _appoint("A3", D(2026, 3, 28), D(2026, 3, 27), "Ministra de Hacienda", "Eva Sol")
    cese = hit(
        "C1",
        D(2026, 3, 27),
        "Real Decreto 9/2026, de 26 de marzo, por el que se dispone el cese de doña Ana Gil Ruiz"
        " como Ministra de Hacienda.",
    )
    successors = {"Ministra de Hacienda": [first, again_2020, again_2023, renewed, other]}
    roles = roles_from_boe(
        "Ana Gil Ruiz",
        [first, again_2020, again_2023, renewed, other],
        [cese],
        lambda cargo, start: successors.get(cargo, []),
        default_governments(),
    )
    plain = [(r.inicio, r.fin, r.en_funciones) for r in roles]
    # In caretaker from the dissolution until the minister is appointed again.
    assert plain[0] == (D(2018, 6, 6), D(2019, 4, 28), False)
    assert plain[1] == (D(2019, 4, 29), D(2020, 1, 11), True)
    assert (D(2023, 7, 24), D(2023, 11, 19), True) in plain
    assert plain[-1] == (D(2024, 1, 2), D(2026, 3, 26), False)  # ends at the dismissal
    assert all(r.organo == "Gobierno de España" and r.source.tipo == "primaria" for r in roles)
    assert {r.source.url.rsplit("=", 1)[1] for r in roles} == {"A1", "A0", "A1b", "A2"}


def test_regional_offices_get_their_territory_and_no_caretaker_split():
    rueda = _appoint(
        "G1",
        D(2024, 4, 12),
        D(2024, 4, 11),
        "Presidente de la Junta de Galicia",
        "Alfonso Rueda Valenzuela",
        art="don",
    )
    [role] = roles_from_boe(
        "Alfonso Rueda Valenzuela", [rueda], [], lambda c, s: [], default_governments()
    )
    assert (role.territorio, role.organo, role.fin) == ("ES-GA", None, None)


ENTITY = {
    "id": "Q1",
    "labels": {"es": {"value": "Ana Gil"}},
    "sitelinks": {"eswiki": {"title": "Ana Gil Ruiz"}},
    "claims": {
        "P569": [
            {
                "mainsnak": {
                    "snaktype": "value",
                    "datavalue": {"value": {"time": "+1970-05-02T00:00:00Z", "precision": 11}},
                },
                "rank": "normal",
            }
        ],
        "P102": [
            {
                "mainsnak": {"snaktype": "value", "datavalue": {"value": {"id": "Q10"}}},
                "rank": "normal",
                "qualifiers": {
                    "P580": [
                        {
                            "snaktype": "value",
                            "datavalue": {
                                "value": {"time": "+2015-07-01T00:00:00Z", "precision": 11}
                            },
                        }
                    ]
                },
            },
            {
                "mainsnak": {"snaktype": "value", "datavalue": {"value": {"id": "Q11"}}},
                "rank": "deprecated",
            },
        ],
        "P39": [
            {
                "mainsnak": {"snaktype": "value", "datavalue": {"value": {"id": "Q20"}}},
                "rank": "normal",
                "qualifiers": {
                    "P580": [
                        {
                            "snaktype": "value",
                            "datavalue": {
                                "value": {"time": "+2023-08-17T00:00:00Z", "precision": 11}
                            },
                        }
                    ]
                },
            },
            {
                "mainsnak": {"snaktype": "value", "datavalue": {"value": {"id": "Q21"}}},
                "rank": "normal",
                "qualifiers": {
                    "P580": [
                        {
                            "snaktype": "value",
                            "datavalue": {
                                "value": {"time": "+2024-01-02T00:00:00Z", "precision": 11}
                            },
                        }
                    ]
                },
            },
            {
                "mainsnak": {"snaktype": "value", "datavalue": {"value": {"id": "Q22"}}},
                "rank": "normal",
                "qualifiers": {
                    "P580": [
                        {
                            "snaktype": "value",
                            "datavalue": {
                                "value": {"time": "+2010-00-00T00:00:00Z", "precision": 9}
                            },
                        }
                    ]
                },
            },
        ],
    },
}
LABELS = {
    "Q10": "Partido Popular",
    "Q11": "Otro",
    "Q20": "diputada en el Congreso de los Diputados",
    "Q21": "ministra de Hacienda de España",
    "Q22": "concejala",
}


def test_wikidata_entities_keep_only_day_precise_dates_and_skip_boe_offices():
    facts = parse_entity(ENTITY, LABELS)
    assert facts.birth == D(1970, 5, 2)
    assert facts.eswiki == "https://es.wikipedia.org/wiki/Ana_Gil_Ruiz"
    assert [p.label for p in facts.parties] == ["Partido Popular"]  # deprecated dropped
    roles, parties = from_wikidata(facts, TODAY)
    assert [r.cargo for r in roles] == ["diputada en el Congreso de los Diputados"]
    assert all(r.source.tipo == "secundaria" and r.source.organismo == "Wikidata" for r in roles)
    assert [(m.partido, m.inicio) for m in parties] == [("Partido Popular", D(2015, 7, 1))]


def test_an_undated_party_counts_as_seen_today_only_when_it_is_the_only_current_one():
    def facts(*parties):
        return WdFacts(qid="Q1", label="X", parties=list(parties))

    only = WdStatement("Q10", "Partido Popular", None, None)
    [m] = from_wikidata(facts(only), TODAY)[1]
    assert (m.partido, m.constatado_en) == ("Partido Popular", TODAY)
    dated_current = WdStatement("Q12", "Vox", D(2014, 1, 1), None)
    assert [m.partido for m in from_wikidata(facts(only, dated_current), TODAY)[1]] == ["Vox"]
    two = WdStatement("Q13", "Otro", None, None)
    assert from_wikidata(facts(only, two), TODAY)[1] == []


SEED = SeedPerson(
    nombre="Ana Gil Ruiz",
    categoria="gobierno",
    alias=["Ana Gil"],
    cargos_actuales=[
        SeedOffice(cargo="Ministra de Hacienda", boe=True),
        SeedOffice(
            cargo="Portavoz",
            organo="Congreso de los Diputados",
            fuente=SeedSource(
                organismo="Congreso", titulo="Portavoces", url="https://www.congreso.es/p"
            ),
        ),
    ],
)


def test_current_offices_are_checked_against_the_boe_and_official_pages():
    appointed = _appoint("A2", D(2024, 1, 3), D(2024, 1, 2), "Ministra de Hacienda", "Ana Gil Ruiz")
    boe_roles = roles_from_boe(
        "Ana Gil Ruiz", [appointed], [], lambda c, s: [], default_governments()
    )
    facts = parse_entity(ENTITY, LABELS)
    ok = build_politician(
        SEED,
        today=TODAY,
        boe_roles=boe_roles,
        current_checks={"Ministra de Hacienda": appointed},
        facts=facts,
    )
    p = ok.politician
    assert ok.warnings == []
    assert (p.id, p.origen, p.estado, p.fecha_nacimiento) == (
        "ana-gil-ruiz",
        "curado",
        "pendiente",
        D(1970, 5, 2),
    )
    spokesperson = next(r for r in p.roles if r.cargo == "Portavoz")
    assert spokesperson.constatado_en == TODAY and spokesperson.source.tipo == "primaria"

    other = _appoint("A9", D(2026, 3, 28), D(2026, 3, 27), "Ministra de Hacienda", "Eva Sol")
    stale = build_politician(
        SEED,
        today=TODAY,
        boe_roles=boe_roles,
        current_checks={"Ministra de Hacienda": other},
        facts=None,
    )
    assert any("Eva Sol" in w for w in stale.warnings)
    assert any("sin Wikidata" in w for w in stale.warnings)


def test_collective_decrees_give_one_appointment_per_line():
    from pipeline.knowledge.collect_sources import parse_collective

    decree = hit(
        "BOE-A-2023-23543",
        D(2023, 11, 21),
        "Real Decreto 835/2023, de 20 de noviembre, por el que se nombran Ministros del Gobierno.",
    )
    body = (
        "<p>De conformidad con lo dispuesto en los artículos 62 y 100 de la Constitución,"
        " vengo en nombrar:</p><p>Ministro del Interior a don Fernando Grande-Marlaska Gómez.</p>"
        "<p>Ministra de Defensa a doña María Margarita Robles Fernández.</p>"
        "<p>Dado en Madrid, el 20 de noviembre de 2023.</p>"
    )
    interior, defensa = parse_collective(decree, body)
    assert interior.appointment() == ("Ministro del Interior", "Fernando Grande-Marlaska Gómez")
    assert defensa.appointment() == ("Ministra de Defensa", "María Margarita Robles Fernández")
    assert defensa.decree_date() == D(2023, 11, 20) and defensa.url.endswith("BOE-A-2023-23543")


def test_a_declared_dismissal_after_an_election_does_not_end_the_office():
    declared = hit(
        "X",
        D(2019, 4, 30),
        "Real Decreto 336/2019, de 29 de abril, por el que se declara el cese de don Pedro"
        " Sánchez Pérez-Castejón como Presidente del Gobierno.",
    )
    assert dismissal(declared) is None  # it opens the caretaker period


def test_the_president_follows_the_caretaker_table_and_ministers_stay_until_replaced():
    name = "Pedro Sánchez Pérez-Castejón"
    pm = _appoint("P1", D(2018, 6, 2), D(2018, 6, 1), "Presidente del Gobierno", name, art="don")
    pm2 = _appoint("P2", D(2020, 1, 8), D(2020, 1, 7), "Presidente del Gobierno", name, art="don")
    rows = roles_from_boe(name, [pm, pm2], [], lambda c, s: [pm, pm2], default_governments())
    assert [(r.inicio, r.fin, r.en_funciones) for r in rows][:2] == [
        (D(2018, 6, 1), D(2019, 4, 28), False),
        (D(2019, 4, 29), D(2020, 1, 6), True),
    ]
    minister = _appoint("M1", D(2023, 1, 2), D(2023, 1, 1), "Ministra de Sanidad", "Eva Sol")
    renewed = _appoint("M2", D(2023, 11, 21), D(2023, 11, 20), "Ministra de Sanidad", "Eva Sol")
    rows = roles_from_boe(
        "Eva Sol", [minister, renewed], [], lambda c, s: [minister, renewed], default_governments()
    )
    assert [(r.inicio, r.fin, r.en_funciones) for r in rows] == [
        (D(2023, 1, 1), D(2023, 7, 23), False),
        (D(2023, 7, 24), D(2023, 11, 19), True),  # until her reappointment, not the investiture
        (D(2023, 11, 20), None, False),
    ]


def test_non_breaking_spaces_in_collective_decrees_are_normalised():
    from pipeline.knowledge.collect_sources import parse_collective

    decree = hit(
        "D",
        D(2023, 11, 21),
        "Real Decreto 835/2023, de 20 de noviembre, por el que se nombran Ministros del Gobierno.",
    )
    body = (
        "<p>Ministro de Derechos Sociales, Consumo y Agenda\u00a02030"
        " a don Pablo Bustinduy Amador.</p>"
    )
    [line] = parse_collective(decree, body)
    cargo = "Ministro de Derechos Sociales, Consumo y Agenda 2030"
    assert latest_appointment([line], cargo) is line


def test_open_wikidata_terms_close_when_the_next_one_starts_and_unlabelled_ones_go():
    facts = WdFacts(
        qid="Q1",
        label="X",
        positions=[
            WdStatement("Q5", "diputado de España", D(2019, 12, 3), None),
            WdStatement("Q5", "diputado de España", D(2023, 8, 17), None),
            WdStatement("Q54856736", "Q54856736", D(2012, 5, 7), D(2013, 9, 9)),
        ],
    )
    roles, _ = from_wikidata(facts, TODAY)
    assert [(r.inicio, r.fin) for r in roles] == [
        (D(2019, 12, 3), D(2023, 8, 16)),
        (D(2023, 8, 17), None),
    ]


def test_a_new_appointment_never_starts_in_caretaker():
    name = "Pedro Sánchez Pérez-Castejón"
    pm = _appoint("P1", D(2018, 6, 2), D(2018, 6, 1), "Presidente del Gobierno", name, art="don")
    pm2 = _appoint("P2", D(2020, 1, 8), D(2020, 1, 7), "Presidente del Gobierno", name, art="don")
    rows = roles_from_boe(name, [pm, pm2], [], lambda c, s: [pm, pm2], default_governments())
    second = [r for r in rows if r.source.url.endswith("P2")]
    assert (second[0].inicio, second[0].en_funciones) == (D(2020, 1, 7), False)


def test_old_open_secondary_offices_are_left_out_and_listed_for_review():
    facts = WdFacts(
        qid="Q1",
        label="X",
        positions=[WdStatement("Q5", "diputado de España", D(2019, 12, 3), None)],
        parties=[WdStatement("Q10", "Partido Popular", D(2015, 1, 1), None)],
    )
    seed = SeedPerson(nombre="Ana Gil Ruiz", categoria="x")
    result = build_politician(seed, today=TODAY, boe_roles=[], current_checks={}, facts=facts)
    assert any(
        "descartado «diputado de España», abierto desde 2019-12-03" in w for w in result.warnings
    )
    assert result.politician.roles == []
