from __future__ import annotations

import argparse
import logging
import sys

from deep_review.errors import WorkflowError
from deep_review.workflow import run_review


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Run the deterministic deep-review workflow.")
    parser.add_argument("jira_issue")
    args = parser.parse_args()
    try:
        result = run_review(args.jira_issue)
        if result.status != "complete":
            raise SystemExit(1)
    except WorkflowError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
