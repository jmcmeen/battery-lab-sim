"""Postgres / TimescaleDB DSN builder.

Every service in the workspace talks to Postgres or TSDB via asyncpg, and
the previous house style was to f-string the DSN inline:

    dsn = f"postgresql://{user}:{password}@{host}:{port}/{db}"

That breaks the moment a password contains ``@``, ``:``, ``/``, ``%``, or
``#`` — asyncpg's DSN parser splits on those characters. The repo's
default ``lab/lab`` credentials are safe so tests passed, but any
real-world deployment with a generated password would have produced a
parse error or, worse, a misinterpreted connection-string. v0.1.8
centralizes the build here so the percent-encoding can't be forgotten and
so every service uses the same shape.

Use ``urllib.parse.quote`` with ``safe=""`` — no characters left
unencoded — because we are escaping credential fields whose contents are
unconstrained. ``quote_plus`` is wrong (DSN authority component is not
``application/x-www-form-urlencoded``).
"""

from __future__ import annotations

from urllib.parse import quote


def make_dsn(user: str, password: str, host: str, port: int | str, db: str) -> str:
    """Build a safe ``postgresql://`` DSN.

    ``user`` and ``password`` are percent-encoded so credentials with
    ``@``, ``:``, ``/``, ``%``, or ``#`` round-trip through asyncpg /
    ``urllib.parse.urlparse`` correctly. ``host``, ``port``, and ``db``
    are interpolated as-is — host is operator-controlled (a docker
    service name like ``timescaledb``), port is numeric, and db is a
    Postgres identifier we own.
    """
    u = quote(user, safe="")
    p = quote(password, safe="")
    return f"postgresql://{u}:{p}@{host}:{port}/{db}"
