from __future__ import annotations

import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from conftest import run_git

from deep_review.models import (
    AgentRole,
    ConsolidationResult,
    CrossPrValidationResult,
    DiscoveryResult,
    FixVerifierDecision,
    JudgmentResult,
    ReviewResult,
    ReviewType,
)
from deep_review.workflow import _reviewer_has_reviewed_current_commit, execute_review


class FakeCommands:
    def __init__(self, head: str, base: str | None = None) -> None:
        self.head = head
        self.base = base or head
        self.metadata_head = self.head
        self.metadata_base = self.base
        self.reviewers: list[dict[str, Any]] = []
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
                        "reviewers": [{"user": {"slug": "reviewer"}, "status": "UNAPPROVED"}],
                        "source": {"name": "main", "commit": self.head},
                        "target": {"name": "develop", "commit": self.base},
                    }
                ],
                "next_cursor": None,
            }
        if name == "get_pull_request":
            return {
                "id": 4,
                "reviewers": deepcopy(self.reviewers),
                "source": {"name": "main", "commit": self.metadata_head},
                "target": {"name": "develop", "commit": self.metadata_base},
            }
        if name in {"add_pull_request_comment", "set_review_status"}:
            return {}
        raise AssertionError((name, arguments))


class FakeAgents:
    def __init__(self, has_finding: bool, finding_line: int = 2) -> None:
        self.has_finding = has_finding
        self.finding_line = finding_line
        self.roles: list[AgentRole] = []

    def discovery(self, context: dict[str, Any]) -> DiscoveryResult:
        self.roles.append(AgentRole.DISCOVERY)
        return DiscoveryResult.model_validate(
            {
                "reviewer": {"username": "reviewer"},
                "requestor": {"username": "requestor"},
                "review_type": "primary",
            }
        )

    def fix_verifier(self, context: dict[str, Any], repository_root: Path) -> FixVerifierDecision:
        self.roles.append(AgentRole.FIX_VERIFIER)
        return FixVerifierDecision(comment_id=1, action="resolve", evidence="Fixed.")

    def architecture_expert(self, context: dict[str, Any], repository_root: Path) -> ReviewResult:
        self.roles.append(AgentRole.ARCHITECTURE_EXPERT)
        if not self.has_finding:
            return ReviewResult(status="no_issues", coverage=["architecture_expert"])
        return ReviewResult.model_validate(
            {
                "status": "findings",
                "coverage": ["architecture_expert"],
                "findings": [
                    {
                        "severity": "Major",
                        "title": "Bad added value",
                        "path": "code.txt",
                        "line": self.finding_line,
                        "side": "destination",
                        "problem_and_impact": "The value breaks the required behavior.",
                        "suggested_fix": "Use the required value.",
                        "evidence": "The added line contains the invalid value.",
                    }
                ],
            }
        )

    def implementation_expert(self, context: dict[str, Any], repository_root: Path) -> ReviewResult:
        self.roles.append(AgentRole.IMPLEMENTATION_EXPERT)
        return ReviewResult(status="no_issues", coverage=["implementation_expert"])

    def code_polish_expert(self, context: dict[str, Any], repository_root: Path) -> ReviewResult:
        self.roles.append(AgentRole.CODE_POLISH_EXPERT)
        return ReviewResult(status="no_issues", coverage=["code_polish_expert"])

    def consolidator(self, context: dict[str, Any]) -> ConsolidationResult:
        self.roles.append(AgentRole.CONSOLIDATOR)
        return ConsolidationResult.model_validate(
            {
                "selections": [
                    {
                        "selected_id": "architecture_expert:1",
                        "duplicate_ids": [],
                        "severity": "Major",
                    }
                ]
            }
        )

    def judgment(self, context: dict[str, Any], repository_root: Path) -> JudgmentResult:
        self.roles.append(AgentRole.JUDGMENT)
        return JudgmentResult.model_validate(
            {
                "decisions": [
                    {
                        "candidate_id": candidate["id"],
                        "action": "keep",
                        "reason": "Material defect.",
                    }
                    for candidate in context["candidates"]
                ]
            }
        )

    def cross_pr_validator(self, context: dict[str, Any]) -> CrossPrValidationResult:
        self.roles.append(AgentRole.CROSS_PR_VALIDATOR)
        return CrossPrValidationResult()


