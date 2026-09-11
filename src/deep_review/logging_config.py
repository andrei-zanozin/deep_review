from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from typing import TextIO

API_LOG_CATEGORY = "api"
LOG_CATEGORY_ATTRIBUTE = "deep_review_category"

_RESET = "\033[0m"
_WHITE = "\033[37m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"


class DeepReviewFormatter(logging.Formatter):
    def __init__(self, use_color: bool) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        self._use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if not self._use_color:
            return message
        if record.levelno >= logging.ERROR:
            color = _RED
        elif record.levelno >= logging.WARNING:
            color = _YELLOW
        elif getattr(record, LOG_CATEGORY_ATTRIBUTE, None) == API_LOG_CATEGORY:
            color = _GREEN
        else:
            color = _WHITE
        return f"{color}{message}{_RESET}"


def configure_logging(
    stream: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> None:
    output = stream if stream is not None else sys.stderr
    variables = environment if environment is not None else os.environ
    use_color = _color_enabled(output, variables)
    handler = logging.StreamHandler(output)
    handler.setFormatter(DeepReviewFormatter(use_color=use_color))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def api_log_context() -> dict[str, str]:
    return {LOG_CATEGORY_ATTRIBUTE: API_LOG_CATEGORY}


def _is_terminal(stream: TextIO) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _color_enabled(stream: TextIO, environment: Mapping[str, str]) -> bool:
    return _is_terminal(stream) and "NO_COLOR" not in environment
