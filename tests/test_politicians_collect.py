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

    only = WdStatement("Q10", "Partido Popular", None, None, sourced=True)
    [m] = from_wikidata(facts(only), TODAY)[1]
    assert (m.partido, m.constatado_en) == ("Partido Popular", TODAY)
    dated_current = WdStatement("Q12", "Vox", D(2014, 1, 1), None)
    assert [m.partido for m in from_wikidata(facts(only, dated_current), TODAY)[1]] == ["Vox"]
    two = WdStatement("Q13", "Otro", None, None, sourced=True)
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


def test_a_declared_dismissal_ends_the_presidency_only_if_someone_else_follows():
    def declared_cese(ident, day, name):
        return hit(
            ident,
            day,
            f"Real Decreto 1/{day.year}, de {day.day} de abril, por el que"
            f" se declara el cese de don {name} como Presidente del Gobierno.",
        )

    s1 = _appoint(
        "S1",
        D(2018, 6, 2),
        D(2018, 6, 1),
        "Presidente del Gobierno",
        "Pedro Sánchez Pérez-Castejón",
        art="don",
    )
    s2 = _appoint(
        "S2",
        D(2020, 1, 8),
        D(2020, 1, 7),
        "Presidente del Gobierno",
        "Pedro Sánchez Pérez-Castejón",
        art="don",
    )
    after_election = declared_cese("C1", D(2019, 4, 29), "Pedro Sánchez Pérez-Castejón")
    rows = roles_from_boe(
        "Pedro Sánchez Pérez-Castejón",
        [s1, s2],
        [after_election],
        lambda c, s: [s1, s2],
        default_governments(),
    )
    assert any(r.en_funciones and r.inicio == D(2019, 4, 29) for r in rows)  # caretaker

    r1 = _appoint(
        "R1",
        D(2016, 10, 31),
        D(2016, 10, 30),
        "Presidente del Gobierno",
        "Mariano Rajoy Brey",
        art="don",
    )
    censure = hit(
        "C2",
        D(2018, 6, 2),
        "Real Decreto 353/2018, de 1 de junio, por el que se"
        " declara el cese de don Mariano Rajoy Brey como Presidente del Gobierno.",
    )
    rows = roles_from_boe(
        "Mariano Rajoy Brey", [r1], [censure], lambda c, s: [r1, s1], default_governments()
    )
    assert rows[-1].fin == D(2018, 6, 1)  # motion of no confidence: a real end


def test_staff_of_the_presidency_are_not_members_of_the_government():
    from pipeline.knowledge.politicians_collect import is_national_government

    assert not is_national_government("Secretario General de la Presidencia del Gobierno")
    assert not is_national_government("Director del Gabinete de la Presidencia del Gobierno")


def test_joining_the_government_closes_other_public_offices():
    judge = _appoint(
        "J",
        D(2012, 3, 6),
        D(2012, 3, 5),
        "Presidente de la Sala de lo Penal de la Audiencia Nacional",
        "Fernando Grande-Marlaska Gómez",
        art="don",
    )
    nxt = _appoint(
        "J2",
        D(2025, 3, 26),
        D(2025, 3, 25),
        "Presidente de la Sala de lo Penal de la Audiencia Nacional",
        "Otra Persona",
        art="don",
    )
    minister = _appoint(
        "M",
        D(2018, 6, 7),
        D(2018, 6, 6),
        "Ministro del Interior",
        "Fernando Grande-Marlaska Gómez",
        art="don",
    )
    notes: list[str] = []
    rows = roles_from_boe(
        "Fernando Grande-Marlaska Gómez",
        [judge, minister],
        [],
        lambda c, s: [judge, nxt, minister],
        default_governments(),
        notes=notes,
    )
    [sala] = [r for r in rows if "Sala" in r.cargo]
    assert sala.fin == D(2018, 6, 5)
    assert any("fin deducido" in n for n in notes)


