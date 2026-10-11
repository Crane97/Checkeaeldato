"""Web local para probar el pipeline: `uv run verificar-web` → http://localhost:8000.

Solo escucha en 127.0.0.1. Un análisis a la vez, en un hilo aparte; la página consulta
el estado cada segundo. Los resultados se guardan como en la CLI, en data/<job_id>/.
"""

import datetime as dt
import hashlib
import json
import logging
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pipeline.email_report import is_valid_email
from pipeline.jobs import JOB_ID_LENGTH, MANIFEST_FILE
from pipeline.models import STEPS, Report
from pipeline.report_html import render_report_html
from pipeline.run import Deps, run_job
from pipeline.speakers import parse_speaker_lines

# (report, address) -> None; raises on failure.
EmailSender = Callable[[Report, str], None]

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 200_000
STATIC_DIR = Path(__file__).parent / "static"
HOST, PORT = "127.0.0.1", 8000


class AnalysisRequest(BaseModel):
    texto: str
    fecha: str
    autor: str | None = None
    email: str | None = None
    oradores: str | None = None  # "Nombre | Partido | Rol" per line
    recalcular: bool = False


@dataclass
class JobState:
    estado: str = "en_curso"  # en_curso | terminado | error
    progreso: str = "Preparando el análisis…"
    error: str | None = None
    # {"estado": pendiente | enviando | enviado | error, "destino": …, "detalle": …}
    correo: dict[str, Any] | None = None


class _ProgressHandler(logging.Handler):
    """Keeps the latest pipeline log line of one worker thread as its progress."""

    def __init__(self, state: JobState, thread_id: int) -> None:
        super().__init__(level=logging.INFO)
        self.state, self.thread_id = state, thread_id

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread == self.thread_id:
            self.state.progreso = record.getMessage()


class JobManager:
    def __init__(
        self,
        data_dir: Path,
        deps_factory: Callable[[], Deps],
        run_in_thread: bool,
        email_sender: EmailSender | None,
    ) -> None:
        self.data_dir = data_dir
        self.deps_factory = deps_factory
        self.run_in_thread = run_in_thread
        self.email_sender = email_sender
        self.states: dict[str, JobState] = {}
        self.busy_with: str | None = None
        self._lock = threading.Lock()

    def start(self, request: AnalysisRequest, speech_date: dt.date) -> str:
        text = request.texto.strip() + "\n"
        job_id = hashlib.sha256(text.encode()).hexdigest()[:JOB_ID_LENGTH]
        with self._lock:
            if self.busy_with is not None:
                raise HTTPException(409, "Ya hay un análisis en curso; espera a que termine")
            self.busy_with = job_id
            self.states[job_id] = JobState()
        email = (request.email or "").strip()
        if email:
            self.states[job_id].correo = {"estado": "pendiente", "destino": email, "detalle": None}

        inputs = self.data_dir / "web"
        inputs.mkdir(parents=True, exist_ok=True)
        path = inputs / f"{job_id}.txt"
        path.write_text(text)

        def work() -> None:
            state = self.states[job_id]
            handler = _ProgressHandler(state, threading.get_ident())
            logging.getLogger("pipeline").addHandler(handler)
            try:
                result = run_job(
                    path,
                    speech_date=speech_date,
                    data_dir=self.data_dir,
                    deps=self.deps_factory(),
                    author=(request.autor or "").strip() or None,
                    speaker_info=(
                        parse_speaker_lines(request.oradores) if request.oradores else None
                    ),
                    # From the start: re-reading text is free and picks up parser changes.
                    from_step="transcript" if request.recalcular else None,
                )
                state.estado, state.progreso = "terminado", "Análisis terminado"
                if state.correo is not None:
                    self._send(state, result.report)
            except Exception as exc:  # shown to the user; details in the server log
                logger.exception("El análisis %s falló", job_id)
                state.estado, state.error = "error", str(exc) or type(exc).__name__
            finally:
                logging.getLogger("pipeline").removeHandler(handler)
                self.busy_with = None

        if self.run_in_thread:
            threading.Thread(target=work, daemon=True, name=f"analisis-{job_id}").start()
        else:
            work()
        return job_id

    def _send(self, state: JobState, report: Report) -> None:
        """Email the report; a failure is shown to the user but keeps the results."""
        assert state.correo is not None
        state.correo["estado"] = "enviando"
        try:
            if self.email_sender is None:
                raise RuntimeError("El envío de correo no está configurado")
            self.email_sender(report, state.correo["destino"])
            state.correo["estado"] = "enviado"
        except Exception as exc:
            logger.exception("No se pudo enviar el informe a %s", state.correo["destino"])
            state.correo.update(estado="error", detalle=str(exc) or type(exc).__name__)

    def report(self, job_id: str) -> Report:
        path = self.data_dir / job_id / "report.json"
        if not path.exists():
            raise HTTPException(404, "Informe no encontrado")
        return Report.model_validate_json(path.read_text())

    def status(self, job_id: str) -> dict[str, Any]:
        job_dir = self.data_dir / job_id
        manifest_path = job_dir / MANIFEST_FILE
        state = self.states.get(job_id)
        if state is None and not manifest_path.exists():
            raise HTTPException(404, "Análisis no encontrado")

        steps: dict[str, Any] = {}
        kind = "texto"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            steps, kind = manifest.get("steps", {}), manifest.get("input_kind", "texto")
        report_path = job_dir / "report.json"
        if state is None:  # e.g. after a server restart
            state = JobState(estado="terminado" if report_path.exists() else "error")

        names = [s for s in STEPS if kind == "video" or s != "audio"]
        pasos = [{"paso": n, "estado": steps.get(n, {}).get("status", "pending")} for n in names]
        if state.estado == "en_curso":
            current = next((p for p in pasos if p["estado"] != "done"), None)
            if current:
                current["estado"] = "running"

        done = state.estado == "terminado" and report_path.exists()
        return {
            "job_id": job_id,
            "estado": state.estado,
            "progreso": state.progreso,
            "error": state.error,
            "correo": state.correo,
            "pasos": pasos,
            "informe": json.loads(report_path.read_text()) if done else None,
        }