def test_primary_no_issues_approves_and_finishes_jira(git_repository: Path) -> None:
    head = run_git(git_repository, "rev-parse", "HEAD")
    commands = FakeCommands(head)
    commands.reviewers = [
        {"user": {"slug": "REVIEWER"}, "last_reviewed_commit": head}
    ]
    agents = FakeAgents(has_finding=False)

    execute_review("ABC-123", git_repository, commands, agents)

    statuses = [
        args["status"] for server, name, args in commands.calls if name == "set_review_status"
    ]
    assert statuses == ["APPROVED"]
    jira_comment = next(args for server, name, args in commands.calls if name == "add_comment")
    assert jira_comment["body"] == "Hi [~requestor], review is done ✅"
    assert AgentRole.CONSOLIDATOR not in agents.roles
    assert AgentRole.JUDGMENT not in agents.roles
    assert {
        AgentRole.ARCHITECTURE_EXPERT,
        AgentRole.IMPLEMENTATION_EXPERT,
        AgentRole.CODE_POLISH_EXPERT,
    }.issubset(agents.roles)


@pytest.mark.parametrize(
    ("reviewers", "expected"),
    [
        ([{"user": {"slug": "REVIEWER"}, "last_reviewed_commit": "a" * 40}], True),
        ([{"user": {"slug": "reviewer"}, "last_reviewed_commit": "b" * 40}], False),
        ([{"user": {"slug": "reviewer"}}], False),
        ([{"user": {"slug": "reviewer"}, "last_reviewed_commit": "bad"}], False),
        ([], False),
        (
            [
                {"user": {"slug": "reviewer"}, "last_reviewed_commit": "a" * 40},
                {"user": {"slug": "REVIEWER"}, "last_reviewed_commit": "a" * 40},
            ],
            False,
        ),
        ([{"user": {"slug": "someone-else"}, "last_reviewed_commit": "a" * 40}], False),
    ],
)
def test_fix_verifier_specialist_skip_requires_one_matching_reviewer_commit(
    reviewers: list[dict[str, Any]], expected: bool
) -> None:
    reviewed_current_commit = _reviewer_has_reviewed_current_commit(
        {"reviewers": reviewers}, "reviewer", "a" * 40
    )

    assert reviewed_current_commit is expected


