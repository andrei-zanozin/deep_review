from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from strands import tool
from unidiff import PatchSet

from deep_review.errors import WorkflowError
from deep_review.models import (
    Finding,
    PullRequestTarget,
    RepositoryIdentity,
    Side,
    _repository_identity_key,
)

LOGGER = logging.getLogger(__name__)

REMOTE_PATTERNS = (
    re.compile(r"^https?://[^/]+/(?:scm/)?(?P<project>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"),
    re.compile(r"^ssh://[^/]+/(?:scm/)?(?P<project>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"),
    re.compile(r"^[^@]+@[^:]+:(?P<project>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"),
)


def git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise WorkflowError(detail)
    return result.stdout.strip()


def discover_repository(start: Path) -> RepositoryIdentity:
    root_text = git(start, "rev-parse", "--show-toplevel")
    root = Path(root_text).resolve()
    remote = git(root, "remote", "get-url", "origin")
    project, repository = parse_bitbucket_remote(remote)
    return RepositoryIdentity(root=root, project=project, repository=repository)


def discover_sibling_repositories(
    start: Path,
) -> dict[tuple[str, str], RepositoryIdentity]:
    """Discover unambiguous Bitbucket checkouts beside the current repository."""
    current = discover_repository(start)
    candidates = [current.root]
    candidates.extend(
        child
        for child in sorted(current.root.parent.iterdir())
        if child != current.root and child.is_dir() and (child / ".git").exists()
    )
    current_identity = _repository_identity_key(current.project, current.repository)
    discovered: dict[tuple[str, str], RepositoryIdentity] = {current_identity: current}
    ambiguous: set[tuple[str, str]] = set()
    for candidate in candidates:
        try:
            repository = discover_repository(candidate)
        except WorkflowError as exc:
            LOGGER.warning(
                "local repository candidate skipped: path=%s, reason=%s",
                candidate.resolve(),
                exc,
            )
            continue
        identity = _repository_identity_key(repository.project, repository.repository)
        if identity == current_identity:
            if repository.root != current.root:
                LOGGER.warning(
                    "duplicate local repository identity ignored in favor of current checkout: "
                    "identity=%s/%s, current_path=%s, duplicate_path=%s",
                    repository.project,
                    repository.repository,
                    current.root,
                    repository.root,
                )
            continue
        if identity in discovered and discovered[identity].root != repository.root:
            LOGGER.warning(
                "ambiguous local repository identity omitted: normalized_identity=%s/%s, "
                "candidate_identity=%s/%s, paths=%s, %s",
                identity[0],
                identity[1],
                repository.project,
                repository.repository,
                discovered[identity].root,
                repository.root,
            )
            ambiguous.add(identity)
        else:
            discovered[identity] = repository
    for identity in ambiguous:
        discovered.pop(identity, None)
    return discovered


def parse_bitbucket_remote(remote: str) -> tuple[str, str]:
    for pattern in REMOTE_PATTERNS:
        if match := pattern.fullmatch(remote.strip().rstrip("/")):
            return match.group("project"), match.group("repo")
    raise WorkflowError("origin is not an unambiguous Bitbucket repository URL")


def prepare_checkout(root: Path, target: PullRequestTarget) -> None:
    _validate_branch(root, target.source_branch)
    _validate_branch(root, target.target_branch)
    if git(root, "status", "--porcelain"):
        raise WorkflowError("local PR checkout verification failed: local checkout is not clean")
    _ensure_commit(root, target.target_branch, target.reviewed_base)
    if git(root, "rev-parse", "HEAD") == target.reviewed_head:
        return

    remote_ref = f"refs/remotes/origin/{target.source_branch}"
    git(
        root,
        "fetch",
        "--no-write-fetch-head",
        "origin",
        f"refs/heads/{target.source_branch}:{remote_ref}",
    )
    if git(root, "rev-parse", remote_ref) != target.reviewed_head:
        raise WorkflowError(
            "local PR checkout verification failed: "
            "fetched source branch does not match the PR head"
        )

    local_exists = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{target.source_branch}"],
        cwd=root,
        check=False,
        timeout=30,
    ).returncode == 0
    if not local_exists:
        git(root, "switch", "--track", "-c", target.source_branch, remote_ref)
    else:
        local_head = git(root, "rev-parse", target.source_branch)
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", local_head, target.reviewed_head],
            cwd=root,
            check=False,
            timeout=30,
        ).returncode == 0
        if not ancestor:
            raise WorkflowError(
                "local PR checkout verification failed: local source branch is ahead or divergent"
            )
        git(root, "switch", target.source_branch)
        git(root, "merge", "--ff-only", remote_ref)

    if git(root, "rev-parse", "HEAD") != target.reviewed_head or git(
        root, "status", "--porcelain"
    ):
        raise WorkflowError("local PR checkout verification failed: final verification failed")


