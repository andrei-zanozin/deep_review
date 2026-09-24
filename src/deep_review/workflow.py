from __future__ import annotations

import logging
from copy import deepcopy
from pathlib import Path
from typing import Literal

from deep_review.configuration import load_config, project_config_path
from deep_review.discovery import discover, find_targets
from deep_review.errors import WorkflowError
from deep_review.fix_verifier import plan_reconciliation
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
    _repository_identity_key,
    same_username,
)
from deep_review.publication import finish_jira, publish, report_incomplete_jira
from deep_review.repository import (
    discover_sibling_repositories,
    prepare_checkout,
    pull_request_diff,
    require_current_target,
)
from deep_review.review import consolidate, judge_findings, run_specialists, validate_cross_prs
from deep_review.usage import UsageCollector

LOGGER = logging.getLogger(__name__)


def run_review(issue: str, *, verbose: bool = False) -> TicketReviewContext:
    """Run a review for the repository containing the current directory."""
    config_path = project_config_path()
    config = load_config(config_path)
    servers = McpFactory(config.mcp, config_path.parent, verbose=verbose)
    usage = UsageCollector()
    agents = runtime_agent_runner(config, servers, usage)
    try:
        with McpCommands(servers) as commands:
            return execute_review(issue, Path.cwd(), commands, agents)
    finally:
        usage.log_summary()


def execute_review(
    issue_key: str,
    start: Path,
    commands: Commands,
    agents: AgentRunner,
) -> TicketReviewContext:
    context, discovery = _initialize_review(issue_key, start, commands, agents)
    repositories = discover_sibling_repositories(start)

    if not _preflight_pull_requests(context, repositories, commands):
        _finish_review(context, [], commands)
        return context
    _review_pull_requests(context, discovery, repositories, commands, agents)
    _apply_cross_pr_validator(context, agents)
    published_pull_requests = _publish_pull_requests_findings(
        context,
        discovery,
        commands,
        agents,
    )

    _finish_review(context, published_pull_requests, commands)
    return context


def _initialize_review(
    issue_key: str,
    start: Path,
    commands: Commands,
    agents: AgentRunner,
) -> tuple[TicketReviewContext, DiscoveryResult]:
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
    return context, discovery


def _finish_review(
    context: TicketReviewContext,
    published_pull_requests: list[str],
    commands: Commands,
) -> None:
    if context.failures:
        context.status = "partial" if published_pull_requests else "failed"
        report_incomplete_jira(
            context.issue_key,
            published_pull_requests,
            context.failures,
            commands,
        )
    else:
        context.status = "complete"
        finish_jira(
            context.issue_key,
            context.requestor.username,
            any(item.issues_found for item in context.pull_requests),
            commands,
        )
    LOGGER.info(
        "deep review completed for %s with status %s",
        context.issue_key,
        context.status,
    )


def _review_pull_requests(
    context: TicketReviewContext,
    discovery: DiscoveryResult,
    repositories: dict[tuple[str, str], RepositoryIdentity],
    commands: Commands,
    agents: AgentRunner,
) -> None:
    related = [_related_pull_request(item) for item in context.pull_requests]
    for pull_request in context.pull_requests:
        if pull_request.status != "prepared":
            continue
        repository = pull_request.repository
        if repository is None:
            raise WorkflowError("prepared pull request has no local repository")
        try:
            _review_pull_request(
                context,
                discovery,
                pull_request,
                repository,
                commands,
                agents,
                related,
            )
        except WorkflowError as exc:
            _fail_pull_request(context, pull_request, str(exc), "failed")


def _preflight_pull_requests(
    context: TicketReviewContext,
    repositories: dict[tuple[str, str], RepositoryIdentity],
    commands: Commands,
) -> bool:
    passed = True
    for pull_request in context.pull_requests:
        identity = _repository_identity_key(pull_request.key.project, pull_request.key.repository)
        repository = repositories.get(identity)
        if repository is None:
            _skip_pull_request_without_repository(context, pull_request, repositories)
            continue
        try:
            current = commands.bitbucket("get_pull_request", pull_request.target.mcp_arguments())
            if (
                not isinstance(current, dict)
                or _commit(current, "source") != pull_request.target.reviewed_head
                or _commit(current, "target") != pull_request.target.reviewed_base
            ):
                raise WorkflowError("pull-request head or base changed before review")
            prepare_checkout(repository.root, pull_request.target)
            require_current_target(repository.root, pull_request.target)
            pull_request.repository = repository
            pull_request.diff = pull_request_diff(repository.root, pull_request.target)
            pull_request.status = "prepared"
        except WorkflowError as exc:
            _fail_pull_request(context, pull_request, str(exc), "failed")
            passed = False
    return passed


def _commit(pull_request: dict[str, object], ref: str) -> str | None:
    value = pull_request.get(ref)
    return value.get("commit") if isinstance(value, dict) else None


def _skip_pull_request_without_repository(
    context: TicketReviewContext,
    pull_request: PrReviewContext,
    repositories: dict[tuple[str, str], RepositoryIdentity],
) -> None:
    available = ", ".join(
        f"{candidate.project}/{candidate.repository} at {candidate.root}"
        for candidate in sorted(
            repositories.values(),
            key=lambda item: (
                *_repository_identity_key(item.project, item.repository),
                item.root,
            ),
        )
    )
    LOGGER.warning(
        "local checkout lookup failed for PR %s: expected identity=%s/%s; "
        "discovered repositories=%s; see repository discovery logs for skipped or ambiguous "
        "candidates",
        _pull_request_label(pull_request),
        pull_request.key.project,
        pull_request.key.repository,
        available or "none",
    )
    _fail_pull_request(
        context,
        pull_request,
        "local sibling checkout is unavailable or ambiguous",
        "skipped",
    )


