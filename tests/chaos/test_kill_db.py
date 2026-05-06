"""Regression for `chaos/kill_db.sh` — TSDB outage and recovery."""

from __future__ import annotations

import pytest


@pytest.mark.chaos
def test_kill_db(chaos_stack, run_chaos_script) -> None:
    result = run_chaos_script("kill_db.sh", env_extra={"OUTAGE_S": "6"})
    if result.returncode != 0:
        pytest.fail(
            f"\n--- chaos/kill_db.sh exited {result.returncode} ---\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}\n"
        )
    assert "PASS" in result.stdout
