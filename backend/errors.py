"""Exception types used across Bridle."""

from typing import Any, Dict, Optional


class BridleError(Exception):
    """An error that maps directly onto an HTTP response."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


class ReplCrashed(Exception):
    """The REPL closed its stdout (process exited) before printing the prompt."""


class OutputLimitExceeded(Exception):
    """A single command produced more output than BRIDLE_MAX_OUTPUT_BYTES."""
