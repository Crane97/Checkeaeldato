"""Sesiones en directo de la web local: arranque, instantánea, eventos (SSE) y control.

Una sesión a la vez: el pipeline y ffmpeg ya ocupan la máquina de desarrollo. Los eventos
se sirven por Server-Sent Events y la instantánea completa por JSON, para que el panel se
reconstruya al recargar la página."""

import datetime as dt
import json
import logging
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from pipeline.governments import Governments, default_governments
from pipeline.live.session import LiveEvent, LiveRunner, LiveState, LiveStore, new_session
from pipeline.live.source import (
    DEFAULT_BLOCK_SECONDS,
    MAX_BLOCK_SECONDS,
    MIN_BLOCK_SECONDS,
    BlockSource,
    FfmpegBlockSource,
    is_local_file,
)
from pipeline.models import SpeakerInfo
from pipeline.run import Deps
from pipeline.speakers import parse_speaker_lines

logger = logging.getLogger(__name__)

STREAM_SCHEMES = ("http://", "https://", "rtmp://", "rtmps://", "rtsp://", "udp://", "srt://")
# Seconds an SSE connection waits for a new event before sending a keep-alive comment.
EVENT_WAIT_SECONDS = 15.0

# (source, blocks dir, block seconds) -> BlockSource
SourceFactory = Callable[[str, Path, int], BlockSource]


def ffmpeg_source(source: str, out_dir: Path, block_seconds: int) -> BlockSource:
    return FfmpegBlockSource(source, out_dir, block_seconds=block_seconds)


class LiveRequest(BaseModel):
    fuente: str  # stream URL or path of a recorded file (played at its own pace)
    fecha: str
    bloque_segundos: int = Field(
        default=DEFAULT_BLOCK_SECONDS, ge=MIN_BLOCK_SECONDS, le=MAX_BLOCK_SECONDS
    )
    oradores: str | None = None  # "Nombre | Partido | Rol" per line


def sse(event: LiveEvent) -> str:
    """One event in the text/event-stream format."""
    data = json.dumps(event.data, ensure_ascii=False)
    return f"id: {event.seq}\nevent: {event.type}\ndata: {data}\n\n"


def validate_source(source: str) -> str:
    source = source.strip()
    if not source:
        raise HTTPException(400, "Indica la URL de la emisión o la ruta de un vídeo")
    if source.lower().startswith(STREAM_SCHEMES) or is_local_file(source):
        return source
    if "://" in source:
        raise HTTPException(400, "Solo se admiten emisiones http(s), rtmp, rtsp, udp o srt")
    raise HTTPException(400, f"No existe el archivo {source}")


