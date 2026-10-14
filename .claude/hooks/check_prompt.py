"""PostToolUse: al editar prompts/*.md, comprueba neutralidad y recuerda la evaluación.

Usa pipeline.neutrality, así que debe ejecutarse con el Python del .venv del proyecto.
"""

import json
import os
import sys
from pathlib import Path

from pipeline.neutrality import neutrality_problems


def main() -> None:
    event = json.load(sys.stdin)
    path = Path(event.get("tool_input", {}).get("file_path", ""))
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path.cwd())).resolve()

    if path.suffix != ".md" or path.resolve().parent != project / "prompts" or not path.is_file():
        return

    problems = neutrality_problems(path.read_text(encoding="utf-8"))
    reminder = (
        f"Has modificado {path.name}. Según CLAUDE.md, ejecuta la evaluación "
        "(uv run python eval/run_eval.py) y reporta las métricas antes/después."
    )
    if problems:
        json.dump(
            {
                "decision": "block",
                "reason": (
                    f"{path.name} incumple la neutralidad de CLAUDE.md: {'; '.join(problems)}. "
                    f"Reescríbelo sin esas palabras. {reminder}"
                ),
            },
            sys.stdout,
        )
        return

    json.dump(
        {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": reminder}},
        sys.stdout,
    )


if __name__ == "__main__":
    main()
