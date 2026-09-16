from __future__ import annotations

import logging
from pathlib import Path

import pytest
from conftest import run_git

from deep_review.errors import WorkflowError
from deep_review.models import Finding, PullRequestTarget, Severity, Side
from deep_review.repository import (
    discover_repository,
    discover_sibling_repositories,
    finding_location_exists,
    location_in_diff,
    parse_bitbucket_remote,
    prepare_checkout,
    pull_request_diff,
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


def test_discovers_sibling_repositories_and_omits_ambiguous_identity(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    current = tmp_path / "current"
    sibling = tmp_path / "sibling"
    duplicate = tmp_path / "duplicate"
    create_repository(current, "PRJ", "current")
    create_repository(sibling, "PRJ", "shared")
    create_repository(duplicate, "prj", "SHARED")

    with caplog.at_level(logging.WARNING, logger="deep_review.repository"):
        repositories = discover_sibling_repositories(current)

    assert repositories.keys() == {("prj", "current")}
    messages = [record.getMessage() for record in caplog.records]
    ambiguity = next(
        message for message in messages if message.startswith("ambiguous local repository identity")
    )
    assert "normalized_identity=prj/shared" in ambiguity
    assert "candidate_identity=PRJ/shared" in ambiguity
    assert str(sibling) in ambiguity
    assert str(duplicate) in ambiguity


def test_logs_invalid_local_repository_candidate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    current = tmp_path / "current"
    invalid = tmp_path / "invalid"
    create_repository(current, "PRJ", "current")
    invalid.mkdir()
    (invalid / ".git").mkdir()

    with caplog.at_level(logging.WARNING, logger="deep_review.repository"):
        repositories = discover_sibling_repositories(current)

    assert repositories.keys() == {("prj", "current")}
    rejection = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("local repository candidate skipped")
    )
    assert f"path={invalid}" in rejection
    assert "reason=" in rejection


def test_prepare_checkout_rejects_dirty_repository(git_repository: Path) -> None:
    (git_repository / "code.txt").write_text("dirty\n", encoding="utf-8")
    target = PullRequestTarget(
        id=1,
        project="PRJ",
        repository="repository",
        source_branch="feature",
        target_branch="main",
        reviewed_head=run_git(git_repository, "rev-parse", "HEAD"),
        reviewed_base=run_git(git_repository, "rev-parse", "HEAD"),
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
        severity=Severity.MAJOR,
        title="Changed line",
        path="code.txt",
        line=2,
        side=Side.DESTINATION,
        problem_and_impact="The added line has a problem.",
        suggested_fix="Correct it.",
        evidence="The diff adds the line.",
    )
    assert location_in_diff(diff, finding)
    assert location_in_diff(diff, finding.model_copy(update={"line": 1}))
    assert location_in_diff(
        diff,
        finding.model_copy(update={"line": 1, "side": "source"}),
    )
    assert not location_in_diff(diff, finding.model_copy(update={"line": 3}))


def test_local_location_validation_has_no_context_distance_limit(
    git_repository: Path,
) -> None:
    original = "\n".join(f"line {number}" for number in range(1, 1_502)) + "\n"
    (git_repository / "code.txt").write_text(original, encoding="utf-8")
    run_git(git_repository, "add", "code.txt")
    run_git(git_repository, "commit", "-m", "large base")
    base = run_git(git_repository, "rev-parse", "HEAD")
    (git_repository / "code.txt").write_text(
        original.replace("line 1\n", "changed line 1\n", 1),
        encoding="utf-8",
    )
    run_git(git_repository, "add", "code.txt")
    run_git(git_repository, "commit", "-m", "change first line")
    head = run_git(git_repository, "rev-parse", "HEAD")
    target = PullRequestTarget(
        id=1,
        project="PRJ",
        repository="repository",
        source_branch="main",
        target_branch="develop",
        reviewed_head=head,
        reviewed_base=base,
    )
    finding = Finding(
        severity=Severity.MAJOR,
        title="Distant existing line",
        path="code.txt",
        line=1_500,
        side=Side.DESTINATION,
        problem_and_impact="The line provides relevant evidence.",
        suggested_fix="Correct the affected behavior.",
        evidence="The line exists at the reviewed head.",
    )

    assert finding_location_exists(git_repository, target, finding)
    changed_diff = pull_request_diff(git_repository, target, unified=0)
    assert not location_in_diff(changed_diff, finding)


def test_local_diff_validates_late_added_line_and_new_file_line(
    git_repository: Path,
) -> None:
    (git_repository / "production.txt").write_text(
        "\n".join(f"base {number}" for number in range(1, 395)) + "\n",
        encoding="utf-8",
    )
    run_git(git_repository, "add", "production.txt")
    run_git(git_repository, "commit", "-m", "production base")
    base = run_git(git_repository, "rev-parse", "HEAD")
    with (git_repository / "production.txt").open("a", encoding="utf-8") as stream:
        stream.write("added line 395\n")
    (git_repository / "new-test.txt").write_text(
        "\n".join(f"test {number}" for number in range(1, 19)) + "\n",
        encoding="utf-8",
    )
    run_git(git_repository, "add", "production.txt", "new-test.txt")
    run_git(git_repository, "commit", "-m", "reviewed change")
    head = run_git(git_repository, "rev-parse", "HEAD")
    target = PullRequestTarget(
        id=1,
        project="PRJ",
        repository="repository",
        source_branch="main",
        target_branch="develop",
        reviewed_head=head,
        reviewed_base=base,
    )

    assert "added line 395" in pull_request_diff(git_repository, target)
    for path, line in (("production.txt", 395), ("new-test.txt", 18)):
        finding = Finding(
            severity=Severity.MAJOR,
            title="Changed line",
            path=path,
            line=line,
            side=Side.DESTINATION,
            problem_and_impact="The changed line has a problem.",
            suggested_fix="Correct it.",
            evidence="The line is added by the pull request.",
        )
        assert finding_location_exists(git_repository, target, finding)
        assert location_in_diff(pull_request_diff(git_repository, target, unified=0), finding)


def test_local_diff_classifies_removed_and_unchanged_source_lines(
    git_repository: Path,
) -> None:
    (git_repository / "code.txt").write_text("old\nkeep\n", encoding="utf-8")
    run_git(git_repository, "add", "code.txt")
    run_git(git_repository, "commit", "-m", "source base")
    base = run_git(git_repository, "rev-parse", "HEAD")
    (git_repository / "code.txt").write_text("new\nkeep\n", encoding="utf-8")
    run_git(git_repository, "add", "code.txt")
    run_git(git_repository, "commit", "-m", "replace first line")
    head = run_git(git_repository, "rev-parse", "HEAD")
    target = PullRequestTarget(
        id=1,
        project="PRJ",
        repository="repository",
        source_branch="main",
        target_branch="develop",
        reviewed_head=head,
        reviewed_base=base,
    )
    removed = Finding(
        severity=Severity.MAJOR,
        title="Removed line",
        path="code.txt",
        line=1,
        side=Side.SOURCE,
        problem_and_impact="The removed behavior is required.",
        suggested_fix="Restore it.",
        evidence="The pull request removes the line.",
    )
    unchanged = removed.model_copy(update={"line": 2})
    changed_diff = pull_request_diff(git_repository, target, unified=0)

    assert finding_location_exists(git_repository, target, removed)
    assert location_in_diff(changed_diff, removed)
    assert finding_location_exists(git_repository, target, unchanged)
    assert not location_in_diff(changed_diff, unchanged)


def test_binary_finding_location_is_invalid(git_repository: Path) -> None:
    (git_repository / "binary.dat").write_bytes(b"first\0second")
    run_git(git_repository, "add", "binary.dat")
    run_git(git_repository, "commit", "-m", "binary")
    head = run_git(git_repository, "rev-parse", "HEAD")
    target = PullRequestTarget(
        id=1,
        project="PRJ",
        repository="repository",
        source_branch="main",
        target_branch="develop",
        reviewed_head=head,
        reviewed_base=head,
    )
    finding = Finding(
        severity=Severity.MAJOR,
        title="Binary location",
        path="binary.dat",
        line=1,
        side=Side.DESTINATION,
        problem_and_impact="Binary content cannot identify a text line.",
        suggested_fix="Use a text-file location.",
        evidence="The file contains binary data.",
    )

    assert not finding_location_exists(git_repository, target, finding)
