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
    ruff = project / ".venv" / "bin" / "ruff"

    if path.suffix != ".py" or not path.is_file() or not ruff.exists():
        return
    if not path.resolve().is_relative_to(project.resolve()):
        return  # only this repo's files follow its ruff config

    check = subprocess.run(
        [str(ruff), "check", "--fix", "--quiet", str(path)],
        capture_output=True,
        text=True,
        check=False,
        cwd=project,
    )
    subprocess.run([str(ruff), "format", "--quiet", str(path)], check=False, cwd=project)
    if check.returncode != 0:
        print(f"ruff encontró errores en {path.name}:\n{check.stdout}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