class ProxyCommands(FakeCommands):
    def __init__(self, head: str) -> None:
        super().__init__(head)
        self.reviewers = [
            {"user": {"slug": "reviewer"}, "last_reviewed_commit": head}
        ]
        self.threads = [
            {
                "id": comment_id,
                "resolved": False,
                "author": {"slug": "reviewer"},
                "replies": (
                    [
                        {
                            "id": comment_id + 10,
                            "author": {"slug": "original-author"},
                            "created_at": 10,
                        }
                    ]
                    if comment_id in {3, 4}
                    else []
                ),
            }
            for comment_id in range(1, 5)
        ]
        self.threads[3]["replies"].append(
            {"id": 24, "author": {"slug": "reviewer"}, "created_at": 11}
        )

    def jira(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "get_issue_comments":
            self.calls.append(("jira", name, arguments))
            return {
                "comments": [
                    {"author": {"name": "original-author"}, "body": "Please review."},
                    {"author": {"name": "proxy"}, "body": "Please review the fixes."},
                ],
                "next_cursor": None,
            }
        return super().jira(name, arguments)

    def bitbucket(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "get_pull_request_comments":
            self.calls.append(("bitbucket", name, arguments))
            return {"comments": deepcopy(self.threads), "next_cursor": None}
        if name == "set_comment_resolved":
            self.calls.append(("bitbucket", name, arguments))
            self.threads[arguments["comment_id"] - 1]["resolved"] = True
            return {"changed": True}
        if name == "add_pull_request_comment":
            self.calls.append(("bitbucket", name, arguments))
            self.threads[arguments["reply_to"] - 1]["replies"].append(
                {"id": 33, "author": {"slug": "reviewer"}, "created_at": 12}
            )
            return {"id": 33}
        return super().bitbucket(name, arguments)


class ProxyAgents(FakeAgents):
    def discovery(self, context: dict[str, Any]) -> DiscoveryResult:
        return DiscoveryResult.model_validate(
            {
                "reviewer": {"username": "reviewer"},
                "requestor": {"username": "proxy"},
                "review_type": "fix_verifier",
            }
        )

    def fix_verifier(self, context: dict[str, Any], repository_root: Path) -> FixVerifierDecision:
        comment_id = context["comment_thread"]["id"]
        if comment_id in {1, 2}:
            return FixVerifierDecision(comment_id=comment_id, action="resolve", evidence="Fixed.")
        if comment_id == 3:
            return FixVerifierDecision(
                comment_id=comment_id, action="reply", reply="Still open.", evidence="Code"
            )
        return FixVerifierDecision(comment_id=comment_id, action="no_action", evidence="Answered.")


def test_proxy_requestor_completes_fix_review_with_two_remaining_findings(
    git_repository: Path,
) -> None:
    head = run_git(git_repository, "rev-parse", "HEAD")
    commands = ProxyCommands(head)
    agents = ProxyAgents(False)
    result = execute_review("ABC-123", git_repository, commands, agents)

    assert result.status == "complete"
    assert result.pull_requests[0].fix_verifier_status == "Done"
    assert len(result.pull_requests[0].fix_verifier_decisions) == 4
    assert [thread["resolved"] for thread in commands.threads] == [True, True, False, False]
    assert [args["status"] for _, name, args in commands.calls if name == "set_review_status"] == [
        "NEEDS_WORK"
    ]
    assert [args["body"] for _, name, args in commands.calls if name == "add_comment"] == [
        "Hi [~proxy], please check my findings in the PR(s) comments."
    ]
    assert [args["username"] for _, name, args in commands.calls if name == "assign_issue"] == [
        "proxy"
    ]
    assert {
        AgentRole.ARCHITECTURE_EXPERT,
        AgentRole.IMPLEMENTATION_EXPERT,
        AgentRole.CODE_POLISH_EXPERT,
    }.isdisjoint(agents.roles)


def test_fix_review_runs_specialists_when_reviewer_last_reviewed_an_older_commit(
    git_repository: Path,
) -> None:
    head = run_git(git_repository, "rev-parse", "HEAD")
    commands = ProxyCommands(head)
    commands.reviewers[0]["last_reviewed_commit"] = "a" * 40
    agents = ProxyAgents(False)

    result = execute_review("ABC-123", git_repository, commands, agents)

    assert result.status == "complete"
    assert {
        AgentRole.ARCHITECTURE_EXPERT,
        AgentRole.IMPLEMENTATION_EXPERT,
        AgentRole.CODE_POLISH_EXPERT,
    }.issubset(agents.roles)


@pytest.mark.parametrize("changed_ref", ["metadata_head", "metadata_base"])
def test_fix_review_fails_when_fresh_metadata_does_not_match_prepared_target(
    git_repository: Path, changed_ref: str,
) -> None:
    head = run_git(git_repository, "rev-parse", "HEAD")
    commands = ProxyCommands(head)
    setattr(commands, changed_ref, "a" * 40)
    agents = ProxyAgents(False)

    result = execute_review("ABC-123", git_repository, commands, agents)

    assert result.status == "failed"
    assert "head or base changed before review" in result.failures[0]
    assert not any(name == "set_review_status" for _, name, _ in commands.calls)
    assert not any(name == "get_pull_request_comments" for _, name, _ in commands.calls)
    assert {
        AgentRole.ARCHITECTURE_EXPERT,
        AgentRole.IMPLEMENTATION_EXPERT,
        AgentRole.CODE_POLISH_EXPERT,
    }.isdisjoint(agents.roles)


def test_skipped_specialists_still_check_pr_freshness_before_reconciliation(
    git_repository: Path,
) -> None:
    class ChangedBeforePublication(ProxyCommands):
        def __init__(self, head: str) -> None:
            super().__init__(head)
            self.pr_reads = 0

        def bitbucket(self, name: str, arguments: dict[str, Any]) -> Any:
            if name == "get_pull_request":
                self.pr_reads += 1
                if self.pr_reads == 3:
                    self.metadata_head = "a" * 40
            return super().bitbucket(name, arguments)

    head = run_git(git_repository, "rev-parse", "HEAD")
    commands = ChangedBeforePublication(head)

    result = execute_review("ABC-123", git_repository, commands, ProxyAgents(False))

    assert result.status == "failed"
    assert "head or base changed after review" in result.failures[0]
    assert commands.pr_reads == 3
    assert not any(
        name in {"set_comment_resolved", "add_pull_request_comment", "set_review_status"}
        for _, name, _ in commands.calls
    )


def test_invalid_posted_reply_stops_review_status_update(git_repository: Path) -> None:
    class InvalidReplyCommands(ProxyCommands):
        def bitbucket(self, name: str, arguments: dict[str, Any]) -> Any:
            result = super().bitbucket(name, arguments)
            if name == "add_pull_request_comment":
                del self.threads[arguments["reply_to"] - 1]["replies"][-1]["created_at"]
            return result

    head = run_git(git_repository, "rev-parse", "HEAD")
    commands = InvalidReplyCommands(head)
    result = execute_review("ABC-123", git_repository, commands, ProxyAgents(False))

    assert result.status == "failed"
    assert "comment 3 (reply): reply has an invalid author or creation time" in result.failures[0]
    assert not any(name == "set_review_status" for _, name, _ in commands.calls)


def test_findings_are_preflighted_before_mocked_publication(
    git_repository: Path, caplog: pytest.LogCaptureFixture
) -> None:
    base = run_git(git_repository, "rev-parse", "HEAD")
    (git_repository / "code.txt").write_text("base\nbad\n", encoding="utf-8")
    run_git(git_repository, "add", "code.txt")
    run_git(git_repository, "commit", "-m", "reviewed")
    head = run_git(git_repository, "rev-parse", "HEAD")
    commands = FakeCommands(head, base)

    agents = FakeAgents(has_finding=True)
    with caplog.at_level(logging.INFO, logger="deep_review.publication"):
        execute_review("ABC-123", git_repository, commands, agents)

    assert not any(
        "preflighting finding location" in record.getMessage() for record in caplog.records
    )
    assert "posted issues: 1" in [record.getMessage() for record in caplog.records]
    comment = next(
        args for server, name, args in commands.calls if name == "add_pull_request_comment"
    )
    assert comment["anchor"] == {"path": "code.txt", "line": 2, "side": "destination"}
    assert "Location:" not in comment["text"]
    status = next(args for server, name, args in commands.calls if name == "set_review_status")
    assert status["status"] == "NEEDS_WORK"
    jira_comment = next(args for server, name, args in commands.calls if name == "add_comment")
    assert "please check my findings" in jira_comment["body"]


def test_judgment_can_discard_a_real_finding_and_approve(
    git_repository: Path, caplog: pytest.LogCaptureFixture
) -> None:
    base = run_git(git_repository, "rev-parse", "HEAD")
    (git_repository / "code.txt").write_text("base\nbad\n", encoding="utf-8")
    run_git(git_repository, "add", "code.txt")
    run_git(git_repository, "commit", "-m", "reviewed")
    commands = FakeCommands(run_git(git_repository, "rev-parse", "HEAD"), base)

    class DiscardingAgents(FakeAgents):
        def judgment(self, context: dict[str, Any], repository_root: Path) -> JudgmentResult:
            assert run_git(repository_root, "rev-parse", "HEAD") == commands.head
            assert context["specialist_results"]["architecture_expert"]["findings"]
            return JudgmentResult.model_validate(
                {
                    "decisions": [
                        {"candidate_id": item["id"], "action": "discard", "reason": "Low value."}
                        for item in context["candidates"]
                    ]
                }
            )

    with caplog.at_level(logging.INFO, logger="deep_review.review"):
        result = execute_review("ABC-123", git_repository, commands, DiscardingAgents(True))

    assert result.status == "complete"
    assert result.pull_requests[0].findings == []
    assert result.pull_requests[0].judgment_result is not None
    stage_logs = [
        record.getMessage()
        for record in caplog.records
        if record.name == "deep_review.review"
        and record.getMessage().startswith(("consolidator:", "judgment:"))
    ]
    assert stage_logs == [
        "consolidator: kept 1/1 findings [Major: 1]",
        "judgment: kept 0/1 findings []",
    ]
    assert not any("new consolidated findings" in record.getMessage() for record in caplog.records)
    assert not any(name == "add_pull_request_comment" for _, name, _ in commands.calls)
    assert [args["status"] for _, name, args in commands.calls if name == "set_review_status"] == [
        "APPROVED"
    ]


def test_judgment_failure_prevents_pr_publication(git_repository: Path) -> None:
    head = run_git(git_repository, "rev-parse", "HEAD")
    commands = FakeCommands(head)

    class FailingAgents(FakeAgents):
        def judgment(self, context: dict[str, Any], repository_root: Path) -> JudgmentResult:
            from deep_review.errors import WorkflowError

            raise WorkflowError("judgment unavailable")

    result = execute_review("ABC-123", git_repository, commands, FailingAgents(True))

    assert result.status == "failed"
    assert result.pull_requests[0].status == "failed"
    assert not any(
        name in {"add_pull_request_comment", "set_review_status"} for _, name, _ in commands.calls
    )


def test_execute_review_does_not_construct_runtime_dependencies(
    git_repository: Path,
) -> None:
    head = run_git(git_repository, "rev-parse", "HEAD")
    execute_review("ABC-123", git_repository, FakeCommands(head), FakeAgents(False))


def test_valid_unchanged_location_is_posted_as_general_comment(
    git_repository: Path,
) -> None:
    base = run_git(git_repository, "rev-parse", "HEAD")
    (git_repository / "code.txt").write_text("base\nbad\n", encoding="utf-8")
    run_git(git_repository, "add", "code.txt")
    run_git(git_repository, "commit", "-m", "reviewed")
    head = run_git(git_repository, "rev-parse", "HEAD")
    commands = FakeCommands(head, base)

    execute_review(
        "ABC-123",
        git_repository,
        commands,
        FakeAgents(has_finding=True, finding_line=1),
    )

    comment = next(args for _, name, args in commands.calls if name == "add_pull_request_comment")
    assert "anchor" not in comment
    assert "Location: code.txt:1 (destination)" in comment["text"]


def test_workflow_does_not_request_bitbucket_raw_diff(git_repository: Path) -> None:
    head = run_git(git_repository, "rev-parse", "HEAD")
    commands = FakeCommands(head)
    agents = FakeAgents(False)

    result = execute_review("ABC-123", git_repository, commands, agents)

    assert result.status == "complete"
    assert not any(name == "get_pull_request_diff" for _, name, _ in commands.calls)


def test_target_commit_drift_stops_publication(git_repository: Path) -> None:
    head = run_git(git_repository, "rev-parse", "HEAD")

    class DriftCommands(FakeCommands):
        def bitbucket(self, name: str, arguments: dict[str, Any]) -> Any:
            result = super().bitbucket(name, arguments)
            if name == "get_pull_request":
                result["target"]["commit"] = "f" * 40
            return result

    commands = DriftCommands(head)
    result = execute_review("ABC-123", git_repository, commands, FakeAgents(False))

    assert result.status == "failed"
    assert "head or base changed" in result.failures[0]
    assert not any(
        name in {"add_pull_request_comment", "set_review_status"} for _, name, _ in commands.calls
    )


def test_invalid_local_location_stops_publication(git_repository: Path) -> None:
    base = run_git(git_repository, "rev-parse", "HEAD")
    (git_repository / "code.txt").write_text("base\nbad\n", encoding="utf-8")
    run_git(git_repository, "add", "code.txt")
    run_git(git_repository, "commit", "-m", "reviewed")
    head = run_git(git_repository, "rev-parse", "HEAD")
    commands = FakeCommands(head, base)

    result = execute_review(
        "ABC-123",
        git_repository,
        commands,
        FakeAgents(has_finding=True, finding_line=3),
    )

    assert result.status == "failed"
    assert "invalid locations: architecture_expert:1" in result.failures[0]
    assert not any(
        name in {"add_pull_request_comment", "set_review_status"} for _, name, _ in commands.calls
    )


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
                        "target": {"name": "develop", "commit": head},
                    }
                    for index, (repository, head) in enumerate(self.heads.items(), start=1)
                ],
                "next_cursor": None,
            }
        repository = str(arguments["repo"])
        if name == "get_pull_request":
            return {
                "source": {"name": "main", "commit": self.heads[repository]},
                "target": {"name": "develop", "commit": self.heads[repository]},
            }
        if name in {"add_pull_request_comment", "set_review_status"}:
            return {}
        raise AssertionError((name, arguments))


