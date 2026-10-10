import importlib.util
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "protect_env.py"
spec = importlib.util.spec_from_file_location("protect_env", HOOK)
protect_env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protect_env)


@pytest.mark.parametrize(
    "command",
    [
        "cat .env",
        "source .env && env",
        "cp .env.example .env",
        "grep KEY <.env",
        'echo "$(cat .env)"',
        "docker run --env-file=.env img",
        "less /Users/x/proyecto/.env.local",
    ],
)
def test_blocks_commands_that_use_env(command):
    assert protect_env.command_touches_env(command)


@pytest.mark.parametrize(
    "command",
    [
        "cat .env.example",
        "uv run pytest -q",
        'git commit -m "block access to .env (secrets)"',
        "ls ~/.local/share/uv-bootstrap",
        "echo 'unbalanced",
    ],
)
def test_allows_commands_that_only_mention_env(command):
    assert not protect_env.command_touches_env(command)


@pytest.mark.parametrize(
    ("path", "secret"),
    [(".env", True), ("/a/.env.local", True), (".env.example", False), ("env.py", False)],
)
def test_is_secret_env(path, secret):
    assert protect_env.is_secret_env(path) is secret