def test_named_implied_dismissals_in_collective_decrees():
    from pipeline.knowledge.collect_sources import implied_in_body

    decree = hit(
        "BOE-A-2020-416",
        D(2020, 1, 13),
        "Real Decreto 8/2020, de 12 de enero, por el que se nombran Ministros del Gobierno.",
    )
    body = (
        "<p>El nombramiento conlleva el cese de don José Luis Escrivá Belmonte como"
        " Presidente de la Autoridad Independiente de Responsabilidad Fiscal.</p>"
    )
    [cese] = implied_in_body(decree, body)
    assert dismissal(cese) == (
        "Presidente de la Autoridad Independiente de Responsabilidad Fiscal",
        "José Luis Escrivá Belmonte",
    )
    assert cese.decree_date() == D(2020, 1, 12)


def test_open_secondary_offices_are_never_loaded_and_the_official_party_wins():
    from pipeline.knowledge.politicians_collect import SeedParty

    facts = WdFacts(
        qid="Q1",
        label="X",
        positions=[WdStatement("Q5", "concejal de Lugo", D(2023, 6, 17), None)],
        parties=[WdStatement("Q9", "Batasuna", D(2001, 1, 1), None)],
    )
    seed = SeedPerson(
        nombre="Ana Gil Ruiz",
        categoria="x",
        partido_actual=SeedParty(
            partido="EH Bildu",
            fuente=SeedSource(
                organismo="EH Bildu",
                titulo="Mahai Politikoa",
                url="https://ehbildu.eus/mahaipolitikoa",
            ),
        ),
    )
    result = build_politician(seed, today=TODAY, boe_roles=[], current_checks={}, facts=facts)
    assert result.politician.roles == []
    assert [(m.partido, m.constatado_en, m.source.tipo) for m in result.politician.parties] == [
        ("EH Bildu", TODAY, "primaria")
    ]
    assert any("Batasuna" in w for w in result.warnings)


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
        positions=[WdStatement("Q5", "concejal de Lugo", D(2019, 12, 3), None)],
        parties=[WdStatement("Q10", "Partido Popular", D(2015, 1, 1), None)],
    )
    seed = SeedPerson(nombre="Ana Gil Ruiz", categoria="x")
    result = build_politician(seed, today=TODAY, boe_roles=[], current_checks={}, facts=facts)
    assert any(
        "Wikidata: descartado «concejal de Lugo», abierto desde 2019-12-03" in w
        for w in result.warnings
    )
    assert result.politician.roles == []


def test_regional_governments_are_not_the_national_government():
    from pipeline.knowledge.politicians_collect import is_national_government

    assert is_national_government("Vicepresidenta Primera del Gobierno")
    assert is_national_government("Ministra de Hacienda")
    assert not is_national_government("Presidente del Gobierno de Canarias")
    assert not is_national_government("Presidente del Gobierno Vasco")


def test_a_dismissal_on_the_successors_day_is_the_end_date():
    a = _appoint(
        "A", D(2023, 11, 21), D(2023, 11, 20), "Ministro de Inclusión", "José Sol", art="don"
    )
    b = _appoint("B", D(2024, 9, 6), D(2024, 9, 5), "Ministro de Inclusión", "Eva Luz")
    cese = hit(
        "C",
        D(2024, 9, 6),
        "Real Decreto 889/2024, de 5 de septiembre, por el que se dispone el cese de don José"
        " Sol como Ministro de Inclusión.",
    )
    [role] = roles_from_boe("José Sol", [a], [cese], lambda c, s: [a, b], default_governments())
    assert role.fin == D(2024, 9, 5)


def test_a_new_cabinet_ends_every_minister_without_an_individual_decree():
    edu = _appoint(
        "E",
        D(1999, 1, 19),
        D(1999, 1, 18),
        "Ministro de Educación y Cultura",
        "Mariano Rajoy Brey",
        art="don",
    )
    rows = roles_from_boe(
        "Mariano Rajoy Brey",
        [edu],
        [],
        lambda c, s: [edu],
        default_governments(),
        cabinet_changes=[D(2000, 4, 27)],
    )
    assert [(r.inicio, r.fin) for r in rows] == [(D(1999, 1, 18), D(2000, 4, 26))]


