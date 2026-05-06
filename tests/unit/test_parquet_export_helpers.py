"""Pure-function tests for parquet_export helpers.

Hour-floor and Hive path are pure; no I/O. Schema is pinned to a stable
column order — drift in either direction breaks DuckDB's read_parquet.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pyarrow as pa
import pytest
from parquet_export.export import TELEMETRY_SCHEMA, hour_floor
from parquet_export.s3 import hive_path


@pytest.mark.unit
def test_hour_floor_strips_subhour_components() -> None:
    ts = datetime(2026, 5, 5, 14, 23, 11, 999_999, tzinfo=UTC)
    result = hour_floor(ts)
    assert result.year == 2026 and result.month == 5 and result.day == 5
    assert result.hour == 14 and result.minute == 0 and result.second == 0
    assert result.microsecond == 0
    assert result.tzinfo == UTC


@pytest.mark.unit
def test_hour_floor_naive_input_becomes_utc() -> None:
    """The exporter normalizes timestamps to UTC even if the caller wasn't tz-aware."""
    ts = datetime(2026, 5, 5, 14, 0, 0)
    result = hour_floor(ts)
    assert result.tzinfo == UTC


@pytest.mark.unit
def test_hive_path_zero_pads_partitions() -> None:
    """Lexical sort = chronological sort. Two-digit padding is required for
    DuckDB's hive_partitioning to glob predictably."""
    ts = datetime(2026, 1, 5, 9, 0, 0, tzinfo=UTC)
    path = hive_path("lab-archive", ts)
    assert path == "lab-archive/telemetry/year=2026/month=01/day=05/hour=09/data.parquet"


@pytest.mark.unit
def test_hive_path_late_year() -> None:
    ts = datetime(2026, 12, 31, 23, 0, 0, tzinfo=UTC)
    path = hive_path("lab-archive", ts)
    assert path == "lab-archive/telemetry/year=2026/month=12/day=31/hour=23/data.parquet"


@pytest.mark.unit
def test_read_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_read_env`` is shared between the periodic loop and ``--now``. Drift
    in defaults silently changes service behaviour (e.g. someone bumps the
    default bucket and ``parquet.export.now`` writes to a stale prefix).
    Pin the documented defaults + their types here.
    """
    from parquet_export.main import _read_env

    for var in (
        "TSDB_HOST",
        "TSDB_PORT",
        "TSDB_USER",
        "TSDB_PASSWORD",
        "TSDB_DB",
        "MINIO_ENDPOINT",
        "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY",
        "PARQUET_BUCKET",
        "PARQUET_EXPORT_AGE_HOURS",
        "PARQUET_EXPORT_PERIOD_S",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = _read_env()

    assert cfg["tsdb_host"] == "timescaledb"
    assert cfg["tsdb_port"] == 5432
    assert cfg["tsdb_user"] == "lab"
    assert cfg["tsdb_pw"] == "lab"
    assert cfg["tsdb_db"] == "telemetry"
    assert cfg["minio_endpoint"] == "minio:9000"
    assert cfg["minio_access"] == "admin"
    assert cfg["minio_secret"] == "admin12345"
    assert cfg["bucket"] == "lab-archive"
    assert cfg["age_hours"] == 24
    assert cfg["period_s"] == 3600.0

    assert isinstance(cfg["tsdb_port"], int)
    assert isinstance(cfg["age_hours"], int)
    assert isinstance(cfg["period_s"], float)


@pytest.mark.unit
def test_read_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env overrides parse with the right types — string→int for port/age,
    string→float for period. Catches a regression where someone refactors
    ``_read_env`` to ``os.environ.get(...)`` without the cast."""
    from parquet_export.main import _read_env

    monkeypatch.setenv("TSDB_PORT", "6543")
    monkeypatch.setenv("PARQUET_EXPORT_AGE_HOURS", "0")
    monkeypatch.setenv("PARQUET_EXPORT_PERIOD_S", "120.5")
    monkeypatch.setenv("PARQUET_BUCKET", "custom-bucket")

    cfg = _read_env()

    assert cfg["tsdb_port"] == 6543
    assert cfg["age_hours"] == 0
    assert cfg["period_s"] == 120.5
    assert cfg["bucket"] == "custom-bucket"


@pytest.mark.unit
def test_telemetry_schema_matches_tsdb_column_order() -> None:
    """Drift between this schema and migrations/timescale/001_telemetry.sql
    silently corrupts cold-tier reads. Pin the column order + types here.
    """
    expected = [
        ("time", pa.timestamp("us", tz="UTC")),
        ("chassis_id", pa.int16()),
        ("channel_idx", pa.int16()),
        ("schedule_id", pa.string()),
        ("cycle_index", pa.int32()),
        ("step_name", pa.string()),
        ("voltage_v", pa.float32()),
        ("current_a", pa.float32()),
        ("temperature_c", pa.float32()),
        ("soc_est", pa.float32()),
    ]
    actual = [(f.name, f.type) for f in TELEMETRY_SCHEMA]
    assert actual == expected
