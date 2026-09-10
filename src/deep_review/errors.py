class WorkflowError(RuntimeError):
    """A fail-closed workflow error safe to report to the caller."""


class ConfigurationError(WorkflowError):
    """A startup configuration error safe to report without exposing values."""
