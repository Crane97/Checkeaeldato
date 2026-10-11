"""Envío del informe por correo (SMTP): resumen en el cuerpo + informe interactivo adjunto.

Los programas de correo bloquean JavaScript, así que el cuerpo es HTML estático; los filtros
por orador y categoría están en el adjunto `informe-<job_id>.html`, que se abre en el
navegador.
"""

import html
import re
import smtplib
from email.message import EmailMessage

from pipeline.config import ConfigError, Settings
from pipeline.models import CATEGORIES, Report
from pipeline.report import CATEGORY_NAMES
from pipeline.report_html import render_report_html, report_subtitle

SMTP_TIMEOUT_SECONDS = 30
# Light-theme colours of report.css, inlined because email clients drop <style>.
COLORS = {
    "verdadero": "#1f8a4c",
    "verdadero_sin_fuente_oficial": "#3f8f8a",
    "confusion": "#6b4fc7",
    "no_verificable": "#6b7280",
    "falso_sin_fuente_oficial": "#c2610f",
    "falso_con_fuente_oficial": "#c53030",
}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EmailError(RuntimeError):
    """The email could not be sent; the message is shown to the user."""


def is_valid_email(address: str) -> bool:
    return bool(_EMAIL_RE.fullmatch(address.strip())) and "\n" not in address


def _esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def _count_color(category: str, count: int) -> str:
    return COLORS[category] if count else "#5d6676"


def _plain_body(report: Report, attachment: str) -> str:
    lines = [f"Informe de verificación — {report_subtitle(report)}", ""]
    lines.append(f"Afirmaciones verificables: {len(report.claims)}")
    lines += [f"  {CATEGORY_NAMES[k]}: {report.by_category[k]}" for k in CATEGORIES]
    if len(report.by_speaker) > 1:
        lines += ["", "Por orador:"]
        for speaker, counts in report.by_speaker.items():
            parts = [f"{CATEGORY_NAMES[k]} {n}" for k, n in counts.items() if n]
            lines.append(f"  {speaker}: {', '.join(parts)}")
    lines += ["", f"Abre el adjunto {attachment} para filtrar por orador y por tipo.", ""]
    for c in report.claims:
        lines += [
            f"[{CATEGORY_NAMES[c.category]}] {report.speaker_name(c.speaker)}",
            f"  «{c.text_original}»",
            f"  {c.explanation}",
            *(f"  - {s.organization}: {s.url}" for s in c.sources),
            "",
        ]
    if report.warnings:
        lines += ["Avisos:", *(f"  - {w}" for w in report.warnings)]
    return "\n".join(lines)


def _html_body(report: Report, attachment: str) -> str:
    cell = "padding:4px 10px;border-bottom:1px solid #dde1e8;"
    summary = "".join(
        f'<tr><td style="{cell}color:{COLORS[k]};font-weight:700">{_esc(CATEGORY_NAMES[k])}</td>'
        f'<td style="{cell}text-align:right;font-weight:700">{report.by_category[k]}</td></tr>'
        for k in CATEGORIES
    )
    parts = [
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#1d2330;'
        'max-width:760px">',
        "<h2 style='margin:0 0 4px'>Informe de verificación</h2>",
        f"<p style='color:#5d6676;margin:0 0 16px'>{_esc(report_subtitle(report))}</p>",
        f"<p><strong>{len(report.claims)} afirmaciones verificables</strong></p>",
        f"<table style='border-collapse:collapse;margin-bottom:16px'>{summary}</table>",
    ]
    if len(report.by_speaker) > 1:
        head = "".join(
            f'<th style="{cell}color:#5d6676;font-size:12px">{_esc(CATEGORY_NAMES[k])}</th>'
            for k in CATEGORIES
        )
        rows = "".join(
            f'<tr><td style="{cell}font-weight:600">{_esc(speaker)}</td>'
            + "".join(
                f'<td style="{cell}text-align:center;color:{_count_color(k, counts[k])}">'
                f"{counts[k]}</td>"
                for k in CATEGORIES
            )
            + "</tr>"
            for speaker, counts in report.by_speaker.items()
        )
        parts += [
            "<h3 style='margin:16px 0 6px'>Por orador</h3>",
            f"<table style='border-collapse:collapse;font-size:14px'><tr><th style='{cell}'></th>"
            f"{head}</tr>{rows}</table>",
        ]
    parts.append(
        f"<p style='background:#eef0f3;padding:10px 12px;border-radius:8px'>Para filtrar por "
        f"orador y por tipo de afirmación, abre el adjunto <strong>{_esc(attachment)}</strong> "
        "en tu navegador.</p>"
    )
    for c in report.claims:
        color = COLORS[c.category]
        sources = "".join(
            f"<li><a href='{_esc(s.url)}' style='color:#2f5bd3'>{_esc(s.title)}</a> "
            f"({_esc(s.organization)})</li>"
            for s in c.sources
        )
        parts.append(
            f"<div style='border-left:4px solid {color};padding:6px 12px;margin:14px 0'>"
            f"<div><strong style='color:{color}'>{_esc(CATEGORY_NAMES[c.category])}</strong>"
            f" · {_esc(report.speaker_name(c.speaker))}</div>"
            f"<div style='font-style:italic;margin:4px 0'>«{_esc(c.text_original)}»</div>"
            f"<div>{_esc(c.explanation)}</div>"
            + (f"<ul style='margin:4px 0;font-size:14px'>{sources}</ul>" if sources else "")
            + "</div>"
        )
    if report.warnings:
        items = "".join(f"<li>{_esc(w)}</li>" for w in report.warnings)
        parts.append(f"<p style='color:#b7791f'>Avisos:</p><ul style='color:#b7791f'>{items}</ul>")
    parts.append("</div>")
    return "".join(parts)


def build_email(report: Report, *, to: str, sender: str) -> EmailMessage:
    attachment = f"informe-{report.job_id}.html"
    message = EmailMessage()
    message["Subject"] = (
        f"Informe de verificación: {len(report.claims)} afirmaciones · {report.source} "
        f"({report.speech_date.strftime('%d/%m/%Y')})"
    )
    message["From"] = sender
    message["To"] = to
    message.set_content(_plain_body(report, attachment))
    message.add_alternative(_html_body(report, attachment), subtype="html")
    message.add_attachment(
        render_report_html(report).encode(), maintype="text", subtype="html", filename=attachment
    )
    return message


def send_email(message: EmailMessage, settings: Settings) -> None:
    missing = [
        name
        for name, value in (
            ("SMTP_HOST", settings.smtp_host),
            ("SMTP_USER", settings.smtp_user),
            ("SMTP_PASSWORD", settings.smtp_password),
        )
        if not value
    ]
    if missing:
        raise ConfigError(f"Para enviar correos define en .env: {', '.join(missing)}")
    password = settings.secret("smtp_password")
    try:
        if settings.smtp_port == 465:
            with smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS
            ) as smtp:
                smtp.login(settings.smtp_user, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(
                settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS
            ) as smtp:
                smtp.starttls()
                smtp.login(settings.smtp_user, password)
                smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailError(
            "El servidor de correo rechazó el usuario o la contraseña (SMTP_USER / "
            "SMTP_PASSWORD; en Gmail hace falta una contraseña de aplicación)"
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"No se pudo enviar el correo: {exc}") from exc


def email_report(report: Report, to: str, settings: Settings) -> None:
    sender = settings.email_from or settings.smtp_user or ""
    send_email(build_email(report, to=to.strip(), sender=sender), settings)
