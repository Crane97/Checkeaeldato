import datetime as dt
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from pipeline.claims import DetectionResult, ExtractedClaim
from pipeline.config import Settings
from pipeline.evidence.base import SourceResult
from pipeline.evidence.tavily import API_URL as TAVILY_URL
from pipeline.evidence.tavily import SearchStats
from pipeline.indicators import IndicatorBase
from pipeline.models import AudioInfo, CostEntry, EvidenceItem, Segment, Source, Transcript
from pipeline.promises import PromiseAnswer
from pipeline.run import PROMPTS_DIR, LiveDeps, input_kind_for, run_job
from pipeline.speakers import SpeakerIdentification
from tests.test_verdict import answer as verdict_answer

DATE = dt.date(2026, 11, 20)
ARTICLE = "Hola a todos.\n\nEl paro ha bajado un 20 % este año, según el Gobierno.\n"


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.unbilled = 0

    def structured(self, *, step: str, output_type: type, **kwargs: Any) -> Any:
        self.calls.append(step)
        self.unbilled += 1
        if output_type is SpeakerIdentification:
            return SpeakerIdentification(speakers=[])
        if output_type is DetectionResult:
            return DetectionResult(
                claims=[
                    ExtractedClaim(
                        segment=1,
                        quote="El paro ha bajado un 20 % este año",
                        text_normalized="El paro en España bajó un 20 % en 2026.",
                        claim_type="estadistica",
                        search_queries=["tasa de paro España 2026"],
                        speaker="",
                        needs_context=False,
                    )
                ]
            )
        return verdict_answer(
            verdict="falso",
            confidence=0.8,
            explanation="Según la EPA, el paro bajó un 15 %, no un 20 %.",
            cited_evidence=["E1"],
        )

    def take_costs(self) -> list[CostEntry]:
        costs = [CostEntry(step="llm", provider="anthropic", model="m", usd=0.01)] * self.unbilled
        self.unbilled = 0
        return costs


class FakeSource:
    name = "oficial"
    skip_if_found: frozenset[str] = frozenset()

    def __init__(self) -> None:
        self.calls = 0

    def search(self, claim, speech_date) -> SourceResult:
        self.calls += 1
        source = Source(
            url="https://www.ine.es/epa", title="EPA", organization="INE", kind="oficial"
        )
        return SourceResult(items=[EvidenceItem(id="", source=source, excerpt="Bajó un 15 %.")])


class FakeTranscriber:
    name = "fake"

    def transcribe(self, audio: AudioInfo) -> Transcript:
        return Transcript(
            provider="fake",
            language="es",
            duration=audio.duration,
            segments=[
                Segment(speaker="speaker_0", start=0, end=2, text="Hola."),
                Segment(
                    speaker="speaker_1",
                    start=3,
                    end=6,
                    text="El paro ha bajado un 20 % este año.",
                ),
            ],
        )


class FakeDeps:
    def __init__(self) -> None:
        self.llm = FakeLLM()
        self.source = FakeSource()
        self.transcriber = FakeTranscriber()
        self.evidence_sources = [self.source]
        self.disabled_sources = ["factcheck"]
        self.speaker_search = None
        self.searches = SearchStats()
        self.verdict_thinking = None
        self.indicators = IndicatorBase({})

    def model_for(self, step: str) -> str:
        return "claude-haiku-5-5"

    def take_search_cost(self, step: str) -> list[CostEntry]:
        return []


def run(path: Path, data_dir: Path, deps: FakeDeps, **kwargs: Any):
    kwargs.setdefault("speech_date", DATE)
    return run_job(path, data_dir=data_dir, deps=deps, prompts_dir=PROMPTS_DIR, **kwargs)


@pytest.fixture
def article(tmp_path: Path) -> Path:
    path = tmp_path / "articulo.txt"
    path.write_text(ARTICLE)
    return path


def test_input_kind_from_extension():
    assert input_kind_for(Path("a.TXT")) == "texto"
    assert input_kind_for(Path("a.md")) == "texto"
    assert input_kind_for(Path("debate.mp4")) == "video"


def test_text_article_end_to_end(article: Path, tmp_path: Path):
    deps = FakeDeps()
    result = run(article, tmp_path / "data", deps, author="Ana Autora")

    job_dir = result.report_path.parent
    for name in (
        "manifest.json",
        "transcript.json",
        "claims.json",
        "evidence.json",
        "verdicts.json",
        "report.json",
    ):
        assert (job_dir / name).exists(), name
    assert not (job_dir / "audio.json").exists()

    report = json.loads(result.report_path.read_text())
    [claim] = report["claims"]
    assert claim["speaker"] == "autor"
    assert claim["text_original"] == "El paro ha bajado un 20 % este año"
    assert (claim["start"], claim["end"]) == (None, None)
    assert claim["verdict"] == "falso"
    assert claim["category"] == "falso_con_fuente_oficial"
    assert claim["basis"] == "oficial"
    assert report["speakers"] == {"autor": "Ana Autora"}
    assert report["input_kind"] == "texto"
    assert report["by_speaker"]["Ana Autora"]["falso_con_fuente_oficial"] == 1
    assert any("factcheck" in w and "desactivada" in w for w in report["warnings"])
    assert report["cost"]["total_usd"] == pytest.approx(0.02)
    assert deps.llm.calls == ["claims", "verdicts"]
    assert "Afirmaciones verificables: 1" in result.summary


