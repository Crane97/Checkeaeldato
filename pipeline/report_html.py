"""Informe HTML autocontenido e interactivo (filtros por categoría y orador).

Es un único archivo, sin dependencias externas: se adjunta al correo y se puede abrir sin
conexión. Usa los mismos estilos y el mismo código de pintado que la web local.
"""

import html
import re
from pathlib import Path

from pipeline.models import Report

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
_PLACEHOLDER_RE = re.compile(r"__(CSS|JS|TITLE|SUBTITLE|GENERATED|REPORT_JSON)__")


def _script_safe_json(report: Report) -> str:
    """Report JSON that cannot close the <script> tag it lives in, whatever it contains."""
    return (
        report.model_dump_json()
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def report_subtitle(report: Report) -> str:
    kind = "vídeo" if report.input_kind == "video" else "texto"
    return f"{report.source} · {kind} del {report.speech_date.strftime('%d/%m/%Y')}"


def render_report_html(report: Report) -> str:
    values = {
        "CSS": (STATIC_DIR / "report.css").read_text(encoding="utf-8"),
        "JS": (STATIC_DIR / "report.js").read_text(encoding="utf-8"),
        "TITLE": html.escape(report.source),
        "SUBTITLE": html.escape(report_subtitle(report)),
        "GENERATED": report.generated_at.strftime("%d/%m/%Y %H:%M UTC"),
        "REPORT_JSON": _script_safe_json(report),
    }
    template = (STATIC_DIR / "informe.html").read_text(encoding="utf-8")
    # One pass, so placeholder-like text inside the data is never substituted again.
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)
