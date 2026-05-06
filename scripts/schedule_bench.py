"""Print a schedule's bench config in a bash-eval-friendly form.

Used by ``scripts/run_soak.sh`` and ``scripts/run_demo.sh`` to read chassis
selection and channel count out of a YAML schedule. Schema validation lives
in ``batterylab.schedule.BenchConfig`` — this is just the shell adapter, so
malformed YAML or out-of-range chassis ids fail here at the same point and
with the same message they'd fail in CI's ``make validate-schedules``.

Output is two ``eval``-safe lines (the chassis list is quoted so bash treats
all ids as a single value, not the first id followed by command lookups):

    CHASSIS_LIST="1 2 3 4 5 6 7 8"
    CHANNELS=32

Callers do ``eval "$(scripts/schedule_bench.py <file>)"`` then ``read -ra
CHASSIS_LIST <<< "$CHASSIS_LIST"`` to get an array. Stdout is the contract;
everything else is stderr.
"""

from __future__ import annotations

import sys
from pathlib import Path

from batterylab.errors import ScheduleError
from batterylab.schedule import load_schedule


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <schedule.yaml>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        schedule, _sha = load_schedule(path)
    except ScheduleError as e:
        print(f"[schedule_bench] {e}", file=sys.stderr)
        return 1

    chassis_str = " ".join(str(c) for c in schedule.bench.chassis)
    print(f'CHASSIS_LIST="{chassis_str}"')
    print(f"CHANNELS={schedule.bench.channels_per_chassis}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
