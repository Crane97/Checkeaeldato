from pipeline.evidence.passages import claim_terms, relevant_excerpt
from pipeline.models import DetectedClaim


def claim(normalized: str, queries: list[str] | None = None) -> DetectedClaim:
    return DetectedClaim(
        id="c1",
        speaker="autor",
        text_original=normalized,
        text_normalized=normalized,
        claim_type="otro",
        search_queries=queries or [],
    )


def test_terms_skip_stopwords_and_keep_numbers():
    terms = claim_terms(claim("España tiene 17 comunidades autónomas y 2 ciudades autónomas."))
    assert {"comunidades", "autónomas", "ciudades", "españa", "17"} <= terms
    assert "tiene" not in terms and "y" not in terms


def test_relevant_paragraph_deep_in_the_page_is_included():
    filler = "\n".join(f"Párrafo de navegación número {i} sin interés." for i in range(200))
    raw = (
        filler
        + "\nLas ciudades autónomas son Ceuta y Melilla, reguladas por sus Estatutos de 1995.\n"
        + filler
    )
    terms = claim_terms(claim("España tiene 17 comunidades autónomas y 2 ciudades autónomas."))
    excerpt = relevant_excerpt("Organización territorial de España.", raw, terms, max_chars=800)

    assert excerpt.startswith("Organización territorial de España.")
    assert "Ceuta y Melilla" in excerpt
    assert "navegación número 150" not in excerpt
    assert len(excerpt) <= 800


def test_selected_passages_keep_page_order():
    raw = (
        "Primero habla de comunidades.\n"
        "Texto sin relación alguna aquí.\n"
        "Luego de ciudades autónomas."
    )
    excerpt = relevant_excerpt("", raw, {"comunidades", "ciudades"}, max_chars=500)
    assert excerpt.index("comunidades") < excerpt.index("ciudades autónomas")
    assert "sin relación" not in excerpt


def test_without_raw_content_the_snippet_is_used():
    assert relevant_excerpt("Fragmento de Tavily.", None, {"x"}, max_chars=10) == "Fragmento"


def test_long_paragraphs_are_split_so_one_block_does_not_eat_the_budget():
    long_paragraph = (
        ". ".join(["Frase irrelevante sobre otra cosa"] * 200)
        + ". Dato clave: 3.202.002 españoles residen en el extranjero."
    )
    terms = claim_terms(claim("Españoles que residen en el extranjero"))
    excerpt = relevant_excerpt("", long_paragraph, terms, max_chars=400)
    assert "3.202.002" in excerpt