def test_an_appointment_can_imply_a_dismissal():
    from pipeline.knowledge.collect_sources import implied_dismissals

    appointed = hit(
        "BOE-A-2026-7048",
        D(2026, 3, 27),
        "Real Decreto 259/2026, de 26 de marzo, por el que se nombra Ministro de Hacienda a"
        " don Arcadi España García.",
    )
    body = (
        "<p>Vengo en nombrar Ministro de Hacienda a don Arcadi España García.</p><p>Este"
        " nombramiento conlleva el cese de don Arcadi España García como Secretario de"
        " Estado de Política Territorial.</p>"
    )
    [implied] = implied_dismissals(appointed, body, "Arcadi España García")
    assert dismissal(implied) == (
        "Secretario de Estado de Política Territorial",
        "Arcadi España García",
    )
    assert implied.decree_date() == D(2026, 3, 26)


def test_spokespeople_come_from_the_junta_de_portavoces():
    from pipeline.knowledge.collect_sources import parse_junta
    from pipeline.knowledge.politicians_collect import roles_from_congreso

    def row(name, cargo, siglas, alta, baja):
        return {
            "apellidosNombre": name,
            "descCargo": cargo,
            "siglas": siglas,
            "fechaAltaFormat": alta,
            "fechaBajaFormat": baja,
        }

    data = {
        "data": [
            row("López Álvarez, Patxi", "Portavoces titulares", "GS", "14/09/2023", "06/10/2026"),
            row("López Álvarez, Patxi", "Portavoces adjuntos", "GS", "01/01/2023", ""),
            row("López Pérez, Ana", "Portavoces titulares", "GP", "14/09/2023", "06/10/2026"),
        ]
    }
    seed = SeedPerson(nombre="Francisco Javier López Álvarez", categoria="x", alias=["Patxi López"])
    [role] = roles_from_congreso(seed, parse_junta(data, "XV"))
    assert role.cargo == "Portavoz del Grupo Parlamentario Socialista"
    assert (role.inicio, role.fin, role.source.tipo) == (
        D(2023, 9, 14),
        D(2026, 10, 6),
        "primaria",
    )


def test_deputies_terms_end_at_the_dissolution():
    facts = WdFacts(
        qid="Q1",
        label="X",
        positions=[WdStatement("Q5", "diputado de España", D(2023, 8, 17), None)],
        parties=[WdStatement("Q10", "Partido Popular", D(2015, 1, 1), None)],
    )
    seed = SeedPerson(nombre="Ana Gil Ruiz", categoria="x")
    result = build_politician(seed, today=TODAY, boe_roles=[], current_checks={}, facts=facts)
    [role] = result.politician.roles
    assert role.fin == D(2026, 10, 6)


def test_boe_search_follows_pages_until_the_requested_date():
    import httpx

    from pipeline.knowledge.collect_sources import boe_search

    def page(items: list[tuple[str, str]], next_offset: int | None) -> str:
        lis = "".join(
            f'<li class="resultado-busqueda"><p>Real Decreto {i}, por el que se nombra X a don'
            f" Y.</p><div>BOE de {d}</div>BOE-A-2000-{i}</li>"
            for i, d in items
        )
        more = (
            f'<a href="boe.php?accion=Mas&id_busqueda=ABC-{next_offset}-50">Más</a>'
            if next_offset
            else ""
        )
        return f"<ul>{lis}</ul>{more}"

    pages = {
        None: page([("1", "01/01/2020")], 50),
        "ABC-50-50": page([("2", "01/01/2010")], 100),
        "ABC-100-50": page([("3", "01/01/2000")], None),
    }
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.params.get("id_busqueda")
        calls.append(key)
        return httpx.Response(200, text=pages[key])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert len(boe_search(client, "nombra X")) == 1  # no date: one page
        hits = boe_search(client, "nombra X", until=D(2005, 1, 1))
    assert [h.ident for h in hits] == ["BOE-A-2000-1", "BOE-A-2000-2", "BOE-A-2000-3"]
    assert calls == [None, None, "ABC-50-50", "ABC-100-50"]