def test_rerun_reuses_everything_and_new_names_only_rebuild_the_report(article, tmp_path):
    deps = FakeDeps()
    run(article, tmp_path / "data", deps)
    deps.llm.calls.clear()

    run(article, tmp_path / "data", deps)
    assert deps.llm.calls == [] and deps.source.calls == 1

    result = run(article, tmp_path / "data", deps, author="Otro Nombre")
    assert deps.llm.calls == []
    report = json.loads(result.report_path.read_text())
    assert report["speakers"] == {"autor": "Otro Nombre"}


def test_from_step_reruns_later_steps(article, tmp_path):
    deps = FakeDeps()
    run(article, tmp_path / "data", deps)
    deps.llm.calls.clear()
    run(article, tmp_path / "data", deps, from_step="evidence")
    assert deps.llm.calls == ["verdicts"]
    assert deps.source.calls == 2


def test_video_pipeline_with_fake_audio_and_transcriber(tmp_path, monkeypatch):
    from pipeline import run as run_module

    video = tmp_path / "debate.mp4"
    video.write_bytes(b"fake video")

    def fake_extract(path: Path, out_dir: Path) -> AudioInfo:
        audio = out_dir / "audio.flac"
        audio.write_bytes(b"x")
        return AudioInfo(path=str(audio), duration=60.0, sample_rate=16000)

    monkeypatch.setattr(run_module, "extract_audio", fake_extract)
    result = run(
        video, tmp_path / "data", FakeDeps(), speakers={"speaker_1": "Candidata"}, purge_audio=True
    )

    report = json.loads(result.report_path.read_text())
    [claim] = report["claims"]
    assert (claim["speaker"], claim["start"], claim["end"]) == ("speaker_1", 3.0, 6.0)
    assert "Candidata" in report["by_speaker"]
    assert not (result.report_path.parent / "audio.flac").exists()
    transcription = [e for e in report["cost"]["entries"] if e["step"] == "transcript"]
    assert transcription and transcription[0]["audio_seconds"] == 60.0


def test_author_is_rejected_for_video(tmp_path):
    video = tmp_path / "debate.mp4"
    video.write_bytes(b"x")
    with pytest.raises(ValueError, match="--autor"):
        run(video, tmp_path / "data", FakeDeps(), author="X")


def test_live_deps_pick_the_configured_llm_backend(tmp_path):
    from pipeline.config import Settings
    from pipeline.llm_claude_code import ClaudeCodeLLM
    from pipeline.run import LiveDeps

    settings = Settings(_env_file=None, llm_backend="claude-code")
    assert isinstance(LiveDeps(settings).llm, ClaudeCodeLLM)


def test_new_speaker_info_reidentifies_speakers(tmp_path):
    from pipeline.models import SpeakerInfo

    text = tmp_path / "debate.txt"
    text.write_text("Ana Ruiz: Hola a todos.\nLuis Gómez: El paro ha bajado un 20 % este año.\n")
    deps = FakeDeps()
    run(text, tmp_path / "data", deps)
    deps.llm.calls.clear()

    result = run(
        text, tmp_path / "data", deps, speaker_info={"Luis Gómez": SpeakerInfo(party="PP")}
    )
    assert deps.llm.calls[0] == "speakers"
    profile = next(p for p in result.report.speaker_profiles if p.label == "Luis Gómez")
    assert profile.party == "PP"
    assert result.report.speakers["Luis Gómez"] == "Luis Gómez (PP)"

    deps.llm.calls.clear()
    run(text, tmp_path / "data", deps)  # None keeps the stored info: nothing to redo
    assert deps.llm.calls == []


