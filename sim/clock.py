"""Deterministic fixed-step sim clock."""
from __future__ import annotations

import random
import time

import numpy as np


class SimClock:
    def __init__(self, control_hz: float = 20.0, realtime_factor: float = 1.0, seed: int = 7):
        self.dt = 1.0 / control_hz
        self.realtime_factor = realtime_factor   # 0 => as fast as possible
        self.t = 0.0
        self.step_n = 0
        self.seed = seed
        self.rng = random.Random(seed)
        self.np_rng = np.random.default_rng(seed)
        self._wall0 = time.monotonic()
        self._t0 = 0.0
        self._m_wall, self._m_t, self.achieved = time.monotonic(), 0.0, 0.0   # measured speed (x real time)

    def tick(self) -> float:
        """Advance one control step; sleeps to keep real-time pace if realtime_factor > 0."""
        self.step_n += 1
        self.t = self.step_n * self.dt
        if self.realtime_factor > 0:
            target = self._wall0 + (self.t - self._t0) / self.realtime_factor
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        now = time.monotonic()
        if now - self._m_wall >= 1.5:
            self.achieved = (self.t - self._m_t) / (now - self._m_wall)
            self._m_wall, self._m_t = now, self.t
        return self.t

    def set_speed(self, rtf: float) -> None:
        """Change the target speed mid-run (x real time; 0 = as fast as possible). Re-bases the pacing."""
        self._wall0 = time.monotonic()
        self._t0 = self.t
        self.realtime_factor = float(rtf)

    def every(self, period_s: float) -> bool:
        k = max(1, round(period_s / self.dt))
        return self.step_n % k == 0
