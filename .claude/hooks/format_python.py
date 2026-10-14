"""PostToolUse: formatea y corrige con ruff el .py que Claude acaba de escribir.

Si quedan errores que ruff no puede arreglar, sale con código 2 para que Claude los vea.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    event = json.load(sys.stdin)
    path = Path(event.get("tool_input", {}).get("file_path", ""))
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path.cwd()))
    # The venv's layout differs: bin/ on macOS and Linux, Scripts/ on Windows.
    candidates = [project / ".venv" / "bin" / "ruff", project / ".venv" / "Scripts" / "ruff.exe"]
    ruff = next((c for c in candidates if c.exists()), None)

    if path.suffix != ".py" or not path.is_file() or ruff is None:
        return
    if not path.resolve().is_relative_to(project.resolve()):
        return  # only this repo's files follow its ruff config

    # Fix what ruff can, format, then report only what is still wrong.
    subprocess.run([str(ruff), "check", "--fix", "--quiet", str(path)], check=False, cwd=project)
    subprocess.run([str(ruff), "format", "--quiet", str(path)], check=False, cwd=project)
    check = subprocess.run(
        [str(ruff), "check", "--quiet", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        cwd=project,
    )
    if check.returncode != 0:
        print(f"ruff encontró errores en {path.name}:\n{check.stdout}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
