from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def run_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def git_repository(tmp_path: Path) -> Path:
    run_git(tmp_path, "init", "-b", "main")
    run_git(tmp_path, "config", "user.name", "Test User")
    run_git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / "code.txt").write_text("base\n", encoding="utf-8")
    run_git(tmp_path, "add", "code.txt")
    run_git(tmp_path, "commit", "-m", "base")
    run_git(tmp_path, "remote", "add", "origin", "ssh://git@example.test:7999/PRJ/repository.git")
    return tmp_path
