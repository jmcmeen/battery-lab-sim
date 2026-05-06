"""Regression for `chaos/kill_cycler.sh` — single-cycler outage is contained."""

from __future__ import annotations

import pytest


@pytest.mark.chaos
def test_kill_cycler(chaos_stack, run_chaos_script) -> None:
    result = run_chaos_script("kill_cycler.sh", env_extra={"CYCLER": "cycler_01"})
    if result.returncode != 0:
        pytest.fail(
            f"\n--- chaos/kill_cycler.sh exited {result.returncode} ---\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}\n"
        )
    assert "PASS" in result.stdout
