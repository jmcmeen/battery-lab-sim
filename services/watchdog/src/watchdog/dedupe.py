"""Edge-triggered alert suppression.

Each (source, chassis_id, channel_idx) key holds an "armed" bit. `update(key,
condition)` returns True only on the rising edge (clear → tripped) and re-arms
when the condition clears. This prevents the watchdog from re-emitting the
same alert on every poll while the underlying condition persists.

Pure function — no I/O. Trivially unit-testable.
"""

from __future__ import annotations

from collections.abc import Hashable


class EdgeTrigger:
    """In-memory rising-edge dedupe keyed by hashable tuple.

    Stateful but tiny — one bit per active key. Survives only as long
    as the watchdog process; on restart the next ``update`` for any key
    re-emits if the condition is still active, which is the desired
    behaviour (operator sees a fresh alert after a restart).
    """

    __slots__ = ("_state",)

    def __init__(self) -> None:
        self._state: dict[Hashable, bool] = {}

    def update(self, key: Hashable, condition: bool) -> bool:
        """Return True only on a rising edge (False → True). Re-arms on falling edge."""
        prev = self._state.get(key, False)
        self._state[key] = condition
        return condition and not prev

    def reset(self, key: Hashable) -> None:
        """Force-clear so the next True triggers an edge. Used on (re)subscribe."""
        self._state.pop(key, None)
