import httpx
import pytest
import respx

from pipeline.config import Settings
from pipeline.models import AudioInfo
from pipeline.transcribe import TranscriptionConfigError, TranscriptionError, make_transcriber
from pipeline.transcribe.elevenlabs import (
    API_URL,
    MODEL_ID,
    ElevenLabsTranscriber,
    words_to_segments,
)


def w(text: str, start: float, speaker: str | None = "speaker_0", kind: str = "word") -> dict:
    return {"text": text, "start": start, "end": start + 0.4, "type": kind, "speaker_id": speaker}


def sp(start: float, speaker: str | None = "speaker_0") -> dict:
    return w(" ", start, speaker, "spacing")


@pytest.fixture
def audio(tmp_path) -> AudioInfo:
    path = tmp_path / "audio.flac"
    path.write_bytes(b"fLaC fake audio")
    return AudioInfo(path=str(path), duration=12.0, sample_rate=16000)


def transcriber(**kwargs) -> ElevenLabsTranscriber:
    return ElevenLabsTranscriber(api_key="xi-KEY", backoff=0, **kwargs)


# --- words_to_segments ------------------------------------------------------


def test_speaker_changes_start_new_segments():
    words = [
        w("Hola.", 0.0),
        sp(0.4),
        w("Buenas", 1.0, "speaker_1"),
        sp(1.4, "speaker_1"),
        w("tardes.", 1.5, "speaker_1"),
    ]
    segments = words_to_segments(words)
    assert [(s.speaker, s.text, s.start, s.end) for s in segments] == [
        ("speaker_0", "Hola.", 0.0, 0.4),
        ("speaker_1", "Buenas tardes.", 1.0, 1.9),
    ]


def test_long_turns_split_at_sentence_ends():
    long_sentence = "palabra " * 50  # ~400 chars
    words = [
        w(long_sentence.strip() + ".", 0.0),
        sp(0.4),
        w("Otra", 1.0),
        sp(1.4),
        w("frase.", 1.5),
    ]
    segments = words_to_segments(words, max_chars=300)
    assert [s.text for s in segments] == [long_sentence.strip() + ".", "Otra frase."]
    assert all(s.speaker == "speaker_0" for s in segments)


def test_audio_events_are_skipped_and_unknown_speaker_is_labelled():
    words = [w("(aplausos)", 0.0, kind="audio_event"), w("Gracias.", 1.0, speaker=None)]
    [segment] = words_to_segments(words)
    assert (segment.text, segment.speaker) == ("Gracias.", "speaker_desconocido")


def test_words_without_times_inherit_neighbours():
    words = [
        w("Hola", 0.0),
        {"text": " ", "type": "spacing", "start": None, "end": None},
        w("a todos.", 0.6),
    ]
    [segment] = words_to_segments(words)
    assert (segment.text, segment.start, segment.end) == ("Hola a todos.", 0.0, 1.0)


# --- API call ---------------------------------------------------------------


@respx.mock
def test_sends_audio_with_diarization_and_builds_transcript(audio):
    route = respx.post(API_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "language_code": "spa",
                "text": "Hola.",
                "words": [w("Hola.", 0.0)],
                "audio_duration_secs": 11.5,
            },
        )
    )
    transcript = transcriber().transcribe(audio)

    request = route.calls[0].request
    assert request.headers["xi-api-key"] == "xi-KEY"
    body = request.content.decode(errors="replace")
    for field, value in [
        ("model_id", MODEL_ID),
        ("diarize", "true"),
        ("language_code", "es"),
        ("timestamps_granularity", "word"),
        ("tag_audio_events", "false"),
    ]:
        assert f'name="{field}"\r\n\r\n{value}' in body
    assert 'name="file"; filename="audio.flac"' in body
    assert (transcript.provider, transcript.language, transcript.duration) == (
        "elevenlabs",
        "spa",
        11.5,
    )
    assert [s.text for s in transcript.segments] == ["Hola."]


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
def test_auth_errors_are_config_errors(audio, status):
    respx.post(API_URL).mock(return_value=httpx.Response(status, json={"detail": "x"}))
    with pytest.raises(TranscriptionConfigError, match="TRANSCRIPTION_API_KEY"):
        transcriber().transcribe(audio)


@respx.mock
def test_transient_errors_are_retried(audio):
    respx.post(API_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"words": [w("Hola.", 0)]})]
    )
    assert len(transcriber().transcribe(audio).segments) == 1


@pytest.mark.parametrize("payload", [{"words": None}, {"detail": "error"}, [], {"words": []}])
@respx.mock
def test_unusable_responses_raise(audio, payload):
    respx.post(API_URL).mock(return_value=httpx.Response(200, json=payload))
    with pytest.raises(TranscriptionError):
        transcriber().transcribe(audio)


# --- factory ----------------------------------------------------------------


def test_factory_builds_configured_provider():
    settings = Settings(_env_file=None, transcription_api_key="xi-KEY")
    assert isinstance(make_transcriber(settings), ElevenLabsTranscriber)


def test_factory_requires_key():
    with pytest.raises(Exception, match="TRANSCRIPTION_API_KEY"):
        make_transcriber(Settings(_env_file=None))
