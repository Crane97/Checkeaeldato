"""Entrada de línea de comandos: `uv run verificar <video>`."""

from pathlib import Path

import typer

app = typer.Typer(add_completion=False)


@app.command()
def main(video: Path) -> None:
    """Analiza un vídeo y genera data/<job_id>/report.json."""
    typer.echo("Pipeline aún no implementado.")
    raise typer.Exit(code=1)
