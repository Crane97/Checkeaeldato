"""ElevenLabs Scribe v2 (por lotes, con diarización)."""

from pathlib import Path
from typing import Any

import httpx

from pipeline.http import request_with_retries
from pipeline.models import AudioInfo, Segment, Transcript
from pipeline.transcribe import TranscriptionConfigError, TranscriptionError

API_URL = "https://api.elevenlabs.io/v1/speech-to-text"
MODEL_ID = "scribe_v2"
UNKNOWN_SPEAKER = "speaker_desconocido"
SENTENCE_ENDS = (".", "?", "!", "…")
# A long turn is split at the first sentence end after this many characters.
MAX_SEGMENT_CHARS = 300
# Uploading and transcribing hours of audio can take minutes.
TIMEOUT = httpx.Timeout(60.0, read=1800.0)


def words_to_segments(words: list[Any], max_chars: int = MAX_SEGMENT_CHARS) -> list[Segment]:
    """Group Scribe words into segments: one per speaker turn, split at sentence ends."""
    segments: list[Segment] = []
    parts: list[str] = []
    speaker: str | None = None
    start: float | None = None
    end: float | None = None
    last_time = 0.0

    def flush() -> None:
        nonlocal parts, start, end
        text = "".join(parts).strip()
        if text and speaker is not None and start is not None:
            segments.append(
                Segment(speaker=speaker, text=text, start=start, end=max(end or start, start))
            )
        parts, start, end = [], None, None

    for word in words:
        if not isinstance(word, dict):
            continue
        kind = word.get("type", "word")
        text = str(word.get("text") or "")
        if kind == "audio_event":
            continue
        if kind == "spacing":
            if parts:
                parts.append(text)
            continue

        word_speaker = word.get("speaker_id") or UNKNOWN_SPEAKER
        if parts and word_speaker != speaker:
            flush()
        speaker = word_speaker
        word_start, word_end = word.get("start"), word.get("end")
        if start is None:
            start = word_start if word_start is not None else last_time
        end = word_end if word_end is not None else (word_start if word_start is not None else end)
        last_time = end if end is not None else last_time
        parts.append(text)

        if text.rstrip().endswith(SENTENCE_ENDS) and len("".join(parts)) >= max_chars:
            flush()
    flush()
    return segments


class ElevenLabsTranscriber:
    name = "elevenlabs"

    def __init__(
        self,
        api_key: str,
        client: httpx.Client | None = None,
        max_attempts: int = 2,
        backoff: float = 1.0,
    ) -> None:
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=TIMEOUT)
        self._max_attempts = max_attempts
        self._backoff = backoff

    def transcribe(self, audio: AudioInfo) -> Transcript:
        path = Path(audio.path)
        response = request_with_retries(
            self._client,
            "POST",
            API_URL,
            max_attempts=self._max_attempts,
            backoff=self._backoff,
            error_cls=TranscriptionError,
            headers={"xi-api-key": self._api_key},
            data={
                "model_id": MODEL_ID,
                "language_code": "es",
                "diarize": "true",
                "timestamps_granularity": "word",
                "tag_audio_events": "false",
            },
            # Bytes, not a file handle, so a retry can resend the upload.
            files={"file": (path.name, path.read_bytes(), "audio/flac")},
        )
        if response.status_code in (401, 403):
            raise TranscriptionConfigError(
                f"ElevenLabs respondió {response.status_code}: revisa TRANSCRIPTION_API_KEY"
            )
        if response.status_code != 200:
            raise TranscriptionError(f"ElevenLabs respondió HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise TranscriptionError("respuesta de ElevenLabs no es JSON") from exc
        if (
            not isinstance(data, dict)
            or "detail" in data
            or not isinstance(data.get("words"), list)
        ):
            raise TranscriptionError("respuesta inesperada de ElevenLabs")

        segments = words_to_segments(data["words"])
        if not segments:
            raise TranscriptionError("La transcripción está vacía: ¿el vídeo tiene voz?")
        return Transcript(
            provider=self.name,
            language=str(data.get("language_code") or "es"),
            duration=data.get("audio_duration_secs") or audio.duration,
            segments=segments,
        )
