from collections import deque
from threading import Lock
from time import monotonic


class InMemoryRateLimiter:
    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        max_keys: int = 10_000,
    ) -> None:
        if limit < 1 or window_seconds <= 0 or max_keys < 1:
            raise ValueError("Rate limiter values must be positive.")
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._attempts: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        now = monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            attempts = self._attempts.setdefault(key, deque())
            while attempts and attempts[0] < cutoff:
                attempts.popleft()
            if len(attempts) >= self.limit:
                return False
            attempts.append(now)
            self._prune(cutoff)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)

    def _prune(self, cutoff: float) -> None:
        if len(self._attempts) <= self.max_keys:
            return
        stale_keys = [
            key
            for key, attempts in self._attempts.items()
            if not attempts or attempts[-1] < cutoff
        ]
        for key in stale_keys:
            self._attempts.pop(key, None)
        while len(self._attempts) > self.max_keys:
            self._attempts.pop(next(iter(self._attempts)))
