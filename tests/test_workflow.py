from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from conftest import run_git

from deep_review.models import (
    AgentRole,
    ConsolidationResult,
    DiscoveryResult,
    LocationVerification,
    ReviewResult,
    FixVerifierDecision,
    CrossPrValidationResult,
)
from deep_review.workflow import execute_review


class FakeCommands:
    def __init__(self, head: str, diff: str = "No changes.") -> None:
        self.head = head
        self.diff = diff
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def jira(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append(("jira", name, arguments))
        if name == "get_issue":
            return {"key": "ABC-123", "assignee": {"name": "reviewer"}}
        if name == "get_issue_comments":
            return {
                "comments": [{"author": {"name": "requestor"}, "body": "Please review."}],
                "next_cursor": None,
            }
        if name in {"add_comment", "assign_issue"}:
            return {}
        raise AssertionError((name, arguments))

    def bitbucket(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append(("bitbucket", name, arguments))
        if name == "search_review_pull_requests":
            return {
                "items": [
                    {
                        "id": 4,
                        "project": "PRJ",
                        "repository": "repository",
                        "state": "OPEN",
                        "reviewers": [
                            {"user": {"slug": "reviewer"}, "status": "UNAPPROVED"}
                        ],
                        "source": {"name": "main", "commit": self.head},
                        "target": {"name": "develop", "commit": "b" * 40},
                    }
                ],
                "next_cursor": None,
            }
        if name == "get_pull_request":
            return {"id": 4, "source": {"name": "main", "commit": self.head}}
        if name == "get_pull_request_diff":
            return self.diff
        if name in {"add_pull_request_comment", "set_review_status"}:
            return {}
        raise AssertionError((name, arguments))


class FakeAgents:
    def __init__(self, has_finding: bool) -> None:
        self.has_finding = has_finding
        self.roles: list[AgentRole] = []

    def discovery(self, _: dict[str, Any]) -> DiscoveryResult:
        self.roles.append(AgentRole.DISCOVERY)
        return DiscoveryResult(
            reviewer={"username": "reviewer"},
            requestor={"username": "requestor"},
            review_type="primary",
        )

    def fix_verifier(self, _: dict[str, Any], __: Path) -> FixVerifierDecision:
        self.roles.append(AgentRole.FIX_VERIFIER)
        return FixVerifierDecision(comment_id=1, action="resolve", evidence="Fixed.")

    def architecture_expert(self, _: dict[str, Any], __: Path) -> ReviewResult:
        self.roles.append(AgentRole.ARCHITECTURE_EXPERT)
        if not self.has_finding:
            return ReviewResult(status="no_issues", coverage=["architecture_expert"])
        return ReviewResult(
            status="findings",
            coverage=["architecture_expert"],
            findings=[
                {
                    "severity": "Major",
                    "title": "Bad added value",
                    "path": "code.txt",
                    "line": 2,
                    "side": "destination",
                    "problem_and_impact": "The value breaks the required behavior.",
                    "suggested_fix": "Use the required value.",
                    "evidence": "The added line contains the invalid value.",
                }
            ],
        )

    def implementation_expert(self, _: dict[str, Any], __: Path) -> ReviewResult:
        self.roles.append(AgentRole.IMPLEMENTATION_EXPERT)
        return ReviewResult(status="no_issues", coverage=["implementation_expert"])

    def code_polish_expert(self, _: dict[str, Any], __: Path) -> ReviewResult:
        self.roles.append(AgentRole.CODE_POLISH_EXPERT)
        return ReviewResult(status="no_issues", coverage=["code_polish_expert"])

    def consolidator(self, _: dict[str, Any]) -> ConsolidationResult:
        self.roles.append(AgentRole.CONSOLIDATOR)
        return ConsolidationResult(
            selections=[
                {
                    "selected_id": "architecture_expert:1",
                    "duplicate_ids": [],
                    "severity": "Major",
                }
            ]
        )

    def location_verifier(self, _: dict[str, Any], __: Path) -> LocationVerification:
        self.roles.append(AgentRole.LOCATION_VERIFIER)
        return LocationVerification(
            decisions=[
                {
                    "finding_id": "architecture_expert:1",
                    "valid": True,
                    "reason": "The destination line contains the described value.",
                }
            ]
        )

    def cross_pr_validator(self, _: dict[str, Any]) -> CrossPrValidationResult:
        self.roles.append(AgentRole.CROSS_PR_VALIDATOR)
        return CrossPrValidationResult()


def test_primary_no_issues_approves_and_finishes_jira(git_repository: Path) -> None:
    head = run_git(git_repository, "rev-parse", "HEAD")
    commands = FakeCommands(head)
    agents = FakeAgents(has_finding=False)

    execute_review("ABC-123", git_repository, commands, agents)

    statuses = [
        args["status"]
        for server, name, args in commands.calls
        if name == "set_review_status"
    ]
    assert statuses == ["APPROVED"]
    jira_comment = next(args for server, name, args in commands.calls if name == "add_comment")
    assert jira_comment["body"] == "Hi [~requestor], review is done ✅"
    assert AgentRole.CONSOLIDATOR not in agents.roles
    assert AgentRole.LOCATION_VERIFIER not in agents.roles


def test_findings_are_preflighted_before_mocked_publication(git_repository: Path) -> None:
    base = run_git(git_repository, "rev-parse", "HEAD")
    (git_repository / "code.txt").write_text("base\nbad\n", encoding="utf-8")
    run_git(git_repository, "add", "code.txt")
    run_git(git_repository, "commit", "-m", "reviewed")
    head = run_git(git_repository, "rev-parse", "HEAD")
    diff = run_git(git_repository, "diff", base, head) + "\n"
    commands = FakeCommands(head, diff)

    execute_review("ABC-123", git_repository, commands, FakeAgents(has_finding=True))

    comment = next(
        args for server, name, args in commands.calls if name == "add_pull_request_comment"
    )
    assert comment["anchor"] == {"path": "code.txt", "line": 2, "side": "destination"}
    assert "Location:" not in comment["text"]
    status = next(args for server, name, args in commands.calls if name == "set_review_status")
    assert status["status"] == "NEEDS_WORK"
    jira_comment = next(args for server, name, args in commands.calls if name == "add_comment")
    assert "please check my findings" in jira_comment["body"]


def test_execute_review_does_not_construct_runtime_dependencies(
    git_repository: Path,
) -> None:
    head = run_git(git_repository, "rev-parse", "HEAD")
    execute_review("ABC-123", git_repository, FakeCommands(head), FakeAgents(False))


def test_truncated_diff_stops_before_specialist_agents(git_repository: Path) -> None:
    head = run_git(git_repository, "rev-parse", "HEAD")
    commands = FakeCommands(head, "[Warning: Bitbucket truncated this diff.]\npartial")
    agents = FakeAgents(False)

    result = execute_review("ABC-123", git_repository, commands, agents)

    assert agents.roles == [AgentRole.DISCOVERY]
    assert result.status == "failed"
    assert "truncated" in result.failures[0]
    assert not any(name == "set_review_status" for _, name, _ in commands.calls)


class MultiRepoCommands(FakeCommands):
    def __init__(
        self,
        heads: dict[str, str],
        approved_repository: str | None = None,
        project: str = "PRJ",
    ) -> None:
        super().__init__(next(iter(heads.values())))
        self.heads = heads
        self.approved_repository = approved_repository
        self.project = project

    def bitbucket(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append(("bitbucket", name, arguments))
        if name == "search_review_pull_requests":
            return {
                "items": [
                    {
                        "id": index,
                        "project": self.project,
                        "repository": repository,
                        "state": "OPEN",
                        "reviewers": [
                            {
                                "user": {"slug": "reviewer"},
                                "status": (
                                    "APPROVED"
                                    if repository == self.approved_repository
                                    else "UNAPPROVED"
                                ),
                            }
                        ],
                        "source": {"name": "main", "commit": head},
                        "target": {"name": "develop", "commit": "b" * 40},
                    }
                    for index, (repository, head) in enumerate(self.heads.items(), start=1)
                ],
                "next_cursor": None,
            }
        repository = str(arguments["repo"])
        if name == "get_pull_request":
            return {"source": {"name": "main", "commit": self.heads[repository]}}
        if name == "get_pull_request_diff":
            return "No changes."
        if name in {"add_pull_request_comment", "set_review_status"}:
            return {}
        raise AssertionError((name, arguments))


class CapturingAgents(FakeAgents):
    def __init__(self) -> None:
        super().__init__(False)
        self.payloads: dict[AgentRole, list[dict[str, Any]]] = {}

    def cross_pr_validator(self, payload: dict[str, Any]) -> CrossPrValidationResult:
        self.payloads.setdefault(AgentRole.CROSS_PR_VALIDATOR, []).append(payload)
        return super().cross_pr_validator(payload)


def create_repository(path: Path, repository: str, project: str = "PRJ") -> str:
    path.mkdir()
    run_git(path, "init", "-b", "main")
    run_git(path, "config", "user.name", "Test User")
    run_git(path, "config", "user.email", "test@example.com")
    (path / "code.txt").write_text(repository, encoding="utf-8")
    run_git(path, "add", "code.txt")
    run_git(path, "commit", "-m", "base")
    run_git(path, "remote", "add", "origin", f"ssh://git@example.test/{project}/{repository}.git")
    return run_git(path, "rev-parse", "HEAD")


def test_ticket_context_correlates_reviewed_and_evidence_only_prs(tmp_path: Path) -> None:
    first = tmp_path / "repo-a"
    second = tmp_path / "repo-b"
    heads = {
        "repo-a": create_repository(first, "repo-a"),
        "repo-b": create_repository(second, "repo-b"),
    }
    commands = MultiRepoCommands(heads, approved_repository="repo-b")
    agents = CapturingAgents()

    result = execute_review("ABC-123", first, commands, agents)

    assert result.status == "complete"
    assert [item.mode for item in result.pull_requests] == ["review", "evidence_only"]
    assert result.pull_requests[0].status == "published"
    assert result.pull_requests[1].status == "prepared"
    assert set(result.pull_requests[0].specialist_results) == {
        AgentRole.ARCHITECTURE_EXPERT,
        AgentRole.IMPLEMENTATION_EXPERT,
        AgentRole.CODE_POLISH_EXPERT,
    }
    correlation = agents.payloads[AgentRole.CROSS_PR_VALIDATOR][0]["pull_requests"]
    assert len(correlation) == 2
    assert set(correlation[0]["specialist_results"]) == {
        "architecture_expert",
        "implementation_expert",
        "code_polish_expert",
    }
    assert correlation[1]["specialist_results"] == {}
    statuses = [call for call in commands.calls if call[1] == "set_review_status"]
    assert len(statuses) == 1


def test_missing_local_repository_publishes_valid_pr_and_reports_partial(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    first = tmp_path / "repo-a"
    head = create_repository(first, "repo-a")
    commands = MultiRepoCommands({"repo-a": head, "missing": "c" * 40})

    with caplog.at_level(logging.WARNING, logger="deep_review.workflow"):
        result = execute_review("ABC-123", first, commands, FakeAgents(False))

    assert result.status == "partial"
    assert {item.key.repository: item.status for item in result.pull_requests} == {
        "repo-a": "published",
        "missing": "skipped",
    }
    jira_comments = [args for server, name, args in commands.calls if name == "add_comment"]
    assert "Deep review is incomplete" in jira_comments[-1]["body"]
    assert not any(name == "assign_issue" for _, name, _ in commands.calls)
    warning = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("local checkout lookup failed")
    )
    assert "expected identity=PRJ/missing" in warning
    assert f"discovered repositories=PRJ/repo-a at {first}" in warning


def test_local_repository_matching_is_case_insensitive(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repository = tmp_path / "repository"
    head = create_repository(repository, "repository", project="prj")
    commands = MultiRepoCommands({"repository": head}, project="PRJ")

    with caplog.at_level(logging.INFO):
        result = execute_review("ABC-123", repository, commands, FakeAgents(False))

    assert result.status == "complete"
    assert result.pull_requests[0].repository is not None
    assert result.pull_requests[0].repository.root == repository
    assert not [
        record for record in caplog.records if record.name == "deep_review.repository"
    ]
    assert not any(
        record.getMessage().startswith("local checkout lookup failed")
        for record in caplog.records
    )
