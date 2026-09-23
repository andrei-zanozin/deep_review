from __future__ import annotations

import pytest

from deep_review.errors import WorkflowError
from deep_review.models import (
    CandidateFinding,
    ConsolidationResult,
    CrossPrValidationResult,
    Finding,
    JudgmentResult,
    Person,
    PrReviewContext,
    PullRequestTarget,
    ReviewType,
    Severity,
    Side,
    TicketReviewContext,
)
from deep_review.review import (
    _validate_consolidation,
    judge_findings,
    render_finding,
    validate_cross_prs,
)


def finding(severity: str = "Minor") -> Finding:
    return Finding(
        severity=Severity(severity),
        title="Clear title",
        path="src/code.py",
        line=8,
        side=Side.DESTINATION,
        problem_and_impact="The behavior is incorrect and callers fail.",
        suggested_fix="Return the correct value.",
        evidence="The changed branch always returns false.",
    )


def test_consolidation_preserves_selected_content_and_highest_severity() -> None:
    candidates = [
        CandidateFinding(id="implementation_expert:1", finding=finding("Major")),
        CandidateFinding(id="architecture_expert:1", finding=finding("Minor")),
    ]
    result = ConsolidationResult.model_validate(
        {
            "selections": [
                {
                    "selected_id": "architecture_expert:1",
                    "duplicate_ids": ["implementation_expert:1"],
                    "severity": "Major",
                }
            ]
        }
    )
    consolidated = _validate_consolidation(candidates, result)
    assert consolidated[0].id == "architecture_expert:1"
    assert consolidated[0].finding.severity == "Major"
    assert consolidated[0].finding.title == "Clear title"


def test_consolidation_derives_highest_severity_when_agent_downgrades_group() -> None:
    candidates = [
        CandidateFinding(id="implementation_expert:1", finding=finding("Major")),
        CandidateFinding(id="architecture_expert:1", finding=finding("Minor")),
    ]
    result = ConsolidationResult.model_validate(
        {
            "selections": [
                {
                    "selected_id": "architecture_expert:1",
                    "duplicate_ids": ["implementation_expert:1"],
                    "severity": "Minor",
                }
            ]
        }
    )

    consolidated = _validate_consolidation(candidates, result)

    assert consolidated[0].finding.severity == "Major"


def test_consolidation_requires_every_candidate() -> None:
    candidates = [CandidateFinding(id="implementation_expert:1", finding=finding())]
    with pytest.raises(WorkflowError, match="every candidate"):
        _validate_consolidation(candidates, ConsolidationResult(selections=[]))


def test_anchored_render_removes_only_location_line() -> None:
    rendered = render_finding(finding(), anchored=True)
    assert rendered.startswith("### Minor: Clear title")
    assert "Location:" not in rendered
    assert "Problem and impact:" in rendered
    assert "Suggested fix:" in rendered
    assert "Evidence:" in rendered


def test_judgment_transfers_context_and_preserves_retained_findings(tmp_path) -> None:
    target = PullRequestTarget(
        id=1,
        project="PRJ",
        repository="service",
        source_branch="feature",
        target_branch="main",
        reviewed_head="a" * 40,
        reviewed_base="b" * 40,
    )
    pr = PrReviewContext(
        key=target.key,
        target=target,
        metadata={"description": "Fix it"},
        diff="reviewed diff",
        status="reviewed",
    )
    context = TicketReviewContext(
        issue_key="ABC-123",
        issue={"key": "ABC-123"},
        jira_comments=[{"body": "Needed"}],
        reviewer=Person(username="reviewer"),
        requestor=Person(username="requestor"),
        review_type=ReviewType.PRIMARY,
        pull_requests=[pr],
    )
    candidates = [
        CandidateFinding(id="one", finding=finding("Minor")),
        CandidateFinding(id="two", finding=finding("Major")),
    ]

    class Agent:
        def judgment(self, payload, repository_root):
            assert repository_root == tmp_path
            assert payload["issue"] == {"key": "ABC-123"}
            assert payload["jira_comments"] == [{"body": "Needed"}]
            assert payload["pull_request"]["diff"] == "reviewed diff"
            assert payload["pull_request"]["metadata"]["description"] == "Fix it"
            assert [item["id"] for item in payload["candidates"]] == ["one", "two"]
            return JudgmentResult.model_validate(
                {
                    "decisions": [
                        {"candidate_id": "two", "action": "discard", "reason": "Low value."},
                        {"candidate_id": "one", "action": "keep", "reason": "Material."},
                    ]
                }
            )

    kept, result = judge_findings(context, pr, candidates, tmp_path, Agent())
    assert kept == candidates[:1]
    assert result is not None


@pytest.mark.parametrize("ids", [[], ["one", "one"], ["wrong"]])
def test_judgment_requires_one_decision_per_candidate(tmp_path, ids) -> None:
    target = PullRequestTarget(
        id=1,
        project="PRJ",
        repository="service",
        source_branch="feature",
        target_branch="main",
        reviewed_head="a" * 40,
        reviewed_base="b" * 40,
    )
    pr = PrReviewContext(key=target.key, target=target, status="reviewed")
    context = TicketReviewContext(
        issue_key="ABC-123",
        issue={},
        reviewer=Person(username="reviewer"),
        requestor=Person(username="requestor"),
        review_type=ReviewType.PRIMARY,
        pull_requests=[pr],
    )

    class Agent:
        def judgment(self, payload, repository_root):
            return JudgmentResult.model_validate(
                {
                    "decisions": [
                        {"candidate_id": item, "action": "keep", "reason": "Material."}
                        for item in ids
                    ]
                }
            )

    with pytest.raises(WorkflowError, match="every candidate exactly once"):
        judge_findings(
            context, pr, [CandidateFinding(id="one", finding=finding())], tmp_path, Agent()
        )


def test_cross_pr_validator_rejects_unknown_target() -> None:
    def pull_request(pr_id: int) -> PrReviewContext:
        target = PullRequestTarget(
            id=pr_id,
            project="PRJ",
            repository="service",
            source_branch=f"feature/{pr_id}",
            target_branch="main",
            reviewed_head=f"{pr_id}" * 40,
            reviewed_base=f"{pr_id}" * 40,
        )
        return PrReviewContext(key=target.key, target=target, status="reviewed")

    context = TicketReviewContext(
        issue_key="ABC-123",
        issue={"key": "ABC-123"},
        reviewer=Person(username="reviewer"),
        requestor=Person(username="requestor"),
        review_type=ReviewType.PRIMARY,
        pull_requests=[pull_request(1), pull_request(2)],
    )

    class Agent:
        def cross_pr_validator(self, _: dict[str, object]) -> CrossPrValidationResult:
            return CrossPrValidationResult.model_validate(
                {
                    "findings": [
                        {
                            "target": {"project": "PRJ", "repository": "service", "id": 99},
                            "finding": finding(),
                        }
                    ]
                }
            )

    with pytest.raises(WorkflowError, match="unknown pull request"):
        validate_cross_prs(context, Agent())  # type: ignore[arg-type]