class ContextLLM(FakeLLM):
    """Detects one "ustedes" claim in a dialogue and resolves it to the addressee."""

    def __init__(self, addressee: str) -> None:
        super().__init__()
        self.addressee = addressee

    def structured(self, *, step: str, output_type: type, **kwargs: Any) -> Any:
        from pipeline.context import ContextAnswer

        if output_type is DetectionResult:
            self.calls.append(step)
            self.unbilled += 1
            return DetectionResult(
                claims=[
                    ExtractedClaim(
                        segment=1,
                        quote="cuando ustedes gobernaban el paro superó el 26 %",
                        text_normalized="Cuando ustedes gobernaban el paro superó el 26 %.",
                        claim_type="estadistica",
                        search_queries=["tasa de paro EPA"],
                        speaker="",
                        needs_context=True,
                    )
                ]
            )
        if output_type is ContextAnswer:
            self.calls.append(step)
            self.unbilled += 1
            return ContextAnswer(
                referent="interlocutor",
                addressee=self.addressee,
                party="",
                president="",
                current_term_only=False,
                years=[],
                years_ago=None,
                confidence=0.9,
                notes="Se dirige al otro candidato.",
                rewritten="Durante los gobiernos de {actor} ({periodo}), el paro superó el 26 %.",
            )
        return super().structured(step=step, output_type=output_type, **kwargs)


DEBATE = (
    "Luis Gómez: Nosotros creamos empleo.\n"
    "Ana Ruiz: Señor Gómez, cuando ustedes gobernaban el paro superó el 26 %.\n"
)


def test_context_is_resolved_with_the_speakers_party(tmp_path):
    from pipeline.models import SpeakerInfo

    text = tmp_path / "debate.txt"
    text.write_text(DEBATE)
    deps = FakeDeps()
    deps.llm = ContextLLM(addressee="Luis Gómez")
    result = run(
        text,
        tmp_path / "data",
        deps,
        speech_date=dt.date(2023, 7, 10),
        speaker_info={"Luis Gómez": SpeakerInfo(party="PP"), "Ana Ruiz": SpeakerInfo(party="PSOE")},
    )
    [claim] = result.report.claims
    assert claim.context_status == "resuelto"
    assert claim.resolved_actor.startswith("PP")
    assert [r.start.year for r in claim.time_scope] == [1996, 2011]
    assert claim.text_normalized.startswith("Durante los gobiernos de PP")
    # Both speakers were fully given, so no identification call is needed.
    assert deps.llm.calls == ["claims", "context", "verdicts"]


def test_unresolved_context_skips_evidence_and_is_no_verificable(tmp_path):
    text = tmp_path / "debate.txt"
    text.write_text(DEBATE)
    deps = FakeDeps()
    deps.llm = ContextLLM(addressee="")
    result = run(text, tmp_path / "data", deps, speech_date=dt.date(2023, 7, 10))
    [claim] = result.report.claims
    assert claim.context_status == "no_resuelto"
    assert claim.verdict == "no_verificable"
    assert "interlocutor" in claim.explanation
    assert deps.source.calls == 0  # no searches spent on it
    assert "verdicts" not in deps.llm.calls


def test_failed_verdicts_are_retried_on_the_next_run(article, tmp_path):
    from pipeline.llm import LLMError

    deps = FakeDeps()
    original = deps.llm.structured

    def flaky(**kwargs):
        if kwargs["step"] == "verdicts":
            raise LLMError("límite de uso")
        return original(**kwargs)

    deps.llm.structured = flaky
    from pipeline.verdict import VerdictError

    with pytest.raises(VerdictError):  # the only verdict failed: the step stops
        run(article, tmp_path / "data", deps)

    deps.llm.structured = original
    deps.llm.calls.clear()
    result = run(article, tmp_path / "data", deps)
    assert deps.llm.calls == ["verdicts"]
    assert result.report.claims[0].analysis_error is None


class PromiseLLM(FakeLLM):
    """Detects one claim and one promise; assesses the promise as not feasible."""

    def structured(self, *, step: str, output_type: type, **kwargs: Any) -> Any:
        if output_type is DetectionResult:
            self.calls.append(step)
            self.unbilled += 1
            [claim] = super().structured(step=step, output_type=output_type, **kwargs).claims
            self.calls.pop()
            self.unbilled -= 1
            promise = ExtractedClaim(
                segment=1,
                quote="Bajaremos el paro al 0 %",
                text_normalized="Se promete reducir la tasa de paro de España al 0 %.",
                claim_type="estadistica",
                search_queries=["tasa de paro mínima histórica España"],
                speaker="",
                needs_context=False,
                kind="promesa",
            )
            return DetectionResult(claims=[claim, promise])
        if output_type is PromiseAnswer:
            self.calls.append(step)
            self.unbilled += 1
            return PromiseAnswer(
                assessment="inviable",
                confidence=0.8,
                explanation="El paro nunca ha bajado del 7,9 % en la serie de la EPA (E1).",
                obstacles=["datos"],
                consequences_sourced="",
                consequences_reasoning="",
                cited_evidence=["E1"],
            )
        return super().structured(step=step, output_type=output_type, **kwargs)


