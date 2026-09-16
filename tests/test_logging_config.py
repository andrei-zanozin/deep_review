from __future__ import annotations

import io
import logging
import re
import subprocess
import sys

import pytest

from deep_review.logging_config import (
    DeepReviewFormatter,
    _color_enabled,
    api_log_context,
)


class TerminalBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize(
    ("level", "extra", "color"),
    [
        (logging.INFO, {}, "\033[37m"),
        (logging.INFO, api_log_context(), "\033[32m"),
        (logging.WARNING, {}, "\033[33m"),
        (logging.ERROR, api_log_context(), "\033[31m"),
    ],
)
def test_formatter_adds_local_timestamp_and_category_color(
    level: int, extra: dict[str, str], color: str
) -> None:
    record = logging.LogRecord("test", level, __file__, 1, "message", (), None)
    for name, value in extra.items():
        setattr(record, name, value)

    output = DeepReviewFormatter(use_color=True).format(record)

    assert output.startswith(color)
    assert re.fullmatch(
        rf"{re.escape(color)}\d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}:\d{{2}} "
        rf"{record.levelname} message\033\[0m",
        output,
    )


def test_formatter_omits_ansi_when_color_is_disabled() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "message", (), None)

    output = DeepReviewFormatter(use_color=False).format(record)

    assert "\033[" not in output
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO message",
        output,
    )


def test_color_requires_a_terminal_and_honors_no_color() -> None:
    assert _color_enabled(TerminalBuffer(), {})
    assert not _color_enabled(TerminalBuffer(), {"NO_COLOR": "1"})
    assert not _color_enabled(io.StringIO(), {})


@pytest.mark.parametrize("verbose", [False, True])
def test_logging_visibility_keeps_call_severity_and_relabels_http_requests(verbose: bool) -> None:
    script = """
import logging
import sys
from deep_review.logging_config import api_log_context, configure_logging, tool_log_context

configure_logging(verbose=sys.argv[1] == "1")
logging.getLogger("deep_review.workflow").info("workflow progress")
logging.getLogger("deep_review.infrastructure").info("LLM call started", extra=api_log_context())
logging.getLogger("deep_review.infrastructure").error("LLM call failed", extra=api_log_context())
logging.getLogger("deep_review.infrastructure").info("tool started", extra=tool_log_context())
logging.getLogger("deep_review.infrastructure").error("tool failed", extra=tool_log_context())
logging.getLogger("strands.tools.executors._executor").error("framework tool failed")
logging.getLogger("mcp.client").error("MCP client failed")
logging.getLogger("httpx").info('HTTP Request: GET https://example.test/ "HTTP/1.1 200 "')
logging.getLogger("deep_review.usage").info("LLM usage total")
logging.getLogger("deep_review.cli").error("Failed: review aborted")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(int(verbose))],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "INFO workflow progress" in result.stderr
    assert "INFO LLM usage total" in result.stderr
    assert "ERROR Failed: review aborted" in result.stderr
    if verbose:
        assert "INFO LLM call started" in result.stderr
        assert "ERROR LLM call failed" in result.stderr
        assert "INFO tool started" in result.stderr
        assert "ERROR tool failed" in result.stderr
        assert "ERROR framework tool failed" in result.stderr
        assert "ERROR MCP client failed" in result.stderr
        assert "DEBUG HTTP Request: GET https://example.test/" in result.stderr
    else:
        assert "LLM call started" not in result.stderr
        assert "LLM call failed" not in result.stderr
        assert "tool started" not in result.stderr
        assert "tool failed" not in result.stderr
        assert "MCP client failed" not in result.stderr
        assert "HTTP Request:" not in result.stderr
