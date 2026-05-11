"""DSN builder: credentials with reserved characters must round-trip
through asyncpg / urllib.parse without splitting the authority component.

The v0.1.7-and-earlier callsites f-stringed credentials into the URL,
which silently corrupted any password containing ``@``, ``:``, ``/``,
``%``, or ``#``. These cases lock the percent-encoding contract.
"""

from __future__ import annotations

from urllib.parse import unquote, urlparse

import pytest
from batterylab.db import make_dsn


@pytest.mark.unit
def test_plain_credentials_round_trip() -> None:
    """Default lab/lab creds — no escaping, parses cleanly."""
    dsn = make_dsn("lab", "lab", "timescaledb", 5432, "lab")
    parsed = urlparse(dsn)
    assert parsed.scheme == "postgresql"
    assert parsed.hostname == "timescaledb"
    assert parsed.port == 5432
    assert parsed.username == "lab"
    assert parsed.password == "lab"
    assert parsed.path == "/lab"


@pytest.mark.unit
def test_at_in_password_does_not_split_authority() -> None:
    """``@`` is the DSN delimiter between userinfo and host. An unescaped
    ``@`` in the password makes urlparse treat the second segment as the
    hostname — silently misrouted connections, not a clean error."""
    dsn = make_dsn("lab", "pa@ss", "timescaledb", 5432, "lab")
    parsed = urlparse(dsn)
    assert parsed.hostname == "timescaledb"
    assert parsed.password is not None
    assert unquote(parsed.password) == "pa@ss"


@pytest.mark.unit
def test_percent_in_password_round_trips() -> None:
    """``%`` is the percent-encoding sigil; an unescaped ``%xx`` looks
    like an encoded byte and would be silently decoded."""
    dsn = make_dsn("lab", "p%ss", "timescaledb", 5432, "lab")
    parsed = urlparse(dsn)
    assert parsed.password is not None
    assert unquote(parsed.password) == "p%ss"


@pytest.mark.unit
def test_colon_and_slash_in_password_round_trip() -> None:
    """``:`` is the userinfo delimiter, ``/`` ends the authority. Both
    must be percent-encoded inside the password field."""
    dsn = make_dsn("lab", "p:s/word", "timescaledb", 5432, "lab")
    parsed = urlparse(dsn)
    assert parsed.hostname == "timescaledb"
    assert parsed.port == 5432
    assert parsed.username == "lab"
    assert parsed.password is not None
    assert unquote(parsed.password) == "p:s/word"


@pytest.mark.unit
def test_special_chars_in_user_round_trip() -> None:
    """Users named ``svc@team`` or ``role:env`` exist in the wild."""
    dsn = make_dsn("svc@team", "secret", "timescaledb", 5432, "lab")
    parsed = urlparse(dsn)
    assert parsed.username is not None
    assert unquote(parsed.username) == "svc@team"
    assert parsed.password == "secret"


@pytest.mark.unit
def test_hash_in_credentials_round_trip() -> None:
    """``#`` is the URL fragment delimiter — unescaped, urlparse treats
    everything past it as the fragment, silently truncating the password
    and dropping the database name. The docstring's reserved-char list
    names ``#`` explicitly; this case pins that the helper escapes it."""
    dsn = make_dsn("svc#a", "pa#ss", "timescaledb", 5432, "lab")
    parsed = urlparse(dsn)
    assert parsed.hostname == "timescaledb"
    assert parsed.port == 5432
    assert parsed.path == "/lab"
    assert parsed.username is not None
    assert parsed.password is not None
    assert unquote(parsed.username) == "svc#a"
    assert unquote(parsed.password) == "pa#ss"
    # And the fragment must be empty — i.e., the `#` truly was escaped,
    # not left raw to split the authority from a fragment.
    assert parsed.fragment == ""
