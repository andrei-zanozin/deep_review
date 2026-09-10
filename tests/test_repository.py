from __future__ import annotations

from pathlib import Path

import pytest
from conftest import run_git

from deep_review.errors import WorkflowError
from deep_review.models import Finding, PullRequestTarget
from deep_review.repository import (
    discover_repository,
    discover_sibling_repositories,
    location_in_diff,
    parse_bitbucket_remote,
    prepare_checkout,
)


def create_repository(path: Path, project: str, repository: str) -> None:
    path.mkdir()
    run_git(path, "init", "-b", "main")
    run_git(path, "config", "user.name", "Test User")
    run_git(path, "config", "user.email", "test@example.com")
    (path / "file.txt").write_text("content", encoding="utf-8")
    run_git(path, "add", "file.txt")
    run_git(path, "commit", "-m", "base")
    run_git(path, "remote", "add", "origin", f"ssh://git@example.test/{project}/{repository}.git")


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://host/scm/PRJ/repo.git", ("PRJ", "repo")),
        ("ssh://git@host:7999/PRJ/repo.git", ("PRJ", "repo")),
        ("git@host:PRJ/repo.git", ("PRJ", "repo")),
    ],
)
def test_parse_bitbucket_remote(remote: str, expected: tuple[str, str]) -> None:
    assert parse_bitbucket_remote(remote) == expected


def test_discover_repository_uses_local_origin(git_repository: Path) -> None:
    result = discover_repository(git_repository)
    assert (result.root, result.project, result.repository) == (
        git_repository,
        "PRJ",
        "repository",
    )


def test_discovers_sibling_repositories_and_omits_ambiguous_identity(tmp_path: Path) -> None:
    current = tmp_path / "current"
    sibling = tmp_path / "sibling"
    duplicate = tmp_path / "duplicate"
    create_repository(current, "PRJ", "current")
    create_repository(sibling, "PRJ", "shared")
    create_repository(duplicate, "PRJ", "shared")

    repositories = discover_sibling_repositories(current)

    assert repositories.keys() == {("PRJ", "current")}


def test_prepare_checkout_rejects_dirty_repository(git_repository: Path) -> None:
    (git_repository / "code.txt").write_text("dirty\n", encoding="utf-8")
    target = PullRequestTarget(
        id=1,
        project="PRJ",
        repository="repository",
        source_branch="feature",
        target_branch="main",
        reviewed_head=run_git(git_repository, "rev-parse", "HEAD"),
    )
    with pytest.raises(WorkflowError, match="not clean"):
        prepare_checkout(git_repository, target)


def test_location_must_be_an_actual_changed_line(git_repository: Path) -> None:
    base = run_git(git_repository, "rev-parse", "HEAD")
    (git_repository / "code.txt").write_text("base\nadded\n", encoding="utf-8")
    run_git(git_repository, "add", "code.txt")
    run_git(git_repository, "commit", "-m", "change")
    diff = run_git(git_repository, "diff", base, "HEAD") + "\n"
    finding = Finding(
        severity="Major",
        title="Changed line",
        path="code.txt",
        line=2,
        side="destination",
        problem_and_impact="The added line has a problem.",
        suggested_fix="Correct it.",
        evidence="The diff adds the line.",
    )
    assert location_in_diff(diff, finding)
    assert location_in_diff(diff, finding.model_copy(update={"line": 1}))
    assert not location_in_diff(diff, finding.model_copy(update={"line": 3}))
