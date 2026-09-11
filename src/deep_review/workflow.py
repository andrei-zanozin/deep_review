from __future__ import annotations

import logging
from copy import deepcopy
from pathlib import Path
from typing import Literal

from deep_review.configuration import load_config, project_config_path
from deep_review.discovery import discover, find_targets
from deep_review.errors import WorkflowError
from deep_review.infrastructure import (
    AgentRunner,
    Commands,
    McpCommands,
    McpFactory,
    runtime_agent_runner,
)
from deep_review.models import (
    CandidateFinding,
    DiscoveryResult,
    PrReviewContext,
    PullRequestTarget,
    RepositoryIdentity,
    ReviewType,
    TicketReviewContext,
)
from deep_review.publication import finish_jira, publish, report_incomplete_jira
from deep_review.repository import discover_sibling_repositories, prepare_checkout
from deep_review.review import consolidate, validate_cross_prs, run_specialists
from deep_review.fix_verifier import plan_reconciliation

LOGGER = logging.getLogger(__name__)


def run_review(issue: str) -> TicketReviewContext:
    """Run a review for the repository containing the current directory."""
    config_path = project_config_path()
    config = load_config(config_path)
    servers = McpFactory(config.mcp, config_path.parent)
    agents = runtime_agent_runner(config, servers)
    with McpCommands(servers) as commands:
        return execute_review(issue, Path.cwd(), commands, agents)


def execute_review(
    issue_key: str,
    start: Path,
    commands: Commands,
    agents: AgentRunner,
) -> TicketReviewContext:
    _, issue, jira_comments, discovery = discover(issue_key, start, commands, agents)
    context = TicketReviewContext(
        issue_key=issue_key,
        issue=issue,
        jira_comments=jira_comments,
        reviewer=discovery.reviewer,
        requestor=discovery.requestor,
        review_type=discovery.review_type,
        pull_requests=find_targets(issue_key, discovery.reviewer.username, commands),
    )
    LOGGER.info("review targets discovered: %d", len(context.pull_requests))
    if not context.pull_requests:
        context.failures.append("no matching OPEN pull requests were found")

    repositories = discover_sibling_repositories(start)
    _prepare_pull_requests(context, discovery, repositories, commands, agents)
    _apply_cross_pr_validator(context, agents)
    published = _publish_pull_requests(context, discovery, commands, agents)

    if context.failures:
        context.status = "partial" if published else "failed"
        report_incomplete_jira(issue_key, published, context.failures, commands)
    else:
        context.status = "complete"
        finish_jira(
            issue_key,
            context.requestor.username,
            any(item.issues_found for item in context.pull_requests),
            commands,
        )
    LOGGER.info("deep review completed for %s with status %s", issue_key, context.status)
    return context


def _prepare_pull_requests(
    context: TicketReviewContext,
    discovery: DiscoveryResult,
    repositories: dict[tuple[str, str], RepositoryIdentity],
    commands: Commands,
    agents: AgentRunner,
) -> None:
    related = [_related_pull_request(item) for item in context.pull_requests]
    for pull_request in context.pull_requests:
        identity = (pull_request.key.project, pull_request.key.repository)
        repository = repositories.get(identity)
        if repository is None:
            _fail_pull_request(
                context,
                pull_request,
                "local sibling checkout is unavailable or ambiguous",
                "skipped",
            )
            continue
        pull_request.repository = repository
        try:
            prepare_checkout(repository.root, pull_request.target)
            pull_request.diff = _verified_diff(pull_request.target, commands)
            pull_request.status = "prepared"
            if pull_request.mode == "evidence_only":
                continue

            if context.review_type == ReviewType.FIX_VERIFIER:
                decisions, existing = plan_reconciliation(
                    context.issue,
                    context.jira_comments,
                    pull_request.diff,
                    discovery,
                    pull_request.target,
                    repository.root,
                    commands,
                    agents,
                )
                pull_request.fix_verifier_decisions = decisions
                pull_request.existing_reviewer_comments = existing

            results, candidates = run_specialists(
                context.issue,
                context.jira_comments,
                pull_request.diff,
                pull_request.target,
                repository.root,
                agents,
                related,
            )
            pull_request.specialist_results = results
            pull_request.candidates = candidates
            pull_request.status = "reviewed"
        except WorkflowError as exc:
            _fail_pull_request(context, pull_request, str(exc), "failed")


def _related_pull_request(context: PrReviewContext) -> dict[str, object]:
    return {
        "key": context.key.model_dump(mode="json"),
        "target": context.target.model_dump(mode="json"),
        "metadata": deepcopy(context.metadata),
        "mode": context.mode,
    }


def _apply_cross_pr_validator(
    context: TicketReviewContext,
    agents: AgentRunner,
) -> None:
    try:
        context.correlation = validate_cross_prs(context, agents)
        by_key = {
            (item.key.project, item.key.repository, item.key.id): item
            for item in context.pull_requests
        }
        for index, routed in enumerate(context.correlation.findings, start=1):
            target = by_key[
                (routed.target.project, routed.target.repository, routed.target.id)
            ]
            target.candidates.append(
                CandidateFinding(id=f"cross_pr_validator:{index}", finding=routed.finding)
            )
    except WorkflowError as exc:
        context.failures.append(f"ticket correlation failed: {exc}")


def _publish_pull_requests(
    context: TicketReviewContext,
    discovery: DiscoveryResult,
    commands: Commands,
    agents: AgentRunner,
) -> list[str]:
    for pull_request in context.pull_requests:
        if pull_request.status != "reviewed":
            continue
        try:
            if pull_request.repository is None:
                raise WorkflowError("reviewed pull request has no local repository")
            pull_request.findings = consolidate(
                pull_request.candidates,
                pull_request.existing_reviewer_comments,
                agents,
            )
            LOGGER.info(
                "new consolidated findings for PR %s: %d",
                _pull_request_label(pull_request),
                len(pull_request.findings),
            )
            issues_found, fix_verifier_status = publish(
                pull_request.target,
                pull_request.findings,
                None,
                pull_request.repository.root,
                commands,
                agents,
                pull_request.fix_verifier_decisions
                if context.review_type == ReviewType.FIX_VERIFIER
                else None,
                discovery,
            )
            pull_request.issues_found = issues_found
            pull_request.fix_verifier_status = fix_verifier_status
            pull_request.status = "published"
        except WorkflowError as exc:
            _fail_pull_request(context, pull_request, str(exc), "failed")
    return [
        _pull_request_label(item)
        for item in context.pull_requests
        if item.status == "published"
    ]


def _pull_request_label(context: PrReviewContext) -> str:
    return f"{context.key.project}/{context.key.repository}#{context.key.id}"


def _fail_pull_request(
    ticket: TicketReviewContext,
    pull_request: PrReviewContext,
    reason: str,
    status: Literal["skipped", "failed"],
) -> None:
    pull_request.status = status
    pull_request.failure_reason = reason
    ticket.failures.append(f"{_pull_request_label(pull_request)}: {reason}")


def _verified_diff(target: PullRequestTarget, commands: Commands) -> str:
    diff = commands.bitbucket("get_pull_request_diff", target.mcp_arguments())
    if not isinstance(diff, str) or not diff.strip():
        raise WorkflowError("pull-request diff is unavailable")
    if diff.startswith("[Warning: Bitbucket truncated this diff.]"):
        raise WorkflowError("Bitbucket returned a truncated pull-request diff")
    return diff