def require_current_target(root: Path, target: PullRequestTarget) -> None:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", target.reviewed_base, target.reviewed_head],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode == 0:
        return
    if result.returncode == 1:
        raise WorkflowError(
            f"source branch {target.source_branch} ({target.reviewed_head}) does not contain "
            f"target branch {target.target_branch} ({target.reviewed_base}); rebase onto or merge "
            f"{target.target_branch} before rerunning the review"
        )
    detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
    raise WorkflowError(f"cannot verify target branch ancestry: {detail}")


def pull_request_diff(root: Path, target: PullRequestTarget, unified: int = 3) -> str:
    if unified < 0:
        raise ValueError("diff context must not be negative")
    base = pull_request_merge_base(root, target)
    diff = git(
        root,
        "diff",
        "--no-ext-diff",
        "--find-renames",
        f"--unified={unified}",
        base,
        target.reviewed_head,
    )
    return f"{diff}\n" if diff else "No changes."


def pull_request_merge_base(root: Path, target: PullRequestTarget) -> str:
    return git(root, "merge-base", target.reviewed_base, target.reviewed_head)


def finding_location_exists(root: Path, target: PullRequestTarget, finding: Finding) -> bool:
    return _location_error(root, target, finding.path, finding.line, finding.side) is None


def check_finding_location(
    root: Path,
    target: PullRequestTarget,
    diff: str,
    path: str,
    line: int,
    side: str,
) -> dict[str, bool | str]:
    """Check a line against one reviewed PR and its effective diff."""
    if side not in {Side.SOURCE, Side.DESTINATION}:
        return {"valid": False, "inline": False, "reason": "invalid diff side"}
    error = _location_error(root, target, path, line, Side(side))
    if error:
        return {"valid": False, "inline": False, "reason": error}
    inline = _line_in_diff(diff, path, line, Side(side))
    return {
        "valid": True,
        "inline": inline,
        "reason": "valid diff line" if inline else "valid file line outside the PR diff",
    }


def _location_error(
    root: Path, target: PullRequestTarget, path: str, line: int, side: Side
) -> str | None:
    if not path or path == ".":
        return "path is not a repository-relative file"
    try:
        safe_path = _safe_relative(path)
    except ValueError:
        return "path is not normalized and repository-relative"
    if safe_path != path:
        return "path is not normalized and repository-relative"
    if line < 1:
        return "line must be positive"
    revision = (
        pull_request_merge_base(root, target)
        if side == Side.SOURCE
        else target.reviewed_head
    )
    file_spec = f"{revision}:{safe_path}"
    kind = subprocess.run(
        ["git", "cat-file", "-t", file_spec],
        cwd=root,
        check=False,
        capture_output=True,
        timeout=30,
    )
    if kind.returncode:
        return "file does not exist on the selected diff side"
    if kind.stdout.strip() != b"blob":
        return "path is not a file"
    result = subprocess.run(
        ["git", "show", file_spec],
        cwd=root,
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        return "file does not exist on the selected diff side"
    if b"\0" in result.stdout:
        return "file is binary"
    if line > len(result.stdout.splitlines()):
        return "line is outside the file"
    return None


def _ensure_commit(root: Path, branch: str, commit: str) -> None:
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=root,
        check=False,
        timeout=30,
    ).returncode == 0
    if exists:
        return
    remote_ref = f"refs/remotes/origin/{branch}"
    git(
        root,
        "fetch",
        "--no-write-fetch-head",
        "origin",
        f"refs/heads/{branch}:{remote_ref}",
    )
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=root,
        check=False,
        timeout=30,
    ).returncode == 0
    if not exists:
        raise WorkflowError(
            "local PR checkout verification failed: fetched branch does not contain "
            "the reviewed commit"
        )


def location_in_diff(diff: str, finding: Finding) -> bool:
    return _line_in_diff(diff, finding.path, finding.line, finding.side)


def _line_in_diff(diff: str, path: str, line: int, side: Side) -> bool:
    try:
        patch = PatchSet(diff.splitlines(keepends=True))
    except Exception as exc:
        raise WorkflowError(f"pull-request diff cannot be parsed: {exc}") from exc

    for changed_file in patch:
        changed_path = changed_file.source_file if side == Side.SOURCE else changed_file.target_file
        if _strip_prefix(changed_path) != path:
            continue
        for hunk in changed_file:
            for changed_line in hunk:
                number = (
                    changed_line.source_line_no
                    if side == Side.SOURCE
                    else changed_line.target_line_no
                )
                changed = (
                    changed_line.is_removed or changed_line.is_context
                    if side == Side.SOURCE
                    else changed_line.is_added or changed_line.is_context
                )
                if number == line and changed:
                    return True
    return False


def _strip_prefix(path: str) -> str:
    return path[2:] if path.startswith(("a/", "b/")) else path