class CapturingAgents(FakeAgents):
    def __init__(self) -> None:
        super().__init__(False)
        self.payloads: dict[AgentRole, list[dict[str, Any]]] = {}

    def cross_pr_validator(self, context: dict[str, Any]) -> CrossPrValidationResult:
        self.payloads.setdefault(AgentRole.CROSS_PR_VALIDATOR, []).append(context)
        return super().cross_pr_validator(context)


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


@pytest.mark.parametrize("review_type", ["primary", "fix_verifier"])
def test_stale_target_stops_review_before_agents_or_pr_mutations(
    git_repository: Path, review_type: str, caplog: pytest.LogCaptureFixture
) -> None:
    base = run_git(git_repository, "rev-parse", "HEAD")
    (git_repository / "code.txt").write_text("base\nfeature\n", encoding="utf-8")
    run_git(git_repository, "add", "code.txt")
    run_git(git_repository, "commit", "-m", "feature")
    feature = run_git(git_repository, "rev-parse", "HEAD")
    run_git(git_repository, "switch", "-c", "develop", base)
    (git_repository / "validation.txt").write_text("new validation\n", encoding="utf-8")
    (git_repository / "graphql.txt").write_text("new field\n", encoding="utf-8")
    run_git(git_repository, "add", "validation.txt", "graphql.txt")
    run_git(git_repository, "commit", "-m", "unrelated target additions")
    target = run_git(git_repository, "rev-parse", "HEAD")
    run_git(git_repository, "switch", "main")

    if review_type == "fix_verifier":
        commands = ProxyCommands(feature)
        commands.base = target
        commands.metadata_base = target
    else:
        commands = FakeCommands(feature, target)
    agents = ProxyAgents(False) if review_type == "fix_verifier" else FakeAgents(False)
    result = execute_review("ABC-123", git_repository, commands, agents)

    assert result.status == "failed"
    assert result.pull_requests[0].status == "failed"
    assert "PRJ/repository#4" in result.failures[0]
    assert f"main ({feature})" in result.failures[0]
    assert f"develop ({target})" in result.failures[0]
    assert "rebase onto or merge develop" in result.failures[0]
    assert set(agents.roles).issubset({AgentRole.DISCOVERY})
    assert not any(
        name
        in {
            "get_pull_request_comments",
            "add_pull_request_comment",
            "set_comment_resolved",
            "set_review_status",
            "assign_issue",
        }
        for _, name, _ in commands.calls
    )
    assert not any(name == "add_comment" for _, name, _ in commands.calls)
    failures = [
        record for record in caplog.records
        if record.name == "deep_review.workflow" and record.levelno == logging.ERROR
    ]
    assert len(failures) == 1
    assert "status failed" in failures[0].getMessage()
    assert "rebase onto or merge develop" in failures[0].getMessage()


