from __future__ import annotations

from pathlib import Path
from typing import Any

from deep_review.models import DiscoveryResult, PullRequestTarget, SecondaryDecision
from deep_review.secondary import plan_reconciliation, reconcile


class SecondaryCommands:
    def __init__(self) -> None:
        self.resolved = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def jira(self, name: str, arguments: dict[str, Any]) -> Any:
        raise AssertionError((name, arguments))

    def bitbucket(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if name == "get_pull_request_comments":
            return {
                "comments": [
                    {
                        "id": 7,
                        "resolved": self.resolved,
                        "author": {"slug": "reviewer"},
                        "replies": [],
                    }
                ],
                "next_cursor": None,
            }
        if name == "set_comment_resolved":
            self.resolved = True
            return {"changed": True}
        raise AssertionError((name, arguments))


class SecondaryAgent:
    def secondary(self, _: dict[str, Any], __: Path) -> SecondaryDecision:
        return SecondaryDecision(comment_id=7, action="resolve", evidence="The defect is fixed.")


def test_secondary_resolves_and_verifies_each_comment() -> None:
    commands = SecondaryCommands()
    result = reconcile(
        {"key": "ABC-123", "description": "Story"},
        [],
        "diff --git a/code.py blk b/code.py",
        DiscoveryResult(
            reviewer={"username": "reviewer"},
            requestor={"username": "requestor"},
            review_type="secondary",
        ),
        PullRequestTarget(
            id=3,
            project="PRJ",
            repository="repo",
            source_branch="feature",
            target_branch="main",
            reviewed_head="a" * 40,
        ),
        Path("."),
        commands,
        SecondaryAgent(),
    )
    assert result == "No issues found"
    assert [name for name, _ in commands.calls].count("set_comment_resolved") == 1
    assert [name for name, _ in commands.calls].count("get_pull_request_comments") == 3


def test_secondary_planning_does_not_mutate_comments() -> None:
    commands = SecondaryCommands()
    discovery = DiscoveryResult(
        reviewer={"username": "reviewer"},
        requestor={"username": "requestor"},
        review_type="secondary",
    )
    target = PullRequestTarget(
        id=3,
        project="PRJ",
        repository="repo",
        source_branch="feature",
        target_branch="main",
        reviewed_head="a" * 40,
    )

    decisions, roots = plan_reconciliation(
        {"key": "ABC-123"},
        [],
        "diff",
        discovery,
        target,
        Path("."),
        commands,
        SecondaryAgent(),
    )

    assert [decision.action for decision in decisions] == ["resolve"]
    assert [root["id"] for root in roots] == [7]
    assert not any(name == "set_comment_resolved" for name, _ in commands.calls)