def test_dismissals_keep_commas_in_the_office_name():
    cese = hit(
        "X",
        D(2023, 12, 29),
        "Real Decreto 1235/2023, de 29 de diciembre, por el que se dispone el cese de doña"
        " Nadia María Calviño Santamaría como Ministra de Economía, Comercio y Empresa.",
    )
    assert dismissal(cese)[0] == "Ministra de Economía, Comercio y Empresa"


def test_offices_taken_while_in_government_are_not_closed_by_a_later_appointment():
    name = "Carlos Cuerpo Caballero"
    minister = _appoint(
        "M", D(2023, 12, 29), D(2023, 12, 29), "Ministro de Economía", name, art="don"
    )
    imf = _appoint(
        "F", D(2024, 1, 30), D(2024, 1, 30), "Gobernador por España en el FMI", name, art="don"
    )
    vp = _appoint(
        "V", D(2026, 3, 27), D(2026, 3, 26), "Vicepresidente Primero del Gobierno", name, art="don"
    )
    rows = roles_from_boe(name, [minister, imf, vp], [], lambda c, s: [], default_governments())
    [governor] = [r for r in rows if "FMI" in r.cargo]
    assert governor.fin is None


def test_a_same_day_dismissal_ends_the_previous_term_not_the_renewal():
    name = "Óscar López Águeda"
    first = _appoint("A", D(2021, 7, 14), D(2021, 7, 13), "Director del Gabinete", name, art="don")
    again = _appoint(
        "B", D(2023, 11, 23), D(2023, 11, 22), "Director del Gabinete", name, art="don"
    )
    cese = hit(
        "C",
        D(2023, 11, 23),
        "Real Decreto 838/2023, de 22 de noviembre, por el que se"
        " declara el cese de don Óscar López Águeda como Director del Gabinete.",
    )
    rows = roles_from_boe(
        name, [first, again], [cese], lambda c, s: [first, again], default_governments()
    )
    assert [(r.inicio, r.fin) for r in rows] == [
        (D(2021, 7, 13), D(2023, 11, 22)),
        (D(2023, 11, 22), None),
    ]


def test_undated_parties_need_a_real_reference_and_independents_are_no_party():
    def facts(*parties):
        return WdFacts(qid="Q1", label="X", parties=list(parties))

    imported = WdStatement("Q10", "Partido Socialista Obrero Español", None, None)
    assert from_wikidata(facts(imported), TODAY)[1] == []
    independent = WdStatement("Q327591", "político independiente", D(2018, 1, 1), None)
    assert from_wikidata(facts(independent), TODAY)[1] == []


def test_office_names_match_across_genders_and_searches_try_both():
    from pipeline.knowledge.politicians_collect import _norm, other_gender

    assert _norm("Presidenta de la Sala de lo Penal") == _norm("Presidente de la Sala de lo Penal")
    assert _norm("Vicepresidenta Primera del Gobierno") == _norm(
        "Vicepresidente Primero del Gobierno"
    )
    assert other_gender("Presidente de la Sala de lo Penal") == "Presidenta de la Sala de lo Penal"
    assert other_gender("Ministra de Sanidad") == "Ministro de Sanidad"

    judge = _appoint(
        "J",
        D(2012, 3, 6),
        D(2012, 3, 5),
        "Presidente de la Sala de lo Penal",
        "Fernando Grande-Marlaska Gómez",
        art="don",
    )
    successor = _appoint(
        "E",
        D(2017, 6, 3),
        D(2017, 6, 2),
        "Presidenta de la Sala de lo Penal",
        "Concepción Espejel Jorquera",
    )
    [role] = roles_from_boe(
        "Fernando Grande-Marlaska Gómez",
        [judge],
        [],
        lambda c, s: [judge, successor],
        default_governments(),
    )
    assert role.fin == D(2017, 6, 1)


