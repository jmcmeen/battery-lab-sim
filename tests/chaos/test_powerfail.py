"""End-to-end regression for the keystone chaos scenario.

Runs `chaos/powerfail.sh` against the live stack. The bash script
does the heavy lifting (preflight, kill, assertions, restart); this test
wraps it so we get a Python exit code we can wire into pytest, plus output
captured into the test report on failure.
"""

from __future__ import annotations

import pytest


@pytest.mark.chaos
def test_powerfail(chaos_stack, run_chaos_script) -> None:
    result = run_chaos_script("powerfail.sh")
    if result.returncode != 0:
        msg = (
            f"\n--- chaos/powerfail.sh exited {result.returncode} ---\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}\n"
        )
        pytest.fail(msg)
    assert "PASS" in result.stdout, "script exited 0 but no PASS marker — review logs"
