from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from deep_review.errors import WorkflowError
from deep_review.fix_verifier import FixVerifierStatus, apply_reconciliation
from deep_review.infrastructure import Commands
from deep_review.models import (
    CandidateFinding,
    DiscoveryResult,
    FixVerifierDecision,
    PullRequestTarget,
)
from deep_review.repository import (
    check_finding_location,
    pull_request_diff,
)
from deep_review.review import render_finding

LOGGER = logging.getLogger(__name__)


def publish(
    target: PullRequestTarget,
    findings: list[CandidateFinding],
    fix_verifier_status: FixVerifierStatus | None,
    diff: str,
    repository_root: Path,
    commands: Commands,
    fix_verifier_decisions: list[FixVerifierDecision] | None = None,
    discovery: DiscoveryResult | None = None,
) -> tuple[bool, FixVerifierStatus | None]:
    verified = _preflight(target, findings, repository_root, commands)
    if fix_verifier_decisions is not None:
        if discovery is None:
            raise WorkflowError("fix_verifier publication requires discovery context")
        fix_verifier_status = apply_reconciliation(
            target, fix_verifier_decisions, discovery, commands
        )
    if verified:
        if not diff:
            raise WorkflowError("pull-request diff is empty before publication")
        for candidate, is_inline in verified:
            finding = candidate.finding
            arguments: dict[str, Any] = {
                **target.mcp_arguments(),
                "text": render_finding(finding, anchored=is_inline),
            }
            if is_inline:
                arguments["anchor"] = {
                    "path": finding.path,
                    "line": finding.line,
                    "side": finding.side.value,
                }
            commands.bitbucket(
                "add_pull_request_comment",
                arguments,
            )
    LOGGER.info("posted issues: %d", len(verified))

    needs_work = bool(verified) or fix_verifier_status == "Done"
    commands.bitbucket(
        "set_review_status",
        {
            **target.mcp_arguments(),
            "status": "NEEDS_WORK" if needs_work else "APPROVED",
        },
    )
    return needs_work, fix_verifier_status


def finish_jira(issue_key: str, requestor: str, issues_found: bool, commands: Commands) -> None:
    message = (
        f"Hi [~{requestor}], please check my findings in the PR(s) comments."
        if issues_found
        else f"Hi [~{requestor}], review is done ✅"
    )
    commands.jira("add_comment", {"issue": issue_key, "body": message})
    commands.jira("assign_issue", {"issue": issue_key, "username": requestor})


def _preflight(
    target: PullRequestTarget,
    findings: list[CandidateFinding],
    repository_root: Path,
    commands: Commands,
) -> list[tuple[CandidateFinding, bool]]:
    current = commands.bitbucket("get_pull_request", target.mcp_arguments())
    if (
        not isinstance(current, dict)
        or _commit(current, "source") != target.reviewed_head
        or _commit(current, "target") != target.reviewed_base
    ):
        raise WorkflowError("pull-request head or base changed after review")
    verified = []
    changed_diff = pull_request_diff(repository_root, target, unified=0)
    for candidate in findings:
        finding = candidate.finding
        check = check_finding_location(
            repository_root,
            target,
            changed_diff,
            finding.path,
            finding.line,
            finding.side.value,
        )
        if not check["valid"] or (
            candidate.id.startswith("cross_pr_validator:") and not check["inline"]
        ):
            reason = (
                "line is outside the target PR diff" if check["valid"] else check["reason"]
            )
            LOGGER.warning(
                "omitted finding before publication: PR=%s/%s#%d, candidate=%s, "
                "location=%s:%d (%s), reason=%s",
                target.project,
                target.repository,
                target.id,
                candidate.id,
                finding.path,
                finding.line,
                finding.side.value,
                reason,
            )
            continue
        verified.append((candidate, bool(check["inline"])))
    return verified


def _commit(pull_request: dict[str, Any], ref: str) -> str | None:
    value = pull_request.get(ref)
    return value.get("commit") if isinstance(value, dict) else None