def test_stale_evidence_only_pr_stops_all_review_agents(tmp_path: Path) -> None:
    first = tmp_path / "repo-a"
    second = tmp_path / "repo-b"
    first_head = create_repository(first, "repo-a")
    second_head = create_repository(second, "repo-b")
    run_git(second, "switch", "-c", "develop")
    (second / "target.txt").write_text("later target change\n", encoding="utf-8")
    run_git(second, "add", "target.txt")
    run_git(second, "commit", "-m", "later target change")
    second_base = run_git(second, "rev-parse", "HEAD")
    run_git(second, "switch", "main")

    class StaleEvidenceCommands(MultiRepoCommands):
        def bitbucket(self, name: str, arguments: dict[str, Any]) -> Any:
            result = super().bitbucket(name, arguments)
            if name == "search_review_pull_requests":
                result["items"][1]["target"]["commit"] = second_base
            elif name == "get_pull_request" and arguments["repo"] == "repo-b":
                result["target"]["commit"] = second_base
            return result

    commands = StaleEvidenceCommands(
        {"repo-a": first_head, "repo-b": second_head}, approved_repository="repo-b"
    )
    agents = CapturingAgents()
    result = execute_review("ABC-123", first, commands, agents)

    assert result.status == "failed"
    assert [item.status for item in result.pull_requests] == ["prepared", "failed"]
    assert result.pull_requests[1].mode == "evidence_only"
    assert agents.roles == [AgentRole.DISCOVERY]
    assert not any(
        name in {"add_pull_request_comment", "set_comment_resolved", "set_review_status"}
        for _, name, _ in commands.calls
    )


