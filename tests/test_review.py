from __future__ import annotations

import pytest

from deep_review.errors import WorkflowError
from deep_review.models import (
    CandidateFinding,
    ConsolidationResult,
    Finding,
    PrReviewContext,
    PullRequestTarget,
    TicketCorrelationResult,
    TicketReviewContext,
)
from deep_review.review import _validate_consolidation, correlate_ticket, render_finding


def finding(severity: str = "Minor") -> Finding:
    return Finding(
        severity=severity,
        title="Clear title",
        path="src/code.py",
        line=8,
        side="destination",
        problem_and_impact="The behavior is incorrect and callers fail.",
        suggested_fix="Return the correct value.",
        evidence="The changed branch always returns false.",
    )


def test_consolidation_preserves_selected_content_and_highest_severity() -> None:
    candidates = [
        CandidateFinding(id="unit:1", finding=finding("Major")),
        CandidateFinding(id="architecture:1", finding=finding("Minor")),
    ]
    result = ConsolidationResult(
        selections=[
            {
                "selected_id": "architecture:1",
                "duplicate_ids": ["unit:1"],
                "severity": "Major",
            }
        ]
    )
    consolidated = _validate_consolidation(candidates, result)
    assert consolidated[0].id == "architecture:1"
    assert consolidated[0].finding.severity == "Major"
    assert consolidated[0].finding.title == "Clear title"


def test_consolidation_requires_every_candidate() -> None:
    candidates = [CandidateFinding(id="unit:1", finding=finding())]
    with pytest.raises(WorkflowError, match="every candidate"):
        _validate_consolidation(candidates, ConsolidationResult(selections=[]))


def test_anchored_render_removes_only_location_line() -> None:
    rendered = render_finding(finding(), anchored=True)
    assert rendered.startswith("### Minor: Clear title")
    assert "Location:" not in rendered
    assert "Problem and impact:" in rendered
    assert "Suggested fix:" in rendered
    assert "Evidence:" in rendered


def test_ticket_correlation_rejects_unknown_target() -> None:
    def pull_request(pr_id: int) -> PrReviewContext:
        target = PullRequestTarget(
            id=pr_id,
            project="PRJ",
            repository="service",
            source_branch=f"feature/{pr_id}",
            target_branch="main",
            reviewed_head=f"{pr_id}" * 40,
        )
        return PrReviewContext(key=target.key, target=target, status="reviewed")

    context = TicketReviewContext(
        issue_key="ABC-123",
        issue={"key": "ABC-123"},
        reviewer={"username": "reviewer"},
        requestor={"username": "requestor"},
        review_type="primary",
        pull_requests=[pull_request(1), pull_request(2)],
    )

    class Agent:
        def run(self, *_: object, **__: object) -> TicketCorrelationResult:
            return TicketCorrelationResult(
                findings=[
                    {
                        "target": {"project": "PRJ", "repository": "service", "id": 99},
                        "finding": finding(),
                    }
                ]
            )

    with pytest.raises(WorkflowError, match="unknown pull request"):
        correlate_ticket(context, Agent())  # type: ignore[arg-type]
