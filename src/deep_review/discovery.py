from __future__ import annotations

from pathlib import Path
from typing import Any

from deep_review.errors import WorkflowError
from deep_review.infrastructure import AgentRunner, Commands
from deep_review.models import (
    DiscoveryResult,
    PrReviewContext,
    PullRequestTarget,
    RepositoryIdentity,
    same_username,
)
from deep_review.repository import discover_repository


def discover(
    issue_key: str,
    start: Path,
    commands: Commands,
    agents: AgentRunner,
) -> tuple[RepositoryIdentity, dict[str, Any], list[dict[str, Any]], DiscoveryResult]:
    repository = discover_repository(start)
    issue = _mapping(commands.jira("get_issue", {"issue": issue_key}), "Jira issue")
    comments = all_pages(
        lambda cursor: commands.jira(
            "get_issue_comments", {"issue": issue_key, "cursor": cursor, "limit": 100}
        ),
        "comments",
    )
    result = agents.discovery({"issue": issue, "comments": comments})
    _validate_people(issue, comments, result)
    return repository, issue, comments, result


def find_targets(
    issue_key: str,
    reviewer: str,
    commands: Commands,
) -> list[PrReviewContext]:
    pull_requests = all_pages(
        lambda cursor: commands.bitbucket(
            "search_review_pull_requests",
            {
                "text": issue_key,
                "cursor": cursor,
                "limit": 100,
            },
        ),
        "items",
    )
    contexts = []
    seen: set[tuple[str, str, int]] = set()
    for pull_request in pull_requests:
        if pull_request.get("state") != "OPEN":
            continue
        target = _target(pull_request)
        identity = (target.project, target.repository, target.id)
        if identity in seen:
            continue
        seen.add(identity)
        contexts.append(
            PrReviewContext(
                key=target.key,
                target=target,
                metadata=pull_request,
                mode="evidence_only" if _approved_by(pull_request, reviewer) else "review",
            )
        )
    return sorted(
        contexts,
        key=lambda context: (
            context.key.project,
            context.key.repository,
            context.key.id,
        ),
    )


def all_pages(fetch: Any, field: str) -> list[dict[str, Any]]:
    cursor: int | None = 0
    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    while cursor is not None:
        if cursor in seen:
            raise WorkflowError("MCP pagination did not make progress")
        seen.add(cursor)
        page = _mapping(fetch(cursor), "MCP page")
        values = page.get(field)
        if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
            raise WorkflowError(f"MCP page has no valid {field} list")
        items.extend(values)
        cursor = page.get("next_cursor")
        if cursor is not None and (not isinstance(cursor, int) or isinstance(cursor, bool)):
            raise WorkflowError("MCP page has an invalid next cursor")
    return items


def _validate_people(
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    result: DiscoveryResult,
) -> None:
    assignee = issue.get("assignee")
    jira_username = assignee.get("name") if isinstance(assignee, dict) else None
    jira_display_name = assignee.get("displayName") if isinstance(assignee, dict) else None
    if not same_username(jira_username, result.reviewer.username):
        raise WorkflowError(
            "reviewer "
            f"(username={result.reviewer.username!r}, "
            f"display_name={result.reviewer.display_name!r}) "
            "does not match Jira assignee "
            f"(username={jira_username!r}, display_name={jira_display_name!r})"
        )
    authors = [
        author.get("name")
        for comment in comments
        if isinstance((author := comment.get("author")), dict)
    ]
    if not any(same_username(author, result.requestor.username) for author in authors):
        raise WorkflowError("discovery requestor is not a Jira comment author")


def _approved_by(pull_request: dict[str, Any], reviewer: str) -> bool:
    for participant in pull_request.get("reviewers", []):
        user = participant.get("user", {}) if isinstance(participant, dict) else {}
        if same_username(user.get("slug"), reviewer) and (
            participant.get("status") == "APPROVED" or participant.get("approved") is True
        ):
            return True
    return False


def _target(pull_request: dict[str, Any]) -> PullRequestTarget:
    source = pull_request.get("source")
    target = pull_request.get("target")
    if not isinstance(source, dict) or not isinstance(target, dict):
        raise WorkflowError("pull request has invalid branch metadata")
    try:
        return PullRequestTarget(
            id=pull_request["id"],
            project=pull_request["project"],
            repository=pull_request["repository"],
            source_branch=source["name"],
            target_branch=target["name"],
            reviewed_head=source["commit"],
            reviewed_base=target["commit"],
        )
    except (KeyError, ValueError) as exc:
        raise WorkflowError(f"pull request has incomplete metadata: {exc}") from exc


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowError(f"{label} is not an object")
    return value