def test_cross_pr_validator_still_runs_when_fix_review_skips_specialists(tmp_path: Path) -> None:
    first = tmp_path / "repo-a"
    second = tmp_path / "repo-b"
    heads = {
        "repo-a": create_repository(first, "repo-a"),
        "repo-b": create_repository(second, "repo-b"),
    }

    class FixReviewCommands(MultiRepoCommands):
        def bitbucket(self, name: str, arguments: dict[str, Any]) -> Any:
            if name == "get_pull_request_comments":
                self.calls.append(("bitbucket", name, arguments))
                return {"comments": [], "next_cursor": None}
            if name == "get_pull_request":
                self.calls.append(("bitbucket", name, arguments))
                repository = str(arguments["repo"])
                return {
                    "reviewers": [
                        {
                            "user": {"slug": "reviewer"},
                            "last_reviewed_commit": heads[repository],
                        }
                    ],
                    "source": {"commit": heads[repository]},
                    "target": {"commit": heads[repository]},
                }
            return super().bitbucket(name, arguments)

    class FixReviewAgents(CapturingAgents):
        def discovery(self, context: dict[str, Any]) -> DiscoveryResult:
            result = super().discovery(context)
            return result.model_copy(update={"review_type": ReviewType.FIX_VERIFIER})

    commands = FixReviewCommands(heads, approved_repository="repo-b")
    agents = FixReviewAgents()

    result = execute_review("ABC-123", first, commands, agents)

    assert result.status == "complete"
    assert result.pull_requests[0].specialist_results == {}
    assert {
        AgentRole.ARCHITECTURE_EXPERT,
        AgentRole.IMPLEMENTATION_EXPERT,
        AgentRole.CODE_POLISH_EXPERT,
    }.isdisjoint(agents.roles)
    assert AgentRole.CROSS_PR_VALIDATOR in agents.payloads
    correlation = agents.payloads[AgentRole.CROSS_PR_VALIDATOR][0]["pull_requests"]
    assert correlation[0]["specialist_results"] == {}
    assert len(correlation) == 2


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
    assert not any(name == "add_comment" for _, name, _ in commands.calls)
    assert not any(name == "assign_issue" for _, name, _ in commands.calls)
    failures = [
        record for record in caplog.records
        if record.name == "deep_review.workflow" and record.levelno == logging.ERROR
    ]
    assert len(failures) == 1
    assert "status partial" in failures[0].getMessage()
    assert "Reviewed PRs: PRJ/repo-a#1" in failures[0].getMessage()
    assert "PRJ/missing" in failures[0].getMessage()
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
    assert not [record for record in caplog.records if record.name == "deep_review.repository"]
    assert not any(
        record.getMessage().startswith("local checkout lookup failed") for record in caplog.records
    )
