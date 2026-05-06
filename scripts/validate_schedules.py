"""Walk schedules/*.yaml and validate each against the Pydantic schema.

Exits 1 on any failure, listing every bad schedule. Used both locally and
in CI as a fast pre-merge gate against schedules that would only blow up
inside the orchestrator on `make demo` / `make soak.start`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from batterylab.errors import ScheduleError
from batterylab.schedule import load_schedule

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEDULES_DIR = REPO_ROOT / "schedules"


def main() -> int:
    yamls = sorted(SCHEDULES_DIR.glob("*.yaml"))
    if not yamls:
        print(f"[validate-schedules] no YAMLs found in {SCHEDULES_DIR}", file=sys.stderr)
        return 1

    failures: list[tuple[Path, str]] = []
    for path in yamls:
        try:
            sched, _sha = load_schedule(path)
        except ScheduleError as e:
            failures.append((path, str(e)))
            continue
        print(f"[validate-schedules] OK  {path.name}  ({sched.schedule_id})")

    if failures:
        print(f"\n[validate-schedules] {len(failures)} failure(s):", file=sys.stderr)
        for path, err in failures:
            print(f"  - {path.relative_to(REPO_ROOT)}: {err}", file=sys.stderr)
        return 1

    print(f"\n[validate-schedules] PASS — {len(yamls)} schedules valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
