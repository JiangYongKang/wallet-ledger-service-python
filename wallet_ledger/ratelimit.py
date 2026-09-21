"""令牌桶 + 在途数限流：超限时显式 RateLimitedError(429)，不静默丢弃。

- 令牌桶控制稳态吞吐（rate_per_sec）与突发（burst）；
- 在途计数限制并发批量数，防止积压扫描无界占用资源；
- 上游失败重试前应等待 Retry-After，失败重试因下游状态机幂等不会重复入账。
"""

from __future__ import annotations

import threading
import time

from .errors import RateLimitedError


class RateLimiter:
    def __init__(
        self,
        rate_per_sec: float = 100.0,
        burst: int = 200,
        max_inflight: int = 8,
    ) -> None:
        self._rate = float(rate_per_sec)
        self._burst = int(burst)
        self._max_inflight = int(max_inflight)
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._inflight = 0
        self._lock = threading.Lock()

    def _refill(self, now: float) -> None:
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            self._updated = now

    def acquire(self, tokens: int = 1, timeout: float = 0.0) -> None:
        """获取令牌并占用一个在途名额。

        桶空：timeout>0 时最多等待 timeout 秒，否则立即 RateLimitedError；
        在途超限：立即 RateLimitedError（含 retry_after 提示）。
        """
        deadline = time.monotonic() + timeout
        with self._lock:
            while True:
                now = time.monotonic()
                self._refill(now)
                if self._inflight >= self._max_inflight:
                    raise RateLimitedError(
                        "too many inflight requests",
                        details={"retry_after": 0.05, "reason": "inflight"},
                    )
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    self._inflight += 1
                    return
                if timeout <= 0:
                    deficit = tokens - self._tokens
                    raise RateLimitedError(
                        "rate limit exceeded",
                        details={
                            "retry_after": round(deficit / self._rate, 4),
                            "reason": "tokens",
                        },
                    )
                wait = (tokens - self._tokens) / self._rate
                if time.monotonic() + wait > deadline:
                    raise RateLimitedError(
                        "rate limit exceeded within timeout",
                        details={"reason": "tokens"},
                    )
                self._lock.release()
                time.sleep(wait)
                self._lock.acquire()

    def release(self) -> None:
        with self._lock:
            if self._inflight > 0:
                self._inflight -= 1

    @property
    def inflight(self) -> int:
        return self._inflight
