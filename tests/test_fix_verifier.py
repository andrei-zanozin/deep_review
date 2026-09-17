from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

import pytest

from deep_review.errors import WorkflowError
from deep_review.fix_verifier import (
    _has_current_reviewer_reply,
    apply_reconciliation,
    plan_reconciliation,
    reconcile,
)
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


class MultipleCommentsCommands(SecondaryCommands):
    def __init__(self, comment_ids: tuple[int, ...]) -> None:
        super().__init__()
        self.comment_ids = comment_ids

    def bitbucket(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if name == "get_pull_request_comments":
            return {
                "comments": [
                    {
                        "id": comment_id,
                        "resolved": False,
                        "author": {"slug": "reviewer"},
                        "replies": [],
                    }
                    for comment_id in self.comment_ids
                ],
                "next_cursor": None,
            }
        raise AssertionError((name, arguments))


class MultipleCommentsAgent:
    def fix_verifier(self, context: dict[str, Any], _: Path) -> FixVerifierDecision:
        return FixVerifierDecision(
            comment_id=context["comment_thread"]["id"],
            action="resolve",
            evidence="The defect is fixed.",
        )


class FailingAgent:
    def fix_verifier(self, _: dict[str, Any], __: Path) -> FixVerifierDecision:
        raise WorkflowError("agent failed")


@pytest.mark.parametrize("comment_ids", [(7, 8), ()])
def test_fix_verifier_logs_one_step_for_all_comments(
    comment_ids: tuple[int, ...], caplog: pytest.LogCaptureFixture
) -> None:
    commands = MultipleCommentsCommands(comment_ids)
    target = PullRequestTarget(
        id=3,
        project="PRJ",
        repository="repo",
        source_branch="feature",
        target_branch="main",
        reviewed_head="a" * 40,
        reviewed_base="b" * 40,
    )
    discovery = DiscoveryResult.model_validate(
        {
            "reviewer": {"username": "reviewer"},
            "requestor": {"username": "requestor"},
            "review_type": "fix_verifier",
        }
    )

    with caplog.at_level(logging.INFO, logger="deep_review.fix_verifier"):
        decisions, _ = plan_reconciliation(
            {"key": "ABC-123"},
            [],
            "diff",
            discovery,
            target,
            Path("."),
            commands,
            cast(AgentRunner, MultipleCommentsAgent()),
        )

    messages = [record.getMessage() for record in caplog.records]
    step = "Reconcile an existing reviewer comment (agent: fix_verifier)"
    assert [decision.comment_id for decision in decisions] == list(comment_ids)
    assert messages.count(f"Starting workflow step: {step}") == 1
    assert messages.count(f"Finished workflow step: {step}") == 1


def test_fix_verifier_does_not_log_finished_step_after_an_agent_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="deep_review.fix_verifier"),
        pytest.raises(WorkflowError, match="agent failed"),
    ):
        plan_reconciliation(
            {"key": "ABC-123"},
            [],
            "diff",
            DiscoveryResult.model_validate(
                {
                    "reviewer": {"username": "reviewer"},
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
            SecondaryCommands(),
            cast(AgentRunner, FailingAgent()),
        )

    messages = [record.getMessage() for record in caplog.records]
    step = "Reconcile an existing reviewer comment (agent: fix_verifier)"
    assert messages.count(f"Starting workflow step: {step}") == 1
    assert f"Finished workflow step: {step}" not in messages


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


def test_reviewer_reply_matching_is_case_insensitive() -> None:
    discovery = DiscoveryResult.model_validate(
        {
            "reviewer": {"username": "ZAO3FE"},
            "requestor": {"username": "proxy"},
            "review_type": "fix_verifier",
        }
    )
    root = {
        "replies": [
            {"author": {"slug": "original-author"}, "created_at": 1},
            {"author": {"slug": "zao3fe"}, "created_at": 2},
        ]
    }

    assert _has_current_reviewer_reply(root, discovery)


def test_reply_from_original_author_counts_with_new_jira_requestor() -> None:
    discovery = DiscoveryResult.model_validate(
        {
            "reviewer": {"username": "REVIEWER"},
            "requestor": {"username": "proxy"},
            "review_type": "fix_verifier",
        }
    )
    root = {
        "id": 7,
        "replies": [
            {"author": {"slug": "original-author"}, "created_at": 1},
            {"author": {"slug": "reviewer"}, "created_at": 2},
        ],
    }

    assert _has_current_reviewer_reply(root, discovery)
    assert _has_current_reviewer_reply({"id": 7, "replies": []}, discovery)
    root["replies"].append({"author": {"slug": "another-contributor"}, "created_at": 3})
    assert not _has_current_reviewer_reply(root, discovery)


class ReplyCommands(SecondaryCommands):
    def __init__(self, *, visible: bool = True, author: str = "reviewer") -> None:
        super().__init__()
        self.visible = visible
        self.author = author
        self.replies: list[dict[str, Any]] = [
            {"id": 8, "author": {"slug": "original-author"}, "created_at": 1}
        ]

    def bitbucket(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "get_pull_request_comments":
            self.calls.append((name, arguments))
            return {
                "comments": [
                    {
                        "id": 7,
                        "resolved": False,
                        "author": {"slug": "reviewer"},
                        "replies": list(self.replies),
                    }
                ],
                "next_cursor": None,
            }
        if name == "add_pull_request_comment":
            self.calls.append((name, arguments))
            if self.visible:
                self.replies.append({"id": 9, "author": {"slug": self.author}, "created_at": 2})
            return {"id": 9}
        return super().bitbucket(name, arguments)


def test_reply_verifies_posted_comment_under_original_author_thread() -> None:
    commands = ReplyCommands()
    status = apply_reconciliation(
        PullRequestTarget(
            id=3,
            project="PRJ",
            repository="repo",
            source_branch="feature",
            target_branch="main",
            reviewed_head="a" * 40,
            reviewed_base="b" * 40,
        ),
        [FixVerifierDecision(comment_id=7, action="reply", reply="Still open.", evidence="Code")],
        DiscoveryResult.model_validate(
            {
                "reviewer": {"username": "reviewer"},
                "requestor": {"username": "proxy"},
                "review_type": "fix_verifier",
            }
        ),
        commands,
    )

    assert status == "Done"
    assert (
        "add_pull_request_comment",
        {"project": "PRJ", "repo": "repo", "pr_id": 3, "text": "Still open.", "reply_to": 7},
    ) in commands.calls


@pytest.mark.parametrize(("visible", "author"), [(False, "reviewer"), (True, "different-user")])
def test_reply_requires_posted_comment_to_be_visible_and_by_reviewer(
    visible: bool, author: str
) -> None:
    commands = ReplyCommands(visible=visible, author=author)
    target = PullRequestTarget(
        id=3,
        project="PRJ",
        repository="repo",
        source_branch="feature",
        target_branch="main",
        reviewed_head="a" * 40,
        reviewed_base="b" * 40,
    )
    discovery = DiscoveryResult.model_validate(
        {
            "reviewer": {"username": "reviewer"},
            "requestor": {"username": "proxy"},
            "review_type": "fix_verifier",
        }
    )

    with pytest.raises(
        WorkflowError, match=r"comment 7 \(reply\): reviewer reply was not verified"
    ):
        apply_reconciliation(
            target,
            [
                FixVerifierDecision(
                    comment_id=7, action="reply", reply="Still open.", evidence="Code"
                )
            ],
            discovery,
            commands,
        )


@pytest.mark.parametrize("malformed", [{"author": {"slug": "author"}}, {"created_at": 1}])
def test_invalid_reply_metadata_fails_with_comment_context(malformed: dict[str, Any]) -> None:
    discovery = DiscoveryResult.model_validate(
        {
            "reviewer": {"username": "reviewer"},
            "requestor": {"username": "proxy"},
            "review_type": "fix_verifier",
        }
    )
    with pytest.raises(WorkflowError, match=r"comment 7 \(no_action\): reply has an invalid"):
        _has_current_reviewer_reply(
            {"id": 7, "replies": [malformed]}, discovery, action="no_action"
        )


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
