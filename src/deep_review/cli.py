from __future__ import annotations

import argparse
import logging

from deep_review.errors import WorkflowError
from deep_review.logging_config import configure_logging
from deep_review.workflow import run_review

LOGGER = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the deterministic deep-review workflow.")
    parser.add_argument("--verbose", action="store_true", help="Show tool and MCP diagnostics.")
    parser.add_argument("jira_issue")
    args = parser.parse_args()
    configure_logging(verbose=args.verbose)
    try:
        result = run_review(args.jira_issue, verbose=args.verbose)
        if result.status != "complete":
            raise SystemExit(1)
    except WorkflowError as exc:
        LOGGER.error("Failed: %s", exc)
        raise SystemExit(1) from exc
