from __future__ import annotations

from pathlib import Path
from typing import Any

from deep_review.errors import WorkflowError
from deep_review.infrastructure import AgentRunner, Commands
from deep_review.models import (
    AgentRole,
    CandidateFinding,
    DiscoveryResult,
    LocationVerification,
    PullRequestTarget,
    SecondaryDecision,
)
from deep_review.repository import location_in_diff
from deep_review.review import render_finding
from deep_review.secondary import SecondaryStatus, apply_reconciliation


def publish(
    target: PullRequestTarget,
    findings: list[CandidateFinding],
    secondary_status: SecondaryStatus | None,
    repository_root: Path,
    commands: Commands,
    agents: AgentRunner,
    secondary_decisions: list[SecondaryDecision] | None = None,
    discovery: DiscoveryResult | None = None,
) -> tuple[bool, SecondaryStatus | None]:
    diff = _preflight(target, findings, repository_root, commands, agents)
    if secondary_decisions is not None:
        if discovery is None:
            raise WorkflowError("secondary publication requires discovery context")
        secondary_status = apply_reconciliation(
            target, secondary_decisions, discovery, commands
        )
    if findings:
        if not diff:
            raise WorkflowError("pull-request diff is empty before publication")
        for candidate in findings:
            finding = candidate.finding
            commands.bitbucket(
                "add_pull_request_comment",
                {
                    **target.mcp_arguments(),
                    "text": render_finding(finding, anchored=True),
                    "anchor": {
                        "path": finding.path,
                        "line": finding.line,
                        "side": finding.side.value,
                    },
                },
            )

    needs_work = bool(findings) or secondary_status == "Done"
    commands.bitbucket(
        "set_review_status",
        {
            **target.mcp_arguments(),
            "status": "NEEDS_WORK" if needs_work else "APPROVED",
        },
    )
    return needs_work, secondary_status


def finish_jira(issue_key: str, requestor: str, issues_found: bool, commands: Commands) -> None:
    message = (
        f"Hi [~{requestor}], please check my findings in the PR(s) comments."
        if issues_found
        else f"Hi [~{requestor}], review is done ✅"
    )
    commands.jira("add_comment", {"issue": issue_key, "body": message})
    commands.jira("assign_issue", {"issue": issue_key, "username": requestor})


def report_incomplete_jira(
    issue_key: str,
    reviewed: list[str],
    failures: list[str],
    commands: Commands,
) -> None:
    reviewed_text = ", ".join(reviewed) if reviewed else "none"
    detail = "; ".join(failures)
    commands.jira(
        "add_comment",
        {
            "issue": issue_key,
            "body": (
                "Deep review is incomplete. "
                f"Reviewed PRs: {reviewed_text}. "
                f"Skipped or failed: {detail}"
            ),
        },
    )


def _preflight(
    target: PullRequestTarget,
    findings: list[CandidateFinding],
    repository_root: Path,
    commands: Commands,
    agents: AgentRunner,
) -> str:
    current = commands.bitbucket("get_pull_request", target.mcp_arguments())
    if not isinstance(current, dict) or _head(current) != target.reviewed_head:
        raise WorkflowError("pull-request head changed after review")
    diff = commands.bitbucket("get_pull_request_diff", target.mcp_arguments())
    if not isinstance(diff, str) or not diff.strip():
        raise WorkflowError("pull-request diff is unavailable")
    if diff.startswith("[Warning: Bitbucket truncated this diff.]"):
        raise WorkflowError("Bitbucket returned a truncated pull-request diff")
    invalid = [
        candidate.id
        for candidate in findings
        if not location_in_diff(diff, candidate.finding)
    ]
    if invalid:
        raise WorkflowError(f"findings have invalid diff anchors: {', '.join(invalid)}")

    if not findings:
        return diff

    verification = agents.run(
        AgentRole.LOCATION_VERIFIER,
        {
            "pull_request": target.model_dump(mode="json"),
            "diff": diff,
            "findings": [candidate.model_dump(mode="json") for candidate in findings],
        },
        LocationVerification,
        repository_root,
    )
    by_id = {decision.finding_id: decision for decision in verification.decisions}
    expected = {candidate.id for candidate in findings}
    if by_id.keys() != expected or len(by_id) != len(verification.decisions):
        raise WorkflowError("location verification did not cover every finding exactly once")
    failures = [
        f"{finding_id}: {by_id[finding_id].reason}"
        for finding_id in expected
        if not by_id[finding_id].valid
    ]
    if failures:
        raise WorkflowError(f"location verification failed: {'; '.join(sorted(failures))}")
    return diff


def _head(pull_request: dict[str, Any]) -> str | None:
    source = pull_request.get("source")
    return source.get("commit") if isinstance(source, dict) else None
