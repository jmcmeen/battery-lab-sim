"""End-to-end provisioning: a real Grafana 11.2.0 boots with our config
files bind-mounted, and exposes both datasources and all three dashboards
via its HTTP API.

This catches the kind of failure that unit tests miss:
  - YAML schema accepted but Grafana refuses on load
  - dashboard JSON structurally valid but rejected by Grafana's parser
  - provisioning paths inside the container don't match what the YAML
    declared

No data-rendering assertions — Grafana's render API needs an active
backend connection that's noisy to set up in test. We assert presence,
not visual correctness.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import requests
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

REPO_ROOT = Path(__file__).resolve().parents[2]
PROVISIONING_DIR = REPO_ROOT / "grafana" / "provisioning"
DASHBOARDS_DIR = REPO_ROOT / "grafana" / "dashboards"


@pytest.fixture(scope="module")
def grafana_container() -> Iterator[str]:
    """Real Grafana 11.2.0 with our provisioning + dashboards mounted RO.
    Yields the base URL (http://host:port).
    """
    c = (
        DockerContainer("grafana/grafana:11.2.0")
        .with_env("GF_AUTH_ANONYMOUS_ENABLED", "true")
        .with_env("GF_AUTH_ANONYMOUS_ORG_ROLE", "Viewer")
        .with_env("GF_SECURITY_ADMIN_PASSWORD", "admin")
        # Provisioning needs these for the YAML's ${VAR} expansion. Pointing
        # at unreachable hosts is fine for this test — we only check that
        # the datasources are *registered*, not that they connect.
        .with_env("TSDB_USER", "lab")
        .with_env("TSDB_PASSWORD", "lab")
        .with_env("TSDB_DB", "telemetry")
        .with_env("POSTGRES_USER", "lab")
        .with_env("POSTGRES_PASSWORD", "lab")
        .with_env("POSTGRES_DB", "lab")
        .with_volume_mapping(str(PROVISIONING_DIR), "/etc/grafana/provisioning", "ro")
        .with_volume_mapping(str(DASHBOARDS_DIR), "/var/lib/grafana/dashboards", "ro")
        .with_exposed_ports(3000)
    )
    c.start()
    try:
        wait_for_logs(c, "HTTP Server Listen", timeout=60)
        host = c.get_container_host_ip()
        port = int(c.get_exposed_port(3000))
        base_url = f"http://{host}:{port}"

        # Wait for /api/health to respond OK — provisioning runs async after
        # the HTTP server starts, so dashboards may not be searchable for
        # the first second or two.
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                r = requests.get(f"{base_url}/api/health", timeout=2)
                if r.ok:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("grafana /api/health never returned ok")
        yield base_url
    finally:
        c.stop()


def _get(base_url: str, path: str) -> dict | list:
    """GET against the anonymous Viewer role. Provisioning grants org access
    to anonymous users so we don't need to send Basic auth here."""
    r = requests.get(f"{base_url}{path}", timeout=5, auth=("admin", "admin"))
    r.raise_for_status()
    return r.json()


@pytest.mark.integration
def test_both_datasources_provisioned(grafana_container: str) -> None:
    datasources = _get(grafana_container, "/api/datasources")
    uids = {ds["uid"] for ds in datasources}
    assert {"tsdb-telemetry", "pg-metadata"} <= uids, (
        f"expected both datasources provisioned, got uids={uids}"
    )


@pytest.mark.integration
def test_three_dashboards_provisioned(grafana_container: str) -> None:
    # Dashboards arrive asynchronously — give the provisioner up to 15 s.
    deadline = time.time() + 15
    while time.time() < deadline:
        results = _get(grafana_container, "/api/search?type=dash-db")
        uids = {d["uid"] for d in results}
        if {"live-bench", "cycle-kpis", "reliability"} <= uids:
            break
        time.sleep(0.5)
    else:
        results = _get(grafana_container, "/api/search?type=dash-db")
        pytest.fail(
            f"expected dashboards live-bench, cycle-kpis, reliability; "
            f"got {[d['uid'] for d in results]}"
        )


@pytest.mark.integration
def test_dashboards_at_top_level(grafana_container: str) -> None:
    """Per the comment in grafana/provisioning/dashboards/dashboards.yml,
    dashboards are intentionally provisioned to the General (top-level)
    folder — this Grafana instance only serves Battery Lab, so a nested
    folder named after the provider is just an extra click.

    Top-level dashboards have either no `folderTitle` field or
    `folderTitle == "General"` in Grafana 11's search API.
    """
    # Wait for provisioning to settle before reading.
    deadline = time.time() + 15
    results: list[dict] = []
    while time.time() < deadline:
        results = _get(grafana_container, "/api/search?type=dash-db")  # type: ignore[assignment]
        if results:
            break
        time.sleep(0.5)

    assert results, "no dashboards visible from the search API"
    misplaced = [d for d in results if d.get("folderTitle") not in (None, "", "General")]
    assert not misplaced, (
        f"dashboards leaked into a non-top-level folder: "
        f"{[(d.get('uid'), d.get('folderTitle')) for d in misplaced]}"
    )
