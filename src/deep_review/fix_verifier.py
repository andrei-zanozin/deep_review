from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any, Literal

from deep_review.discovery import all_pages
from deep_review.errors import WorkflowError
from deep_review.infrastructure import AgentRunner, Commands
from deep_review.models import DiscoveryResult, FixVerifierDecision, PullRequestTarget

FixVerifierStatus = Literal["No issues found", "Done"]

LOGGER = logging.getLogger(__name__)


def reconcile(
    issue: dict[str, Any],
    jira_comments: list[dict[str, Any]],
    diff: str,
    discovery: DiscoveryResult,
    target: PullRequestTarget,
    repository_root: Any,
    commands: Commands,
    agents: AgentRunner,
) -> FixVerifierStatus:
    decisions, _ = plan_reconciliation(
        issue,
        jira_comments,
        diff,
        discovery,
        target,
        repository_root,
        commands,
        agents,
    )
    return apply_reconciliation(target, decisions, discovery, commands)


def plan_reconciliation(
    issue: dict[str, Any],
    jira_comments: list[dict[str, Any]],
    diff: str,
    discovery: DiscoveryResult,
    target: PullRequestTarget,
    repository_root: Any,
    commands: Commands,
    agents: AgentRunner,
) -> tuple[list[FixVerifierDecision], list[dict[str, Any]]]:
    roots = _reviewer_roots(_comments(target, commands), discovery.reviewer.username)
    decisions: list[FixVerifierDecision] = []
    for root in roots:
        decision = agents.fix_verifier(
            {
                "issue": deepcopy(issue),
                "jira_comments": deepcopy(jira_comments),
                "diff": diff,
                "reviewer": discovery.reviewer.model_dump(),
                "requestor": discovery.requestor.model_dump(),
                "pull_request": target.model_dump(mode="json"),
                "comment_thread": deepcopy(root),
            },
            repository_root,
        )
        if decision.comment_id != root.get("id"):
            raise WorkflowError("fix_verifier decision references a different comment")
        decisions.append(decision)
    LOGGER.info(
        "fix_verifier: Resolved %d/%d",
        sum(decision.action == "resolve" for decision in decisions),
        len(roots),
    )
    return decisions, roots


def apply_reconciliation(
    target: PullRequestTarget,
    decisions: list[FixVerifierDecision],
    discovery: DiscoveryResult,
    commands: Commands,
) -> FixVerifierStatus:
    for decision in decisions:
        _apply_decision(target, decision, commands)
        _verify_decision(target, decision, discovery, commands)

    remaining = _reviewer_roots(_comments(target, commands), discovery.reviewer.username)
    if not remaining:
        return "No issues found"
    if all(_has_current_reviewer_reply(root, discovery) for root in remaining):
        return "Done"
    raise WorkflowError("fix_verifier review left a comment without the required reviewer reply")


def unresolved_reviewer_comments(
    target: PullRequestTarget, reviewer: str, commands: Commands
) -> list[dict[str, Any]]:
    return _reviewer_roots(_comments(target, commands), reviewer)


def _comments(target: PullRequestTarget, commands: Commands) -> list[dict[str, Any]]:
    return all_pages(
        lambda cursor: commands.bitbucket(
            "get_pull_request_comments",
            {**target.mcp_arguments(), "cursor": cursor, "limit": 100},
        ),
        "comments",
    )


def _reviewer_roots(comments: list[dict[str, Any]], reviewer: str) -> list[dict[str, Any]]:
    return [
        comment
        for comment in comments
        if not comment.get("resolved")
        and isinstance(comment.get("author"), dict)
        and comment["author"].get("slug") == reviewer
    ]


def _apply_decision(
    target: PullRequestTarget, decision: FixVerifierDecision, commands: Commands
) -> None:
    if decision.action == "resolve":
        commands.bitbucket(
            "set_comment_resolved",
            {**target.mcp_arguments(), "comment_id": decision.comment_id, "resolved": True},
        )
    elif decision.action == "reply":
        commands.bitbucket(
            "add_pull_request_comment",
            {
                **target.mcp_arguments(),
                "text": decision.reply,
                "reply_to": decision.comment_id,
            },
        )


def _verify_decision(
    target: PullRequestTarget,
    decision: FixVerifierDecision,
    discovery: DiscoveryResult,
    commands: Commands,
) -> None:
    root = next(
        (
            comment
            for comment in _comments(target, commands)
            if comment.get("id") == decision.comment_id
        ),
        None,
    )
    if root is None:
        raise WorkflowError("fix_verifier mutation verification could not find the root comment")
    if decision.action == "resolve" and not root.get("resolved"):
        raise WorkflowError("fix_verifier resolve action was not verified")
    if decision.action in {"reply", "no_action"} and not _has_current_reviewer_reply(
        root, discovery
    ):
        raise WorkflowError("fix_verifier reviewer reply was not verified")


def _has_current_reviewer_reply(root: dict[str, Any], discovery: DiscoveryResult) -> bool:
    replies = list(_walk_replies(root.get("replies", [])))
    requestor_times = [
        reply.get("created_at", -1)
        for reply in replies
        if _author(reply) == discovery.requestor.username
    ]
    reviewer_times = [
        reply.get("created_at", -1)
        for reply in replies
        if _author(reply) == discovery.reviewer.username
    ]
    if not requestor_times:
        return False
    latest_requestor = max(requestor_times)
    return any(time > latest_requestor for time in reviewer_times)


def _walk_replies(replies: list[Any]):
    for reply in replies:
        if isinstance(reply, dict):
            yield reply
            yield from _walk_replies(reply.get("replies", []))


def _author(comment: dict[str, Any]) -> str | None:
    author = comment.get("author")
    return author.get("slug") if isinstance(author, dict) else None
