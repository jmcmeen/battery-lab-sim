"""Root conftest — adds service src directories to sys.path for local test runs.

Inside Docker the services install themselves via `uv sync --package <name>`.
For local pytest runs, the workspace's editable installs only cover the
shared lib `batterylab`, so we need to surface the service packages explicitly.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent
for src in (
    _ROOT / "services" / "cycler" / "src",
    _ROOT / "services" / "chamber" / "src",
    _ROOT / "services" / "ingester" / "src",
    _ROOT / "services" / "orchestrator" / "src",
    _ROOT / "services" / "watchdog" / "src",
    _ROOT / "services" / "parquet_export" / "src",
    _ROOT / "services" / "analytics" / "src",
):
    p = str(src)
    if src.is_dir() and p not in sys.path:
        sys.path.insert(0, p)
