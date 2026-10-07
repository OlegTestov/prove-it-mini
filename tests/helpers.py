"""Shared helpers for the kit's tests: temp git repos and fake CLIs. Standard library only."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
TOOLS = KIT / "tools"
PY = sys.executable


def run(cmd, cwd=None, stdin=None, env=None, timeout=120):
    full_env = dict(os.environ)
    full_env.pop("CLAUDE_PROJECT_DIR", None)
    full_env.update(env or {})
    return subprocess.run(cmd, cwd=cwd, input=stdin, capture_output=True, text=True, env=full_env,
                          timeout=timeout, shell=isinstance(cmd, str))


def git_repo(files: dict[str, str] | None = None) -> Path:
    root = Path(tempfile.mkdtemp(prefix="proveit-test-"))
    run(["git", "init", "-q", "-b", "main"], cwd=root)
    run(["git", "config", "user.email", "test@example.com"], cwd=root)
    run(["git", "config", "user.name", "Test"], cwd=root)
    for name, body in (files or {"README.md": "test\n"}).items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")
    run(["git", "add", "-A"], cwd=root)
    run(["git", "commit", "-q", "-m", "base"], cwd=root)
    return root


def fake_cli(dir_: Path, name: str, body: str) -> str:
    """Write an executable Python script; return a shell command that runs it."""
    p = dir_ / name
    p.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
    p.chmod(0o755)
    return f"{PY} {p}"
