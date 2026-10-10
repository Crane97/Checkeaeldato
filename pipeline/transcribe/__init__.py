"""Interfaz Transcriber y fábrica de proveedores."""

from typing import Protocol

from pipeline.config import Settings
from pipeline.models import AudioInfo, Transcript


class TranscriptionError(RuntimeError):
    """The audio could not be transcribed."""


class TranscriptionConfigError(TranscriptionError):
    """Bad key or permissions: retrying will not help."""


class Transcriber(Protocol):
    """Cloud speech-to-text with diarization and per-word or per-segment timestamps.

    Batch only for now; a streaming variant for live debates (phase 7) will be a
    separate interface producing the same Segment model.
    """

    name: str

    def transcribe(self, audio: AudioInfo) -> Transcript: ...


def make_transcriber(settings: Settings) -> Transcriber:
    from pipeline.transcribe.elevenlabs import ElevenLabsTranscriber

    providers = {"elevenlabs": ElevenLabsTranscriber}
    return providers[settings.transcription_provider](settings.secret("transcription_api_key"))