def test_promises_go_to_their_own_section(tmp_path: Path):
    path = tmp_path / "mitin.txt"
    path.write_text("Hola.\n\nEl paro ha bajado un 20 % este año. Bajaremos el paro al 0 %.\n")
    deps = FakeDeps()
    deps.llm = PromiseLLM()
    result = run(path, tmp_path / "data", deps)

    report = json.loads(result.report_path.read_text())
    assert [c["kind"] for c in report["claims"]] == ["afirmacion"]
    [promise] = report["promises"]
    assert promise["assessment"] == "inviable"
    assert promise["sources"][0]["url"] == "https://www.ine.es/epa"
    assert report["by_promise"]["inviable"] == 1
    assert sum(report["by_category"].values()) == 1  # promises are not counted as claims
    assert deps.llm.calls == ["claims", "verdicts", "promises"]
    assert "Promesas: 1" in result.summary


@respx.mock
def test_live_deps_share_one_cached_tavily_client_and_report_searches(tmp_path: Path):
    respx.post(TAVILY_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    settings = Settings(tavily_api_key="tvly-k", data_dir=tmp_path, tavily_max_results=3)
    deps = LiveDeps(settings)
    assert deps.speaker_search is not None
    assert deps.evidence_sources[1]._tavily is deps.tavily  # oficial uses the shared client

    deps.tavily.search("paro")
    deps.tavily.search("paro")
    [entry] = deps.take_search_cost("evidence")
    assert (entry.step, entry.searches, entry.cached_searches, entry.credits) == (
        "evidence",
        1,
        1,
        1,
    )
    assert deps.take_search_cost("verdicts") == []
    assert (deps.searches.real, deps.searches.cached) == (1, 1)
    assert list((tmp_path / "cache" / "search").rglob("*.json"))

    fresh = LiveDeps(settings, use_cache=False)
    fresh.tavily.search("paro")
    assert fresh.searches.real == 0 and fresh.take_search_cost("x")[0].searches == 1


def test_without_tavily_key_there_is_no_search_cost(tmp_path: Path):
    deps = LiveDeps(Settings(data_dir=tmp_path))
    assert deps.tavily is None and deps.take_search_cost("evidence") == []


def test_report_has_the_explanation_but_not_the_audit_analysis(article: Path, tmp_path: Path):
    result = run(article, tmp_path / "data", FakeDeps())
    [claim] = json.loads(result.report_path.read_text())["claims"]
    assert claim["explanation"] and "analysis" not in claim and "evidence_used" not in claim
    [verdict] = json.loads((result.report_path.parent / "verdicts.json").read_text())
    assert verdict["analysis"]["conclusion"] and verdict["evidence_used"] == ["E1"]


def test_changing_the_verdict_thinking_recomputes_only_from_the_verdicts(article, tmp_path):
    from pipeline.llm import ThinkingConfig

    deps = FakeDeps()
    run(article, tmp_path / "data", deps)
    deps.llm.calls.clear()
    deps.verdict_thinking = ThinkingConfig(True, "high")
    result = run(article, tmp_path / "data", deps)
    assert deps.llm.calls == ["verdicts"]
    assert result.steps_run == ["verdicts", "promises", "report"]


def test_claim_covered_by_the_local_indicators_skips_the_web_search(article, tmp_path):
    from pipeline.indicators import Indicator

    source = {"organismo": "INE", "documento": "EPA", "url": "https://www.ine.es/epa"}
    paro = Indicator.model_validate(
        {
            "id": "paro_epa",
            "nombre": "Tasa de paro (EPA)",
            "unidad": "%",
            "periodicidad": "anual",
            "origen": "ine",
            "estado": "automatico",
            "actualizado": "2026-10-13",
            "serie": [
                {"desde": "2025-01-01", "hasta": "2025-12-31", "valor": 12.0, "fuente": source},
                {"desde": "2026-01-01", "hasta": "2026-12-31", "valor": 10.5, "fuente": source},
            ],
        }
    )

    class IndicatorLLM(FakeLLM):
        def structured(self, *, step: str, output_type: type, **kwargs: Any) -> Any:
            result = super().structured(step=step, output_type=output_type, **kwargs)
            if output_type is DetectionResult:
                assert "paro_epa" in kwargs["user_content"]  # the catalog reaches the model
                [c] = result.claims
                return DetectionResult(
                    claims=[c.model_copy(update={"indicator": "paro_epa", "years": [2026]})]
                )
            return result

    deps = FakeDeps()
    deps.llm = IndicatorLLM()
    deps.indicators = IndicatorBase({"paro_epa": paro})
    result = run(article, tmp_path / "data", deps)

    assert deps.source.calls == 0  # no web search for this claim
    evidence = json.loads((result.report_path.parent / "evidence.json").read_text())
    [item] = evidence["claims"][0]["items"]
    assert item["source"]["kind"] == "indicador_local" and item["id"] == "E1"
    entries = json.loads(result.report_path.read_text())["cost"]["entries"]
    assert any(e["provider"] == "indicadores" and e["indicator_hits"] == 1 for e in entries)