def test_another_member_of_a_board_does_not_replace_this_one():
    name = "Félix Bolaños García"
    vocal = _appoint(
        "V",
        D(2018, 6, 23),
        D(2018, 6, 22),
        "Vocal del Consejo de Administración del Patrimonio Nacional",
        name,
        art="don",
    )
    other = _appoint(
        "O",
        D(2018, 7, 21),
        D(2018, 7, 20),
        "Vocal del Consejo de Administración del Patrimonio Nacional",
        "Eva Sol",
    )
    cese = hit(
        "C",
        D(2021, 7, 28),
        "Real Decreto 644/2021, de 27 de julio, por el que se dispone"
        " el cese de don Félix Bolaños García como Vocal del Consejo de Administración del"
        " Patrimonio Nacional.",
    )
    [role] = roles_from_boe(
        name, [vocal], [cese], lambda c, s: [vocal, other], default_governments()
    )
    assert role.fin == D(2021, 7, 27)


def test_assuming_the_functions_of_an_office_is_an_appointment():
    saiz = hit(
        "BOE-A-2025-26354",
        D(2025, 12, 23),
        "Real Decreto 1185/2025, de 22 de diciembre,"
        " por el que se dispone que doña Elma Saiz Delgado, Ministra de Inclusión, Seguridad"
        " Social y Migraciones, asuma las funciones de Portavoz del Gobierno.",
    )
    assert saiz.appointment() == ("Portavoz del Gobierno", "Elma Saiz Delgado")


def test_dismissals_written_with_d_and_a_comma_and_named_only_in_the_body():
    from pipeline.knowledge.collect_sources import dismissals_in_body

    turull = hit(
        "T",
        D(2017, 10, 28),
        "Real Decreto 943/2017, de 27 de octubre, por el que se"
        " dispone el cese de D. JORDI TURULL i NEGRE, como Consejero de Presidencia.",
    )
    assert dismissal(turull) == ("Consejero de Presidencia", "JORDI TURULL i NEGRE")
    decree = hit(
        "BOE-A-2017-12332",
        D(2017, 10, 28),
        "Real Decreto 942/2017, de 27 de octubre,"
        " por el que se dispone, en virtud de las medidas autorizadas…, el cese del M.H."
        " Sr. Presidente de la Generalitat de Cataluña.",
    )
    body = (
        "<p>Vengo en disponer el cese de don Carles Puigdemont i Casamajó como Presidente de"
        " la Generalitat de Cataluña.</p>"
    )
    [cese] = dismissals_in_body(decree, body)
    assert dismissal(cese) == (
        "Presidente de la Generalitat de Cataluña",
        "Carles Puigdemont i Casamajó",
    )
    assert cese.decree_date() == D(2017, 10, 27)


def test_a_president_who_loses_an_election_stays_in_caretaker_until_the_successor():
    name = "Felipe González Márquez"
    fg = _appoint("F", D(1993, 7, 10), D(1993, 7, 9), "Presidente del Gobierno", name, art="don")
    aznar = _appoint(
        "A",
        D(1996, 5, 5),
        D(1996, 5, 4),
        "Presidente del Gobierno",
        "José María Aznar López",
        art="don",
    )
    declared_cese = hit(
        "C",
        D(1996, 3, 5),
        "Real Decreto 439/1996, de 4 de marzo, por el que se"
        " declara el cese de don Felipe González Márquez como Presidente del Gobierno.",
    )
    rows = roles_from_boe(
        name, [fg], [declared_cese], lambda c, s: [fg, aznar], default_governments()
    )
    assert [(r.inicio, r.fin, r.en_funciones) for r in rows] == [
        (D(1993, 7, 9), D(1996, 3, 3), False),
        (D(1996, 3, 4), D(1996, 5, 3), True),
    ]


def test_wikidata_spokesperson_offices_are_left_to_the_congreso():
    facts = WdFacts(
        qid="Q1",
        label="X",
        positions=[
            WdStatement(
                "Q7", "líder del Grupo Vasco en el Congreso", D(2012, 12, 18), D(2026, 10, 6)
            )
        ],
    )
    assert from_wikidata(facts, TODAY)[0] == []


