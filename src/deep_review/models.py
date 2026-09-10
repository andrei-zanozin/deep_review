from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AgentRole(StrEnum):
    DISCOVERY = "discovery"
    SECONDARY = "secondary"
    ARCHITECTURE = "architecture"
    UNIT = "unit"
    CODE_POLISH = "code_polish"
    CONSOLIDATION = "consolidation"
    LOCATION_VERIFIER = "location_verifier"
    TICKET_CORRELATION = "ticket_correlation"


class Person(StrictModel):
    username: str = Field(min_length=1)
    display_name: str | None = None


class ReviewType(StrEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"


class DiscoveryResult(StrictModel):
    reviewer: Person
    requestor: Person
    review_type: ReviewType


class RepositoryIdentity(StrictModel):
    root: Path
    project: str = Field(min_length=1)
    repository: str = Field(min_length=1)


class PullRequestTarget(StrictModel):
    id: Annotated[int, Field(gt=0)]
    project: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    source_branch: str = Field(min_length=1)
    target_branch: str = Field(min_length=1)
    reviewed_head: str = Field(pattern=r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")

    def mcp_arguments(self) -> dict[str, int | str]:
        return {"project": self.project, "repo": self.repository, "pr_id": self.id}

    @property
    def key(self) -> PullRequestKey:
        return PullRequestKey(project=self.project, repository=self.repository, id=self.id)


class PullRequestKey(StrictModel):
    project: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    id: Annotated[int, Field(gt=0)]


class Severity(StrEnum):
    CRITICAL = "Critical"
    MAJOR = "Major"
    MINOR = "Minor"


class Side(StrEnum):
    SOURCE = "source"
    DESTINATION = "destination"


class Finding(StrictModel):
    severity: Severity
    title: str = Field(min_length=1)
    path: str = Field(min_length=1)
    line: Annotated[int, Field(gt=0)]
    side: Side
    problem_and_impact: str = Field(min_length=1)
    suggested_fix: str = Field(min_length=1)
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_path(self) -> Finding:
        candidate = Path(self.path)
        if candidate.is_absolute() or not candidate.parts or any(
            part in {"", ".", ".."} for part in candidate.parts
        ):
            raise ValueError("finding path must be normalized and repository-relative")
        return self


class ReviewResult(StrictModel):
    status: Literal["no_issues", "findings", "failed"]
    findings: list[Finding] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    failure_reason: str | None = None

    @model_validator(mode="after")
    def validate_status(self) -> ReviewResult:
        if self.status != "failed" and not self.coverage:
            raise ValueError("a successful review requires coverage")
        if self.status == "findings" and not self.findings:
            raise ValueError("findings status requires at least one finding")
        if self.status != "findings" and self.findings:
            raise ValueError("findings are allowed only with findings status")
        if self.status == "failed" and not self.failure_reason:
            raise ValueError("failed status requires failure_reason")
        if self.status != "failed" and self.failure_reason:
            raise ValueError("failure_reason is allowed only with failed status")
        return self


class SecondaryDecision(StrictModel):
    comment_id: Annotated[int, Field(gt=0)]
    action: Literal["resolve", "reply", "no_action"]
    reply: str | None = None
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_reply(self) -> SecondaryDecision:
        if (self.action == "reply") != bool(self.reply and self.reply.strip()):
            raise ValueError("reply text is required only for the reply action")
        return self


class CandidateFinding(StrictModel):
    id: str = Field(min_length=1)
    finding: Finding


class RoutedFinding(StrictModel):
    target: PullRequestKey
    related_pull_requests: list[PullRequestKey] = Field(default_factory=list)
    finding: Finding


class TicketCorrelationResult(StrictModel):
    findings: list[RoutedFinding] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ConsolidationSelection(StrictModel):
    selected_id: str = Field(min_length=1)
    duplicate_ids: list[str] = Field(default_factory=list)
    severity: Severity


class ConsolidationResult(StrictModel):
    selections: list[ConsolidationSelection]
    existing_comment_duplicates: list[str] = Field(default_factory=list)


class LocationDecision(StrictModel):
    finding_id: str = Field(min_length=1)
    valid: bool
    reason: str = Field(min_length=1)


class LocationVerification(StrictModel):
    decisions: list[LocationDecision]


class PrReviewContext(StrictModel):
    model_config = ConfigDict(validate_assignment=True)

    key: PullRequestKey
    repository: RepositoryIdentity | None = None
    target: PullRequestTarget
    metadata: dict[str, Any] = Field(default_factory=dict)
    diff: str | None = None
    mode: Literal["review", "evidence_only"] = "review"
    status: Literal[
        "discovered", "prepared", "reviewed", "published", "skipped", "failed"
    ] = "discovered"
    secondary_decisions: list[SecondaryDecision] = Field(default_factory=list)
    existing_reviewer_comments: list[dict[str, Any]] = Field(default_factory=list)
    secondary_status: Literal["No issues found", "Done"] | None = None
    specialist_results: dict[AgentRole, ReviewResult] = Field(default_factory=dict)
    candidates: list[CandidateFinding] = Field(default_factory=list)
    findings: list[CandidateFinding] = Field(default_factory=list)
    issues_found: bool = False
    failure_reason: str | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> PrReviewContext:
        if self.key != self.target.key:
            raise ValueError("pull-request context key does not match its target")
        if self.repository is not None and (
            self.repository.project,
            self.repository.repository,
        ) != (self.key.project, self.key.repository):
            raise ValueError("pull-request context repository does not match its key")
        return self


class TicketReviewContext(StrictModel):
    model_config = ConfigDict(validate_assignment=True)

    issue_key: str = Field(min_length=1)
    issue: dict[str, Any]
    jira_comments: list[dict[str, Any]] = Field(default_factory=list)
    reviewer: Person
    requestor: Person
    review_type: ReviewType
    pull_requests: list[PrReviewContext] = Field(default_factory=list)
    correlation: TicketCorrelationResult | None = None
    failures: list[str] = Field(default_factory=list)
    status: Literal["running", "complete", "partial", "failed"] = "running"

    @model_validator(mode="after")
    def validate_pull_request_keys(self) -> TicketReviewContext:
        keys = [
            (item.key.project, item.key.repository, item.key.id)
            for item in self.pull_requests
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("ticket context contains duplicate pull-request keys")
        return self