def create_app(
    *,
    data_dir: Path,
    deps_factory: Callable[[], Deps],
    run_in_thread: bool = True,
    email_sender: EmailSender | None = None,
) -> FastAPI:
    logging.getLogger("pipeline").setLevel(logging.INFO)
    app = FastAPI(title="Checkea el Dato (local)", docs_url=None, redoc_url=None)
    app.state.jobs = JobManager(data_dir, deps_factory, run_in_thread, email_sender)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def home() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.post("/api/analisis", status_code=202)
    def start(request: AnalysisRequest) -> dict[str, str]:
        if not request.texto.strip():
            raise HTTPException(400, "El texto está vacío")
        if len(request.texto) > MAX_TEXT_CHARS:
            raise HTTPException(
                400, f"El texto es demasiado largo (máximo {MAX_TEXT_CHARS} caracteres)"
            )
        try:
            speech_date = dt.date.fromisoformat(request.fecha)
        except ValueError as exc:
            raise HTTPException(400, "La fecha debe tener formato AAAA-MM-DD") from exc
        if request.email and request.email.strip() and not is_valid_email(request.email):
            raise HTTPException(400, "La dirección de correo no es válida")
        return {"job_id": app.state.jobs.start(request, speech_date)}

    def checked(job_id: str) -> str:
        if not (len(job_id) == JOB_ID_LENGTH and job_id.isalnum()):
            raise HTTPException(404, "Análisis no encontrado")
        return job_id

    @app.get("/api/analisis/{job_id}")
    def status(job_id: str) -> dict[str, Any]:
        return app.state.jobs.status(checked(job_id))

    @app.get("/api/analisis/{job_id}/informe.html")
    def download(job_id: str) -> HTMLResponse:
        report = app.state.jobs.report(checked(job_id))
        return HTMLResponse(
            render_report_html(report),
            headers={"Content-Disposition": f'attachment; filename="informe-{job_id}.html"'},
        )

    return app


def main() -> None:
    import uvicorn

    from pipeline.config import get_settings
    from pipeline.run import LiveDeps

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = get_settings()
    from pipeline.email_report import email_report

    app = create_app(
        data_dir=settings.data_dir,
        deps_factory=lambda: LiveDeps(settings),
        email_sender=lambda report, to: email_report(report, to, settings),
    )
    url = f"http://localhost:{PORT}"
    print(f"\nVerificador local en {url}  (Ctrl+C para parar)\n")
    threading.Timer(1.5, webbrowser.open, args=[url]).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
