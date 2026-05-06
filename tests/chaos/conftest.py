"""Chaos test fixtures.

Chaos tests run against a *real* docker-compose stack — no testcontainers.
They are explicit-opt-in via `make test.chaos`. The user is expected to have
run `make up` first; the fixture verifies that and seeds an experiment if
none is currently running.

Why a long-lived experiment instead of demo-per-test:
- Each chaos scenario is ~30–45s of wall-time waiting (heartbeat thresholds,
  watchdog detection windows, recovery settling). Re-seeding before every
  test would multiply runtime by O(n_tests) for no extra signal.
- The scenarios are independent: `kill_db` doesn't care that `kill_cycler`
  ran before it. They share read-only invariants (stack is up + an experiment
  exists) which the session-scoped fixture pins.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_SCHEDULE = REPO_ROOT / "schedules" / "demo_5cycle.yaml"


def _docker_compose() -> list[str]:
    if shutil.which("docker") is None:
        pytest.skip("docker not installed; chaos tests require a running stack")
    return ["docker", "compose"]


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, **kw)


def _stack_is_up() -> bool:
    res = _run([*_docker_compose(), "ps", "--format", "{{.Service}} {{.Status}}"])
    if res.returncode != 0:
        return False
    seen = {line.split(maxsplit=1)[0]: line for line in res.stdout.splitlines() if line.strip()}
    required = {"postgres", "timescaledb", "mosquitto", "orchestrator", "watchdog", "ingester"}
    if not required <= seen.keys():
        return False
    return all("Up" in seen[svc] for svc in required)


def _psql(sql: str) -> str:
    res = _run(
        [
            *_docker_compose(),
            "exec",
            "-T",
            "postgres",
            "psql",
            "-tA",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "lab",
            "-d",
            "lab",
            "-c",
            sql,
        ]
    )
    if res.returncode != 0:
        raise RuntimeError(f"psql failed: {res.stderr}")
    return res.stdout.strip()


def _running_experiment_count() -> int:
    return int(_psql("SELECT count(*) FROM experiments WHERE status = 'running'") or "0")


def _seed_pending_experiment() -> None:
    """Insert demo schedule + a pending experiment without waiting for completion."""
    schedule_yaml = DEMO_SCHEDULE.read_text()
    # psql inside the container needs the YAML body as a single argument; use stdin.
    # Use a heredoc-style INSERT. Wrap body in dollar-quoted string to avoid
    # quoting hell.
    sql = f"""
    INSERT INTO schedules (id, body_yaml, git_sha)
    VALUES ('chaos_demo', $BODY${schedule_yaml}$BODY$, 'chaos-test')
    ON CONFLICT (id) DO UPDATE SET body_yaml=EXCLUDED.body_yaml, git_sha=EXCLUDED.git_sha;

    INSERT INTO experiments (id, chassis_id, channel_idx, schedule_id, schedule_git_sha, status)
    VALUES ('chaos-seed-c1-ch0', 1, 0, 'chaos_demo', 'chaos-test', 'pending')
    ON CONFLICT (id) DO UPDATE SET status='pending', updated_at=now();
    """
    res = subprocess.run(
        [
            *_docker_compose(),
            "exec",
            "-T",
            "postgres",
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "lab",
            "-d",
            "lab",
        ],
        input=sql,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"seed insert failed: {res.stderr}")


@pytest.fixture(scope="session")
def chaos_stack() -> Iterator[None]:
    """Session-scoped: stack must be up + at least one experiment running.

    Skips (not fails) if the stack isn't up, so `make test` doesn't break for
    contributors who haven't run `make up`. `make test.chaos` is the entry point.
    """
    if not _stack_is_up():
        pytest.skip("chaos tests require `make up` first — required services not all Up")

    if _running_experiment_count() == 0:
        _seed_pending_experiment()
        # Orchestrator polls pending experiments — give it up to 30s to flip to running.
        for _ in range(30):
            if _running_experiment_count() > 0:
                break
            time.sleep(1)
        else:
            pytest.skip("could not seed a running experiment within 30s")

    yield


@pytest.fixture
def run_chaos_script() -> Callable[..., subprocess.CompletedProcess]:
    """Run a chaos script; return CompletedProcess. Caller asserts on returncode/stdout."""

    def _run_script(name: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env_extra = env_extra or {}
        import os

        env = {**os.environ, **env_extra}
        return subprocess.run(
            ["bash", f"chaos/{name}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
        )

    return _run_script
