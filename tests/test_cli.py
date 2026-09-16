from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from deep_review import cli


@pytest.mark.parametrize("verbose", [False, True])
def test_cli_passes_verbose_to_logging_and_review(monkeypatch: Any, verbose: bool) -> None:
    configured: list[bool] = []
    reviewed: list[tuple[str, bool]] = []
    arguments = ["deep-review", "ABC-123"]
    if verbose:
        arguments.insert(1, "--verbose")
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(cli, "configure_logging", lambda *, verbose: configured.append(verbose))

    def fake_review(issue: str, *, verbose: bool) -> SimpleNamespace:
        reviewed.append((issue, verbose))
        return SimpleNamespace(status="complete")

    monkeypatch.setattr(cli, "run_review", fake_review)

    cli.main()

    assert configured == [verbose]
    assert reviewed == [("ABC-123", verbose)]
