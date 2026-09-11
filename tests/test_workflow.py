from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import run_git

from deep_review.models import (
    AgentRole,
    ConsolidationResult,
    DiscoveryResult,
    LocationVerification,
    ReviewResult,
    SecondaryDecision,
    TicketCorrelationResult,
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

    def secondary(self, _: dict[str, Any], __: Path) -> SecondaryDecision:
        self.roles.append(AgentRole.SECONDARY)
        return SecondaryDecision(comment_id=1, action="resolve", evidence="Fixed.")

    def architecture(self, _: dict[str, Any], __: Path) -> ReviewResult:
        self.roles.append(AgentRole.ARCHITECTURE)
        if not self.has_finding:
            return ReviewResult(status="no_issues", coverage=["architecture"])
        return ReviewResult(
            status="findings",
            coverage=["architecture"],
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

    def unit(self, _: dict[str, Any], __: Path) -> ReviewResult:
        self.roles.append(AgentRole.UNIT)
        return ReviewResult(status="no_issues", coverage=["unit"])

    def code_polish(self, _: dict[str, Any], __: Path) -> ReviewResult:
        self.roles.append(AgentRole.CODE_POLISH)
        return ReviewResult(status="no_issues", coverage=["code_polish"])

    def consolidation(self, _: dict[str, Any]) -> ConsolidationResult:
        self.roles.append(AgentRole.CONSOLIDATION)
        return ConsolidationResult(
            selections=[
                {
                    "selected_id": "architecture:1",
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
                    "finding_id": "architecture:1",
                    "valid": True,
                    "reason": "The destination line contains the described value.",
                }
            ]
        )

    def ticket_correlation(self, _: dict[str, Any]) -> TicketCorrelationResult:
        self.roles.append(AgentRole.TICKET_CORRELATION)
        return TicketCorrelationResult()


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
    assert AgentRole.CONSOLIDATION not in agents.roles
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
    def __init__(self, heads: dict[str, str], approved_repository: str | None = None) -> None:
        super().__init__(next(iter(heads.values())))
        self.heads = heads
        self.approved_repository = approved_repository

    def bitbucket(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append(("bitbucket", name, arguments))
        if name == "search_review_pull_requests":
            return {
                "items": [
                    {
                        "id": index,
                        "project": "PRJ",
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

    def ticket_correlation(self, payload: dict[str, Any]) -> TicketCorrelationResult:
        self.payloads.setdefault(AgentRole.TICKET_CORRELATION, []).append(payload)
        return super().ticket_correlation(payload)


def create_repository(path: Path, repository: str) -> str:
    path.mkdir()
    run_git(path, "init", "-b", "main")
    run_git(path, "config", "user.name", "Test User")
    run_git(path, "config", "user.email", "test@example.com")
    (path / "code.txt").write_text(repository, encoding="utf-8")
    run_git(path, "add", "code.txt")
    run_git(path, "commit", "-m", "base")
    run_git(path, "remote", "add", "origin", f"ssh://git@example.test/PRJ/{repository}.git")
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
        AgentRole.ARCHITECTURE,
        AgentRole.UNIT,
        AgentRole.CODE_POLISH,
    }
    correlation = agents.payloads[AgentRole.TICKET_CORRELATION][0]["pull_requests"]
    assert len(correlation) == 2
    assert set(correlation[0]["specialist_results"]) == {
        "architecture",
        "unit",
        "code_polish",
    }
    assert correlation[1]["specialist_results"] == {}
    statuses = [call for call in commands.calls if call[1] == "set_review_status"]
    assert len(statuses) == 1


def test_missing_local_repository_publishes_valid_pr_and_reports_partial(tmp_path: Path) -> None:
    first = tmp_path / "repo-a"
    head = create_repository(first, "repo-a")
    commands = MultiRepoCommands({"repo-a": head, "missing": "c" * 40})

    result = execute_review("ABC-123", first, commands, FakeAgents(False))

    assert result.status == "partial"
    assert {item.key.repository: item.status for item in result.pull_requests} == {
        "repo-a": "published",
        "missing": "skipped",
    }
    jira_comments = [args for server, name, args in commands.calls if name == "add_comment"]
    assert "Deep review is incomplete" in jira_comments[-1]["body"]
    assert not any(name == "assign_issue" for _, name, _ in commands.calls)
