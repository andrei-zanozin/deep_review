from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from deep_review.fix_verifier import _has_current_reviewer_reply, plan_reconciliation, reconcile
from deep_review.infrastructure import AgentRunner
from deep_review.models import DiscoveryResult, FixVerifierDecision, PullRequestTarget


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
    def fix_verifier(self, _: dict[str, Any], __: Path) -> FixVerifierDecision:
        return FixVerifierDecision(comment_id=7, action="resolve", evidence="The defect is fixed.")


def test_fix_verifier_resolves_and_verifies_each_comment() -> None:
    commands = SecondaryCommands()
    result = reconcile(
        {"key": "ABC-123", "description": "Story"},
        [],
        "diff --git a/code.py blk b/code.py",
        DiscoveryResult.model_validate(
            {
                "reviewer": {"username": "REVIEWER"},
                "requestor": {"username": "requestor"},
                "review_type": "fix_verifier",
            }
        ),
        PullRequestTarget(
            id=3,
            project="PRJ",
            repository="repo",
            source_branch="feature",
            target_branch="main",
            reviewed_head="a" * 40,
            reviewed_base="b" * 40,
        ),
        Path("."),
        commands,
        cast(AgentRunner, SecondaryAgent()),
    )
    assert result == "No issues found"
    assert [name for name, _ in commands.calls].count("set_comment_resolved") == 1
    assert [name for name, _ in commands.calls].count("get_pull_request_comments") == 3


def test_reply_authors_match_jira_usernames_regardless_of_case() -> None:
    discovery = DiscoveryResult.model_validate(
        {
            "reviewer": {"username": "ZAO3FE"},
            "requestor": {"username": "KFV1KOR"},
            "review_type": "fix_verifier",
        }
    )
    root = {
        "replies": [
            {"author": {"slug": "kfv1kor"}, "created_at": 1},
            {"author": {"slug": "zao3fe"}, "created_at": 2},
        ]
    }

    assert _has_current_reviewer_reply(root, discovery)


def test_fix_verifier_planning_does_not_mutate_comments() -> None:
    commands = SecondaryCommands()
    discovery = DiscoveryResult.model_validate(
        {
            "reviewer": {"username": "reviewer"},
            "requestor": {"username": "requestor"},
            "review_type": "fix_verifier",
        }
    )
    target = PullRequestTarget(
        id=3,
        project="PRJ",
        repository="repo",
        source_branch="feature",
        target_branch="main",
        reviewed_head="a" * 40,
        reviewed_base="b" * 40,
    )

    decisions, roots = plan_reconciliation(
        {"key": "ABC-123"},
        [],
        "diff",
        discovery,
        target,
        Path("."),
        commands,
        cast(AgentRunner, SecondaryAgent()),
    )

    assert [decision.action for decision in decisions] == ["resolve"]
    assert [root["id"] for root in roots] == [7]
    assert not any(name == "set_comment_resolved" for name, _ in commands.calls)
