"""structlog setup — JSON output, bound context per task."""

from __future__ import annotations

import logging
import sys

import structlog


def configure(level: str = "INFO") -> None:
    """Wire stdlib logging + structlog for JSON output to stdout.

    Idempotent on re-call (structlog caches the wrapper). Services call
    this once at startup; tests don't need to call it because the default
    structlog config emits readable strings.
    """
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=getattr(logging, level.upper())
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        cache_logger_on_first_use=True,
    )


def get(name: str) -> structlog.stdlib.BoundLogger:
    """Return a logger bound to ``name`` (typically the module dotted path).

    Per CLAUDE.md, services use ``get("<service>.<component>")`` and bind
    request-scoped context (cell_id, experiment_id) at the top of each
    task with ``structlog.contextvars.bind_contextvars``.
    """
    return structlog.get_logger(name)  # type: ignore[no-any-return]
