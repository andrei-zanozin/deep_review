from __future__ import annotations

import io
import logging
import re

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