def test_a_boe_dismissal_closes_the_same_office_taken_from_wikidata():
    facts = WdFacts(
        qid="Q1",
        label="X",
        positions=[
            WdStatement(
                "Q8",
                "Consejero de Presidencia de la Generalidad de Cataluña",
                D(2017, 7, 14),
                D(2018, 10, 27),
            )
        ],
        parties=[WdStatement("Q10", "Junts", D(2020, 1, 1), None)],
    )
    cese = hit(
        "T",
        D(2017, 10, 28),
        "Real Decreto 943/2017, de 27 de octubre, por el que se dispone el cese de D. JORDI"
        " TURULL i NEGRE, como Consejero de Presidencia.",
    )
    seed = SeedPerson(nombre="Jordi Turull i Negre", categoria="x")
    result = build_politician(
        seed, today=TODAY, boe_roles=[], current_checks={}, facts=facts, dismissals=[cese]
    )
    [role] = result.politician.roles
    assert role.fin == D(2017, 10, 27)


def test_overlapping_caretaker_periods_give_valid_rows():
    name = "Pedro Sánchez Pérez-Castejón"
    s1 = _appoint("S1", D(2018, 6, 2), D(2018, 6, 1), "Presidente del Gobierno", name, art="don")
    s2 = _appoint("S2", D(2020, 1, 8), D(2020, 1, 7), "Presidente del Gobierno", name, art="don")
    declared_cese = hit(
        "C",
        D(2019, 4, 30),
        "Real Decreto 336/2019, de 29 de abril, por el que se declara el cese de don Pedro"
        " Sánchez Pérez-Castejón como Presidente del Gobierno.",
    )
    rows = roles_from_boe(
        name, [s1, s2], [declared_cese], lambda c, s: [s1, s2], default_governments()
    )
    assert all(r.fin is None or r.fin >= r.inicio for r in rows)
    assert [(r.inicio, r.fin, r.en_funciones) for r in rows if r.source.url.endswith("S1")] == [
        (D(2018, 6, 1), D(2019, 4, 28), False),
        (D(2019, 4, 29), D(2020, 1, 6), True),
    ]


def test_a_published_dismissal_wins_over_the_inferred_incompatibility():
    name = "Félix Bolaños García"
    vocal = _appoint(
        "V",
        D(2018, 6, 23),
        D(2018, 6, 22),
        "Vocal del Consejo de Administración del Patrimonio Nacional",
        name,
        art="don",
    )
    minister = _appoint(
        "M", D(2021, 7, 12), D(2021, 7, 10), "Ministro de la Presidencia", name, art="don"
    )
    cese = hit(
        "C",
        D(2021, 7, 28),
        "Real Decreto 644/2021, de 27 de julio, por el que se dispone"
        " el cese de don Félix Bolaños García como Vocal del Consejo de Administración del"
        " Patrimonio Nacional.",
    )
    rows = roles_from_boe(name, [vocal, minister], [cese], lambda c, s: [], default_governments())
    [role] = [r for r in rows if r.cargo.startswith("Vocal")]
    assert role.fin == D(2021, 7, 27)


def test_listed_dismissals_in_a_decree_body():
    from pipeline.knowledge.collect_sources import dismissals_in_body

    decree = hit(
        "BOE-A-2017-12333",
        D(2017, 10, 28),
        "Real Decreto 943/2017, de 27 de octubre,"
        " por el que se dispone, en virtud de las medidas autorizadas…, el cese del"
        " Vicepresidente y de los Consejeros.",
    )
    body = (
        "<p>Vengo en disponer el cese de: D. ORIOL JUNQUERAS i VIES, como Titular del"
        " Departamento de la Vicepresidencia; D. JORDI TURULL i NEGRE, como Consejero de"
        " Presidencia; Dª. CLARA PONSATÍ i OBIOLS, como Consejera de Enseñanza.</p>"
    )
    ceses = [dismissal(x) for x in dismissals_in_body(decree, body)]
    assert ("Consejero de Presidencia", "JORDI TURULL i NEGRE") in ceses and len(ceses) == 3
