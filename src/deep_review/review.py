from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any

from deep_review.errors import WorkflowError
from deep_review.infrastructure import AgentRunner
from deep_review.models import (
    AgentRole,
    CandidateFinding,
    ConsolidationResult,
    CrossPrValidationResult,
    Finding,
    PullRequestTarget,
    ReviewResult,
    Severity,
    TicketReviewContext,
)

REVIEW_ROLES = (
    AgentRole.ARCHITECTURE_EXPERT,
    AgentRole.IMPLEMENTATION_EXPERT,
    AgentRole.CODE_POLISH_EXPERT,
)
SEVERITY_RANK = {Severity.MINOR: 0, Severity.MAJOR: 1, Severity.CRITICAL: 2}

LOGGER = logging.getLogger(__name__)


def run_specialists(
    issue: dict[str, Any],
    jira_comments: list[dict[str, Any]],
    diff: str,
    target: PullRequestTarget,
    repository_root: Path,
    agents: AgentRunner,
    related_pull_requests: list[dict[str, Any]] | None = None,
) -> tuple[dict[AgentRole, ReviewResult], list[CandidateFinding]]:
    payload = {
        "issue": deepcopy(issue),
        "jira_comments": deepcopy(jira_comments),
        "diff": diff,
        "pull_request": target.model_dump(mode="json"),
        "repository_root": str(repository_root),
        "related_pull_requests": related_pull_requests or [],
    }
    with ThreadPoolExecutor(max_workers=len(REVIEW_ROLES)) as executor:
        futures = {
            AgentRole.ARCHITECTURE_EXPERT: executor.submit(
                agents.architecture_expert, deepcopy(payload), repository_root
            ),
            AgentRole.IMPLEMENTATION_EXPERT: executor.submit(
                agents.implementation_expert, deepcopy(payload), repository_root
            ),
            AgentRole.CODE_POLISH_EXPERT: executor.submit(
                agents.code_polish_expert, deepcopy(payload), repository_root
            ),
        }
    results: dict[AgentRole, ReviewResult] = {}
    candidates: list[CandidateFinding] = []
    for role, future in futures.items():
        result = future.result()
        if result.status == "failed":
            raise WorkflowError(f"{role.value} review failed: {result.failure_reason}")
        results[role] = result
        candidates.extend(
            CandidateFinding(id=f"{role.value}:{index}", finding=finding)
            for index, finding in enumerate(result.findings, start=1)
        )
    return results, candidates


def validate_cross_prs(
    context: TicketReviewContext,
    agents: AgentRunner,
) -> CrossPrValidationResult:
    available = [
        pull_request
        for pull_request in context.pull_requests
        if pull_request.status in {"prepared", "reviewed"}
    ]
    if len(available) < 2:
        return CrossPrValidationResult()
    result = agents.cross_pr_validator(
        {
            "issue": deepcopy(context.issue),
            "jira_comments": deepcopy(context.jira_comments),
            "reviewer": context.reviewer.model_dump(mode="json"),
            "requestor": context.requestor.model_dump(mode="json"),
            "review_type": context.review_type.value,
            "pull_requests": [
                {
                    "key": item.key.model_dump(mode="json"),
                    "target": item.target.model_dump(mode="json"),
                    "metadata": deepcopy(item.metadata),
                    "diff": item.diff,
                    "mode": item.mode,
                    "specialist_results": {
                        role.value: output.model_dump(mode="json")
                        for role, output in item.specialist_results.items()
                    },
                }
                for item in available
            ],
        }
    )
    known = {
        (item.key.project, item.key.repository, item.key.id): item for item in available
    }
    for routed in result.findings:
        target_key = (routed.target.project, routed.target.repository, routed.target.id)
        if target_key not in known:
            raise WorkflowError("ticket correlation targets an unknown pull request")
        if known[target_key].mode != "review":
            raise WorkflowError("ticket correlation targets an evidence-only pull request")
        for related in routed.related_pull_requests:
            key = (related.project, related.repository, related.id)
            if key not in known:
                raise WorkflowError("ticket correlation references an unknown pull request")
    return result


def consolidate(
    candidates: list[CandidateFinding],
    existing_comments: list[dict[str, Any]],
    agents: AgentRunner,
) -> list[CandidateFinding]:
    if not candidates:
        LOGGER.info("consolidator: accepted issues: 0, rejected issues: 0 []")
        return []
    result = agents.consolidator(
        {
            "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
            "existing_reviewer_comments": existing_comments,
        }
    )
    consolidated = _validate_consolidation(candidates, result, bool(existing_comments))
    LOGGER.info(
        "consolidator: accepted issues: %d, rejected issues: %d %s",
        len(consolidated),
        len(candidates) - len(consolidated),
        _severity_summary(consolidated),
    )
    return consolidated


def _severity_summary(findings: list[CandidateFinding]) -> str:
    counts = {severity: 0 for severity in Severity}
    for candidate in findings:
        counts[candidate.finding.severity] += 1
    groups = [f"{severity.value}: {counts[severity]}" for severity in Severity if counts[severity]]
    return f"[{' | '.join(groups)}]" if groups else "[]"


def _validate_consolidation(
    candidates: list[CandidateFinding],
    result: ConsolidationResult,
    existing_comments_present: bool = False,
) -> list[CandidateFinding]:
    by_id = {candidate.id: candidate for candidate in candidates}
    used: set[str] = set()
    consolidated: list[CandidateFinding] = []
    for selection in result.selections:
        group = [selection.selected_id, *selection.duplicate_ids]
        invalid_group = len(group) != len(set(group)) or any(
            candidate_id not in by_id for candidate_id in group
        )
        if invalid_group:
            raise WorkflowError("consolidation references an invalid candidate group")
        if used.intersection(group):
            raise WorkflowError("consolidation references a candidate more than once")
        used.update(group)
        expected = max(
            (by_id[candidate_id].finding.severity for candidate_id in group),
            key=SEVERITY_RANK.__getitem__,
        )
        selected = by_id[selection.selected_id]
        consolidated.append(
            selected.model_copy(
                update={"finding": selected.finding.model_copy(update={"severity": expected})}
            )
        )

    excluded = set(result.existing_comment_duplicates)
    if excluded and not existing_comments_present:
        raise WorkflowError("consolidation excluded findings without existing comments")
    if unknown := excluded - by_id.keys():
        raise WorkflowError(f"consolidation excluded unknown candidates: {sorted(unknown)}")
    if used.intersection(excluded) or used | excluded != by_id.keys():
        raise WorkflowError("consolidation did not account for every candidate exactly once")
    return consolidated


def render_finding(finding: Finding, anchored: bool = False) -> str:
    lines = [
        f"### {finding.severity.value}: {finding.title}",
        "",
        f"Location: {finding.path}:{finding.line} ({finding.side.value})",
        "",
        f"Problem and impact: {finding.problem_and_impact}",
        "",
        f"Suggested fix: {finding.suggested_fix}",
        "",
        f"Evidence: {finding.evidence}",
    ]
    if anchored:
        del lines[2:4]
    return "\n".join(lines)
