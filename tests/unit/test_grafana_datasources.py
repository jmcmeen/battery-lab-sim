"""Datasource provisioning YAML — schema + connection-target pinning.

The dashboards reference datasource UIDs (`tsdb-telemetry`, `pg-metadata`).
Drift between these names + the declared YAML targets is invisible until
a panel renders blank in production. Pin both ends here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

DATASOURCES_FILE = (
    Path(__file__).resolve().parents[2]
    / "grafana"
    / "provisioning"
    / "datasources"
    / "datasources.yml"
)


@pytest.fixture(scope="module")
def datasources() -> list[dict]:
    data = yaml.safe_load(DATASOURCES_FILE.read_text())
    return data["datasources"]


@pytest.mark.unit
def test_apiversion_is_1() -> None:
    data = yaml.safe_load(DATASOURCES_FILE.read_text())
    assert data["apiVersion"] == 1


@pytest.mark.unit
def test_two_datasources_present(datasources: list[dict]) -> None:
    uids = {d["uid"] for d in datasources}
    assert uids == {"tsdb-telemetry", "pg-metadata"}


@pytest.mark.unit
def test_tsdb_points_at_timescaledb(datasources: list[dict]) -> None:
    tsdb = next(d for d in datasources if d["uid"] == "tsdb-telemetry")
    assert tsdb["type"] == "postgres"
    assert tsdb["url"] == "timescaledb:5432"
    assert tsdb["isDefault"] is True
    assert tsdb["jsonData"]["timescaledb"] is True


@pytest.mark.unit
def test_pg_metadata_points_at_postgres(datasources: list[dict]) -> None:
    pg = next(d for d in datasources if d["uid"] == "pg-metadata")
    assert pg["type"] == "postgres"
    assert pg["url"] == "postgres:5432"
    assert pg.get("isDefault", False) is False


@pytest.mark.unit
def test_credentials_are_env_interpolated(datasources: list[dict]) -> None:
    """Hard-coded credentials in this file would leak via git. Use ${VAR} so
    docker-compose forwards from env at provisioning time."""
    for ds in datasources:
        assert ds["user"].startswith("${"), f"{ds['uid']}: user must be env-interpolated"
        pw = ds["secureJsonData"]["password"]
        assert pw.startswith("${"), f"{ds['uid']}: password must be env-interpolated"


@pytest.mark.unit
def test_no_shell_default_substitution(datasources: list[dict]) -> None:
    """Grafana provisioning supports $VAR and ${VAR} only — `${VAR:-default}`
    is silently expanded to the empty string, leaving datasources connected as
    user '' to database '', so every panel renders 'no data'. We've been bitten
    by this once; pin it. Only check actual values, not comments."""

    def _walk(obj: object) -> list[str]:
        if isinstance(obj, str):
            return [obj]
        if isinstance(obj, dict):
            return [s for v in obj.values() for s in _walk(v)]
        if isinstance(obj, list):
            return [s for v in obj for s in _walk(v)]
        return []

    bad = [s for s in _walk(datasources) if ":-" in s and "${" in s]
    assert not bad, (
        f"datasources.yml uses ${{VAR:-default}} shell-default syntax in {bad}. "
        "Grafana provisioning does NOT support this — the field will resolve "
        "to ''. Use plain ${VAR} and put the default in .env.example."
    )