class LiveManager:
    def __init__(
        self,
        data_dir: Path,
        deps_factory: Callable[[], Deps],
        *,
        source_factory: SourceFactory = ffmpeg_source,
        run_in_thread: bool = True,
        governments: Governments | None = None,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self.store = LiveStore(data_dir / "live")
        self.deps_factory = deps_factory
        self.source_factory = source_factory
        self.run_in_thread = run_in_thread
        self.governments = governments or default_governments()
        self.now = now
        self.runners: dict[str, LiveRunner] = {}
        self._lock = threading.Lock()

    # --- lifecycle ------------------------------------------------------------

    def start(self, request: LiveRequest) -> str:
        source = validate_source(request.fuente)
        try:
            speech_date = dt.date.fromisoformat(request.fecha)
        except ValueError as exc:
            raise HTTPException(400, "La fecha debe tener formato AAAA-MM-DD") from exc
        info: dict[str, SpeakerInfo] = (
            parse_speaker_lines(request.oradores) if request.oradores else {}
        )
        with self._lock:
            if any(not r.finished for r in self.runners.values()):
                raise HTTPException(409, "Ya hay una sesión en directo; párala antes de otra")
            state = new_session(
                source,
                speech_date=speech_date,
                block_seconds=request.bloque_segundos,
                started_at=self.now(),
                speaker_info=info,
            )
            session_id = state.config.session_id
            block_source = self.source_factory(
                source, self.store.blocks_dir(session_id), request.bloque_segundos
            )
            runner = LiveRunner(
                state,
                self.deps_factory(),
                block_source,
                store=self.store,
                governments=self.governments,
                now=self.now,
            )
            self.runners[session_id] = runner
            self.store.save(state)
        if self.run_in_thread:
            runner.start()
        else:
            runner.run()
        return session_id

    def stop(self, session_id: str) -> None:
        runner = self.runners.get(session_id)
        if runner is None:
            raise HTTPException(404, "Sesión no encontrada o ya terminada")
        runner.stop()

    def rename_speaker(self, session_id: str, label: str, info: SpeakerInfo) -> dict[str, str]:
        runner = self.runners.get(session_id)
        if runner is not None:
            with runner.lock:
                runner.state.rename_speaker(label, info, self.governments)
                self.store.save(runner.state)
                names = runner.state.display_names()
            runner.emit("oradores", {"oradores": names})
            return names
        state = self._stored(session_id)
        state.rename_speaker(label, info, self.governments)
        self.store.save(state)
        return state.display_names()

    # --- reading --------------------------------------------------------------

    def _stored(self, session_id: str) -> LiveState:
        state = self.store.load(session_id)
        if state is None:
            raise HTTPException(404, "Sesión no encontrada")
        if state.status == "en_curso":  # the server died mid-session
            state.status, state.error = "error", "La sesión se interrumpió"
        return state

    def state(self, session_id: str) -> LiveState:
        runner = self.runners.get(session_id)
        return runner.state if runner else self._stored(session_id)

    def sessions(self) -> list[dict[str, Any]]:
        items = []
        for session_id in self.store.list_ids():
            state = self.state(session_id)
            items.append(
                {
                    "session_id": session_id,
                    "estado": state.status,
                    "fuente": state.config.source,
                    "fecha": state.config.speech_date.isoformat(),
                    "inicio": state.config.started_at.isoformat(),
                    "bloques": len(state.blocks),
                    "afirmaciones": len(state.detected),
                }
            )
        return sorted(items, key=lambda s: s["inicio"], reverse=True)

    def snapshot(self, session_id: str) -> dict[str, Any]:
        runner = self.runners.get(session_id)
        state = runner.state if runner else self._stored(session_id)
        lock = runner.lock if runner else threading.Lock()
        with lock:
            report = state.report(self.now())
            return {
                "session_id": session_id,
                "estado": state.status,
                "error": state.error,
                "fuente": state.config.source,
                "fecha": state.config.speech_date.isoformat(),
                "inicio": state.config.started_at.isoformat(),
                "bloque_segundos": state.config.block_seconds,
                "bloques": [b.model_dump() for b in state.blocks],
                "oradores": state.display_names(),
                "etiquetas": [s.label for s in state.speakers.speakers],
                "transcripcion": [s.model_dump() for s in state.transcript],
                "pendientes": [c.model_dump(mode="json") for c in state.pending()],
                "informe": report.model_dump(mode="json"),
                "ultimo_evento": len(runner.events) if runner else 0,
            }

    def events(self, session_id: str, after: int) -> Iterator[str]:
        """SSE stream from event `after` + 1 until the session reaches a final state."""
        runner = self.runners.get(session_id)
        if runner is None:
            state = self._stored(session_id)
            yield sse(
                LiveEvent(
                    seq=after + 1,
                    at=self.now(),
                    type="estado",
                    data={"estado": state.status, "error": state.error},
                )
            )
            return
        seq = after
        while True:
            batch = runner.wait_for(seq, EVENT_WAIT_SECONDS)
            if not batch:
                yield ": sigo aquí\n\n"
                continue
            for event in batch:
                yield sse(event)
                seq = event.seq
                if event.type == "estado" and event.data.get("estado") != "en_curso":
                    return
