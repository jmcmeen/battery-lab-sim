"""Catch the 'committed broken JSON' failure mode early.

Asserts every grafana/dashboards/*.json file:
  - parses
  - has the required top-level keys for Grafana provisioning
  - every panel has a non-empty `targets` (or is a `text` panel which doesn't need them)
  - every datasource UID referenced is one of our two declared UIDs

Drift between dashboard datasource UIDs and what's in datasources.yml means
the panel renders with "Datasource not found" — silent UX failure that
doesn't trip Grafana's healthcheck.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "grafana" / "dashboards"
KNOWN_DATASOURCE_UIDS = {"tsdb-telemetry", "pg-metadata"}
REQUIRED_TOP_LEVEL_KEYS = {"uid", "title", "panels", "schemaVersion", "refresh"}
PANELS_WITHOUT_TARGETS = {"text", "row"}


def _all_dashboard_files() -> list[Path]:
    return sorted(p for p in DASHBOARD_DIR.glob("*.json"))


def _iter_panels(panels: list[dict]) -> list[dict]:
    """Flatten any nested panels (Grafana row panels can contain children)."""
    out = []
    for p in panels:
        out.append(p)
        if "panels" in p:
            out.extend(_iter_panels(p["panels"]))
    return out


@pytest.mark.unit
def test_dashboards_directory_has_three_files() -> None:
    files = _all_dashboard_files()
    names = {f.stem for f in files}
    assert names == {"live_bench", "cycle_kpis", "reliability"}, (
        f"unexpected dashboard set: {names}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("path", _all_dashboard_files(), ids=lambda p: p.stem)
def test_dashboard_parses_and_has_required_keys(path: Path) -> None:
    data = json.loads(path.read_text())
    missing = REQUIRED_TOP_LEVEL_KEYS - data.keys()
    assert not missing, f"{path.name}: missing top-level keys {missing}"
    assert isinstance(data["panels"], list) and data["panels"], f"{path.name}: empty panels"


@pytest.mark.unit
@pytest.mark.parametrize("path", _all_dashboard_files(), ids=lambda p: p.stem)
def test_panels_have_targets_or_are_text(path: Path) -> None:
    data = json.loads(path.read_text())
    for panel in _iter_panels(data["panels"]):
        if panel.get("type") in PANELS_WITHOUT_TARGETS:
            continue
        targets = panel.get("targets", [])
        assert targets, (
            f"{path.name} panel id={panel.get('id')} type={panel.get('type')} has no targets"
        )


@pytest.mark.unit
@pytest.mark.parametrize("path", _all_dashboard_files(), ids=lambda p: p.stem)
def test_panel_datasource_uids_are_known(path: Path) -> None:
    data = json.loads(path.read_text())
    for panel in _iter_panels(data["panels"]):
        ds = panel.get("datasource")
        if ds is None:
            # text panels don't need a datasource
            assert panel.get("type") in PANELS_WITHOUT_TARGETS
            continue
        assert isinstance(ds, dict), f"{path.name} panel {panel.get('id')}: ds must be a dict"
        uid = ds.get("uid")
        assert uid in KNOWN_DATASOURCE_UIDS, (
            f"{path.name} panel {panel.get('id')}: datasource uid {uid!r} "
            f"is not one of {KNOWN_DATASOURCE_UIDS}"
        )


@pytest.mark.unit
@pytest.mark.parametrize("path", _all_dashboard_files(), ids=lambda p: p.stem)
def test_uid_matches_filename(path: Path) -> None:
    """Catches accidental copy-paste UID collisions (which would silently
    overwrite an existing dashboard at provisioning time)."""
    data = json.loads(path.read_text())
    expected_uid = path.stem.replace("_", "-")
    assert data["uid"] == expected_uid, (
        f"{path.name}: uid={data['uid']!r}, expected {expected_uid!r}"
    )
