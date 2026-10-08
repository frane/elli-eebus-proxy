"""Power limits of several energy managers, merged into the one the wallbox gets.

Each energy manager (by SKI) has at most one limit. The wallbox gets the
lowest active one. A limit can end at a given time (LPC ``timePeriod``). If an
energy manager is lost (disconnected or no heartbeat), its limit is replaced
by the failsafe limit for the failsafe duration, as LPC demands of a
controllable system.
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Limit:
    value: float  # W
    active: bool = True
    until: float | None = None  # epoch seconds
    failsafe: bool = False

    def in_force(self, now: float) -> bool:
        return self.active and (self.until is None or now < self.until)


class LimitArbiter:
    def __init__(self, path: Path | None = None, clock=time.time) -> None:
        self.path = path
        self.clock = clock
        self.limits: dict[str, Limit] = {}
        self._load()

    def set(self, ski: str, value: float, active: bool, duration: float | None = None) -> None:
        until = self.clock() + duration if duration and active else None
        self.limits[ski] = Limit(value, active, until)
        self._save()

    def lost(self, ski: str, failsafe_value: float | None, failsafe_duration: float | None) -> bool:
        """The energy manager is gone: apply the failsafe limit (if it ever set a limit).

        Returns True if something changed.
        """
        if ski not in self.limits or self.limits[ski].failsafe:
            return False
        if failsafe_value is None:
            del self.limits[ski]
        else:
            until = self.clock() + failsafe_duration if failsafe_duration else None
            self.limits[ski] = Limit(failsafe_value, True, until, failsafe=True)
        self._save()
        return True

    def expire(self) -> list[str]:
        """Drop limits whose time is up; returns the SKIs whose limit ended."""
        now = self.clock()
        ended = [ski for ski, lim in self.limits.items()
                 if lim.active and lim.until is not None and now >= lim.until]
        for ski in ended:
            lim = self.limits[ski]
            if lim.failsafe:
                del self.limits[ski]
            else:
                self.limits[ski] = Limit(lim.value, False)
        if ended:
            self._save()
        return ended

    def effective(self) -> float | None:
        """The lowest limit in force (W), or None for no limit."""
        now = self.clock()
        values = [lim.value for lim in self.limits.values() if lim.in_force(now)]
        return min(values) if values else None

    def next_deadline(self) -> float | None:
        deadlines = [lim.until for lim in self.limits.values() if lim.active and lim.until is not None]
        return min(deadlines) if deadlines else None

    # persistence (a restart must not lift a limit or failsafe)
    def _load(self) -> None:
        if self.path is None:
            return
        with contextlib.suppress(OSError, ValueError, TypeError):
            data = json.loads(self.path.read_text())
            self.limits = {ski: Limit(**lim) for ski, lim in data.items()}

    def _save(self) -> None:
        if self.path is None:
            return
        with contextlib.suppress(OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({ski: asdict(lim) for ski, lim in self.limits.items()}))
