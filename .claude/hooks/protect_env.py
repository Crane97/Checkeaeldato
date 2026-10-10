"""PreToolUse: impide que Claude lea, edite o use en Bash el archivo .env (secretos).

.env.example sí se permite. Solo usa la biblioteca estándar.
"""

import json
import shlex
import sys
from pathlib import PurePath

# Shell punctuation glued to a path: "<.env", "$(cat .env)", "--env-file=.env".
_STRIP = "<>|&;()$`'\""


def is_secret_env(path: str) -> bool:
    name = PurePath(path).name
    return (name == ".env" or name.startswith(".env.")) and name != ".env.example"


def command_touches_env(command: str) -> bool:
    """True if any shell argument is a path to a secret .env file.

    Works on arguments, not raw text, so a commit message that merely mentions
    ".env" inside a quoted string is not blocked. A guard against accidents, not
    a security boundary.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        if is_secret_env(token.strip(_STRIP).rsplit("=", 1)[-1]):
            return True
        # Command substitution inside quotes still runs: check its inner command.
        if "$(" in token or "`" in token:
            inner = token.replace("$(", " ").replace(")", " ").replace("`", " ")
            if inner != token and command_touches_env(inner):
                return True
    return False


def main() -> None:
    event = json.load(sys.stdin)
    tool = event.get("tool_name", "")
    params = event.get("tool_input", {})

    if tool == "Bash":
        blocked = command_touches_env(params.get("command", ""))
    else:
        candidates = [params.get(k, "") for k in ("file_path", "path", "pattern", "glob")]
        blocked = any(c and is_secret_env(c) for c in candidates)

    if blocked:
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "El archivo .env contiene secretos y está protegido. Usa .env.example "
                        "para ver las variables; el código las lee con pipeline.config."
                    ),
                }
            },
            sys.stdout,
        )


if __name__ == "__main__":
    main()
