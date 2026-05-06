"""Cross-source schema alignment for the telemetry table.

Per `CLAUDE.md`: the hot-tier DDL, the cold-tier Parquet schema,
the ingester COPY column list, and the cycler MQTT payload (joined
with the orchestrator's experiment-context topic) must all stay in
lock-step or DuckDB cross-tier reads silently corrupt.

Adding a column to one source without updating the others should fail
this test.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pyarrow as pa
import pytest
from ingester.main import COLUMNS, _parse, _parse_context
from parquet_export.export import TELEMETRY_SCHEMA

REPO_ROOT = Path(__file__).resolve().parents[2]
TELEMETRY_DDL = REPO_ROOT / "migrations" / "timescale" / "001_telemetry.sql"

# Mapping from SQL types in the migration to the pyarrow types we expect
# the cold-tier Parquet schema to use. If a new SQL type lands in the
# DDL, add it here so the alignment test covers it.
SQL_TO_PA: dict[str, pa.DataType] = {
    "REAL": pa.float32(),
    "TIMESTAMPTZ": pa.timestamp("us", tz="UTC"),
    "SMALLINT": pa.int16(),
    "INTEGER": pa.int32(),
    "TEXT": pa.string(),
}


def _parse_telemetry_ddl() -> list[tuple[str, str]]:
    """Extract (column_name, sql_type) pairs from the CREATE TABLE in
    declaration order. Small regex parser — sqlglot would be overkill."""
    body = TELEMETRY_DDL.read_text()
    m = re.search(
        r"CREATE TABLE IF NOT EXISTS telemetry\s*\((.*?)\);",
        body,
        flags=re.DOTALL,
    )
    assert m, "could not locate CREATE TABLE telemetry in 001_telemetry.sql"
    cols: list[tuple[str, str]] = []
    for raw_line in m.group(1).splitlines():
        line = raw_line.strip().rstrip(",")
        if not line or line.startswith("--"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) < 2:
            continue
        name = parts[0]
        # First token of the type spec — strips NOT NULL / DEFAULT etc.
        sql_type = parts[1].split()[0].upper()
        cols.append((name, sql_type))
    return cols


@pytest.mark.unit
def test_column_order_aligned_across_sql_parquet_and_ingester() -> None:
    """SQL declaration order ≡ Parquet schema order ≡ ingester COPY columns."""
    sql_cols = [name for name, _ in _parse_telemetry_ddl()]
    parquet_cols = [f.name for f in TELEMETRY_SCHEMA]
    assert parquet_cols == sql_cols, (
        f"parquet schema drift: {parquet_cols} vs SQL {sql_cols}"
    )
    assert COLUMNS == sql_cols, f"ingester COPY drift: {COLUMNS} vs SQL {sql_cols}"


@pytest.mark.unit
def test_pyarrow_types_match_sql_types() -> None:
    for name, sql_type in _parse_telemetry_ddl():
        expected = SQL_TO_PA.get(sql_type)
        assert expected is not None, (
            f"unmapped SQL type {sql_type!r} for column {name!r}; "
            f"add it to SQL_TO_PA"
        )
        actual = TELEMETRY_SCHEMA.field(name).type
        assert actual == expected, (
            f"type drift for {name!r}: SQL {sql_type} → expected pa "
            f"{expected}, got {actual}"
        )


@pytest.mark.unit
def test_mqtt_payload_join_covers_every_copy_column() -> None:
    """Cycler emits fast values on telemetry/<chassis>/<channel>; orchestrator
    emits slow context on experiment/<chassis>/<channel>. The ingester joins
    them at parse time. Every COPY column must end up populated."""
    sample_telemetry = json.dumps(
        {
            "t": 1_700_000_000.0,
            "v": 3.7,
            "i": 0.5,
            "tc": 25.0,
            "soc": 0.5,
            "soh": 1.0,
            "cyc": 12,
            "mode": "cc",
            "err": 0,
        }
    ).encode()
    sample_context_payload = json.dumps(
        {
            "schedule_id": "soak_25c",
            "step_name": "cc_charge",
            "step_index": 0,
            "cycle_index": 12,
            "experiment_id": "exp-1",
        }
    ).encode()

    parsed_ctx = _parse_context("experiment/3/7", sample_context_payload)
    assert parsed_ctx is not None
    key, ctx = parsed_ctx
    assert ctx is not None
    context = {key: ctx}

    row = _parse("telemetry/3/7", sample_telemetry, context)
    assert row is not None, "ingester._parse rejected a known-good payload"
    assert len(row) == len(COLUMNS), (
        f"row tuple width {len(row)} != COPY column count {len(COLUMNS)}"
    )
    by_col = dict(zip(COLUMNS, row, strict=True))
    # Every column must be populated by either the telemetry payload or the
    # joined context — None or "" indicate a parse path is missing.
    for col, val in by_col.items():
        assert val is not None, f"{col} unpopulated after parse+join"
    # Spot-check the join: context fields landed in the right columns.
    assert by_col["schedule_id"] == "soak_25c"
    assert by_col["step_name"] == "cc_charge"