def _review_pull_request(
    context: TicketReviewContext,
    discovery: DiscoveryResult,
    pull_request: PrReviewContext,
    repository: RepositoryIdentity,
    commands: Commands,
    agents: AgentRunner,
    related_pull_requests: list[dict[str, object]],
) -> None:
    prepare_checkout(repository.root, pull_request.target)
    if pull_request.mode == "evidence_only":
        return

    if context.review_type == ReviewType.FIX_VERIFIER:
        # Fail on a changed PR before verifier agents spend time on the stale checkout.
        skip_specialists = _should_skip_fix_verifier_specialists(
            commands,
            pull_request.target,
            discovery.reviewer.username,
        )
        decisions, existing_comments = plan_reconciliation(
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
        pull_request.existing_reviewer_comments = existing_comments

        if skip_specialists:
            LOGGER.info(
                "Skipping specialist review for %s: reviewer already reviewed source commit %s; "
                "comment reconciliation will continue",
                _pull_request_label(pull_request),
                pull_request.target.reviewed_head,
            )
            pull_request.status = "reviewed"
            return
        LOGGER.info(
            "Running specialist review for %s: no confirmed prior review of the source commit",
            _pull_request_label(pull_request),
        )

    results, candidates = run_specialists(
        context.issue,
        context.jira_comments,
        pull_request.diff,
        pull_request.target,
        repository.root,
        agents,
        related_pull_requests,
    )
    pull_request.specialist_results = results
    pull_request.candidates = candidates
    pull_request.status = "reviewed"


def _should_skip_fix_verifier_specialists(
    commands: Commands,
    target: PullRequestTarget,
    reviewer: str,
) -> bool:
    current_pr = commands.bitbucket("get_pull_request", target.mcp_arguments())
    if not isinstance(current_pr, dict):
        raise WorkflowError("pull request metadata is not an object")
    source = current_pr.get("source")
    target_ref = current_pr.get("target")
    if (
        not isinstance(source, dict)
        or source.get("commit") != target.reviewed_head
        or not isinstance(target_ref, dict)
        or target_ref.get("commit") != target.reviewed_base
    ):
        raise WorkflowError("pull-request head or base changed before fix verification")
    return _reviewer_has_reviewed_current_commit(
        current_pr,
        reviewer,
        target.reviewed_head,
    )


def _reviewer_has_reviewed_current_commit(
    pull_request: dict[str, object], reviewer: str, source_commit: str
) -> bool:
    reviewers = pull_request.get("reviewers")
    if not isinstance(reviewers, list):
        return False
    matches = []
    for item in reviewers:
        if not isinstance(item, dict):
            continue
        user = item.get("user")
        slug = user.get("slug") if isinstance(user, dict) else None
        if same_username(slug, reviewer):
            matches.append(item)
    if len(matches) != 1:
        return False
    reviewed_commit = matches[0].get("last_reviewed_commit")
    return isinstance(reviewed_commit, str) and (
        reviewed_commit.casefold() == source_commit.casefold()
    )


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
            target = by_key[(routed.target.project, routed.target.repository, routed.target.id)]
            target.candidates.append(
                CandidateFinding(id=f"cross_pr_validator:{index}", finding=routed.finding)
            )
    except WorkflowError as exc:
        context.failures.append(f"ticket correlation failed: {exc}")


def _publish_pull_requests_findings(
    context: TicketReviewContext,
    discovery: DiscoveryResult,
    commands: Commands,
    agents: AgentRunner,
) -> list[str]:
    for pull_request in context.pull_requests:
        if pull_request.status != "reviewed":
            continue
        try:
            _publish_pull_request_findings(
                context,
                discovery,
                pull_request,
                commands,
                agents,
            )
        except WorkflowError as exc:
            _fail_pull_request(context, pull_request, str(exc), "failed")
    return [
        _pull_request_label(item) for item in context.pull_requests if item.status == "published"
    ]


def _publish_pull_request_findings(
    context: TicketReviewContext,
    discovery: DiscoveryResult,
    pull_request: PrReviewContext,
    commands: Commands,
    agents: AgentRunner,
) -> None:
    if pull_request.repository is None:
        raise WorkflowError("reviewed pull request has no local repository")

    consolidated = consolidate(
        pull_request.candidates,
        pull_request.existing_reviewer_comments,
        agents,
    )
    if consolidated:
        prepare_checkout(pull_request.repository.root, pull_request.target)
    pull_request.findings, pull_request.judgment_result = judge_findings(
        context,
        pull_request,
        consolidated,
        pull_request.repository.root,
        agents,
    )
    issues_found, fix_verifier_status = publish(
        pull_request.target,
        pull_request.findings,
        None,
        pull_request.diff or "No changes.",
        pull_request.repository.root,
        commands,
        fix_verifier_decisions=(
            pull_request.fix_verifier_decisions
            if context.review_type == ReviewType.FIX_VERIFIER
            else None
        ),
        discovery=discovery,
    )
    pull_request.issues_found = issues_found
    pull_request.fix_verifier_status = fix_verifier_status
    pull_request.status = "published"


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
