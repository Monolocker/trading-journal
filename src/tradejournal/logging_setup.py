"""Logging configuration for command-line runs."""

from __future__ import annotations

import logging
import sys

def configure_logging(level: str = "INFO", *, quiet: bool = False) -> None:
    """Send log records to stderr so stdout stays parseable.
    
    Reports go to stdout, while diagnostics go to stderr, meaning a
    summarization command like `tradejournal report` captures the report
    in out.txt. This allows findings to remain visible in the terminal.
    """
    resolved = logging.CRITICAL if quiet else _level_for(level)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved)

    def _level_for(level: str) -> int:
        resolved = logging.getLevelNamesMapping().get(level.strip().upper())
        if resolved is None:
            # Unrecognized level should not abort a run
            # INFO is the safe middle and fallback is announced
            logging.getLogger(__name__).warning(
                "unknown log level %r; falling back to INFO", level
            )
            return logging.INFO
        return resolved