def bounded_command(root: Path, command: Sequence[str], limit: int = 40_000) -> str:
    result = subprocess.run(
        list(command),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    output = result.stdout if result.returncode == 0 else result.stderr
    if len(output) <= limit:
        return output
    return f"{output[:limit]}\n[Output truncated; narrow the request.]"


def repository_tools(root: Path) -> list[Any]:
    root = root.resolve()
    return [
        _list_files_tool(root),
        _search_code_tool(root),
        _read_file_tool(root),
        _git_diff_tool(root),
        _git_show_tool(root),
        _git_log_tool(root),
        _git_blame_tool(root),
    ]


def pr_location_tool(root: Path, target: PullRequestTarget, diff: str) -> Any:
    @tool
    def check_location(
        path: str, line: int, side: Literal["source", "destination"]
    ) -> dict[str, bool | str]:
        """Check a file line on this reviewed PR; inline means a diff anchor exists."""
        return check_finding_location(root, target, diff, path, line, side)

    return check_location


def cross_pr_location_tool(pull_requests: list[dict[str, Any]]) -> Any:
    targets = {
        (item["key"]["project"], item["key"]["repository"], item["key"]["id"]): item
        for item in pull_requests
    }

    @tool
    def check_location(
        project: str,
        repository: str,
        pr_id: int,
        path: str,
        line: int,
        side: Literal["source", "destination"],
    ) -> dict[str, bool | str]:
        """Check a line against the exact target PR; inline means a diff anchor exists."""
        item = targets.get((project, repository, pr_id))
        if item is None or item.get("repository_root") is None:
            return {"valid": False, "inline": False, "reason": "unknown reviewed PR"}
        target = PullRequestTarget.model_validate(item["target"])
        return check_finding_location(
            Path(item["repository_root"]), target, item["diff"], path, line, side
        )

    return check_location


def _list_files_tool(root: Path) -> Any:
    @tool
    def list_files(pattern: str = "") -> str:
        """List repository files, optionally retaining paths containing pattern."""
        output = bounded_command(root, ["rg", "--files"])
        if pattern:
            output = "\n".join(line for line in output.splitlines() if pattern in line)
        return output

    return list_files


def _search_code_tool(root: Path) -> Any:
    @tool
    def search_code(query: str, path: str = ".") -> str:
        """Search repository text with ripgrep."""
        safe = _safe_path(root, path)
        return bounded_command(root, ["rg", "-n", "--", query, str(safe.relative_to(root))])

    return search_code


def _read_file_tool(root: Path) -> Any:
    @tool
    def read_file(path: str, start: int = 1, end: int = 240) -> str:
        """Read a bounded inclusive range of lines from a repository file."""
        if start < 1 or end < start or end - start > 500:
            raise ValueError("invalid or excessive line range")
        candidate = _safe_path(root, path)
        if not candidate.is_file():
            raise ValueError("path is not a file")
        lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        stop = min(end, len(lines)) + 1
        return "\n".join(f"{number}: {lines[number - 1]}" for number in range(start, stop))

    return read_file


def _git_diff_tool(root: Path) -> Any:
    @tool
    def git_diff(base: str, head: str = "HEAD") -> str:
        """Read the Git diff between two validated revisions."""
        return bounded_command(
            root,
            ["git", "diff", "--no-ext-diff", _safe_revision(base), _safe_revision(head)],
        )

    return git_diff


def _git_show_tool(root: Path) -> Any:
    @tool
    def git_show(revision: str, path: str = "") -> str:
        """Read a commit or one file at a commit."""
        revision = _safe_revision(revision)
        spec = revision if not path else f"{revision}:{_safe_relative(path)}"
        return bounded_command(root, ["git", "show", "--no-ext-diff", spec])

    return git_show


def _git_log_tool(root: Path) -> Any:
    @tool
    def git_log(path: str = "") -> str:
        """Read recent Git history, optionally for a repository-relative path."""
        command = ["git", "log", "-n", "30", "--oneline"]
        if path:
            command.extend(["--", _safe_relative(path)])
        return bounded_command(root, command)

    return git_log


def _git_blame_tool(root: Path) -> Any:
    @tool
    def git_blame(path: str, start: int = 1, end: int = 200) -> str:
        """Read blame for a bounded line range."""
        if start < 1 or end < start or end - start > 500:
            raise ValueError("invalid or excessive line range")
        safe = _safe_path(root, path).relative_to(root)
        return bounded_command(root, ["git", "blame", f"-L{start},{end}", "--", str(safe)])

    return git_blame


def _safe_path(root: Path, path: str) -> Path:
    candidate = (root / _safe_relative(path)).resolve()
    if not candidate.is_relative_to(root) or not candidate.exists():
        raise ValueError("path is outside the repository or does not exist")
    return candidate


def _safe_relative(path: str) -> str:
    candidate = Path(path)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        if path != ".":
            raise ValueError("path must be normalized and repository-relative")
    return str(candidate)


def _safe_revision(revision: str) -> str:
    if not revision or revision.startswith("-") or not all(
        character.isalnum() or character in "_./^~{}-" for character in revision
    ):
        raise ValueError("invalid Git revision")
    return revision


def _validate_branch(root: Path, branch: str) -> None:
    result = subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise WorkflowError("local PR checkout verification failed: invalid source branch")
