import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from pipeline.claims import DetectionResult, ExtractedClaim
from pipeline.evidence.base import SourceResult
from pipeline.models import AudioInfo, CostEntry, EvidenceItem, Segment, Source, Transcript
from pipeline.run import PROMPTS_DIR, input_kind_for, run_job
from pipeline.verdict import VerdictAnswer

DATE = dt.date(2026, 11, 20)
ARTICLE = "Hola a todos.\n\nEl paro ha bajado un 20 % este año, según el Gobierno.\n"


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def structured(self, *, step: str, output_type: type, **kwargs: Any) -> Any:
        self.calls.append(step)
        if output_type is DetectionResult:
            return DetectionResult(
                claims=[
                    ExtractedClaim(
                        segment=1,
                        quote="El paro ha bajado un 20 % este año",
                        text_normalized="El paro en España bajó un 20 % en 2026.",
                        claim_type="estadistica",
                        search_queries=["tasa de paro España 2026"],
                    )
                ]
            )
        return VerdictAnswer(
            verdict="falso",
            confidence=0.8,
            explanation="Según la EPA, el paro bajó un 15 %, no un 20 %.",
            cited_evidence=["E1"],
            matched_factcheck=None,
            follow_up_queries=[],
        )

    def take_costs(self) -> list[CostEntry]:
        return [CostEntry(step="llm", provider="anthropic", model="m", usd=0.01)]


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

    def model_for(self, step: str) -> str:
        return "claude-haiku-5-5"


def run(path: Path, data_dir: Path, deps: FakeDeps, **kwargs: Any):
    return run_job(
        path, speech_date=DATE, data_dir=data_dir, deps=deps, prompts_dir=PROMPTS_DIR, **kwargs
    )


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
