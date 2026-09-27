"""批量确认扫描 + 限流策略。

- 扫描按区块高度（tip）单调推进；回退的 tip -> OutOfOrderError(422)，不静默。
- 单批事件数受 max_batch_size 限制；整体受 RateLimiter 的吞吐/在途约束，
  处理积压或上游失败时按策略延迟（等待令牌）或拒绝（429，带 retry_after）。
- 同一批允许混着多种资产的事件：资产由事件自然键解析，每条只落自己的资产
  口径；限流按“事件数”计令牌，资产种类增多不会放大资源占用。
- 同一批若 reorg_keys 与确认事件同时到达：先回撤后确认，结论确定可复现。
- 所有下游变更由 LedgerService 的状态机幂等保证，重试不重复入账。
"""

from __future__ import annotations

from dataclasses import dataclass

from .audit import get_logger
from .errors import OutOfOrderError, RateLimitedError, ValidationError
from .ratelimit import RateLimiter
from .service import LedgerService

log = get_logger("wallet_ledger.scanner")


@dataclass(slots=True)
class ScanPolicy:
    required_confirmations: int = 3
    finalization_depth: int = 64
    max_batch_size: int = 100
    max_inflight: int = 4
    rate_per_sec: float = 100.0
    burst: int = 200


class ConfirmationScanner:
    def __init__(self, service: LedgerService, policy: ScanPolicy | None = None) -> None:
        self.service = service
        self.policy = policy or ScanPolicy()
        self._last_tip = 0
        self._limiter = RateLimiter(
            rate_per_sec=self.policy.rate_per_sec,
            burst=self.policy.burst,
            max_inflight=self.policy.max_inflight,
        )

    @property
    def last_tip(self) -> int:
        return self._last_tip

    def scan_batch(
        self,
        principal,
        tip_height: int,
        events: list[tuple[str, int, int]],
        reorg_keys: list[tuple[str, int]] | None = None,
        acquire_timeout: float = 0.0,
    ) -> dict:
        """执行一批“先回撤后确认”的确定性扫描。

        events 为 (tx_hash, log_index, confirmations) 三元组列表。
        """
        reorg_keys = reorg_keys or []

        if not isinstance(tip_height, int) or tip_height < 0:
            raise ValidationError("tip_height must be non-negative integer")
        if tip_height < self._last_tip:
            raise OutOfOrderError(
                "tip height moved backwards",
                details={"last_tip": self._last_tip, "tip_height": tip_height},
            )
        if len(events) > self.policy.max_batch_size:
            # 显式拒绝而非截断，防止调用方误以为全部处理。
            raise RateLimitedError(
                "batch size exceeds limit",
                details={
                    "reason": "batch_size",
                    "max_batch_size": self.policy.max_batch_size,
                    "retry_after": 0.0,
                },
            )

        # 在途 + 令牌：积压时按 timeout 决定延迟等待还是立即拒绝。
        self._limiter.acquire(tokens=max(1, len(events)), timeout=acquire_timeout)
        try:
            # 确定性：事件按自然键排序，回撤优先于确认。
            ordered_events = sorted(events, key=lambda e: (e[0], e[1]))
            ordered_reorg = sorted(reorg_keys, key=lambda k: (k[0], k[1]))

            reorged = self.service.apply_reorg(
                principal, ordered_reorg, detached_height=tip_height
            )
            # 回撤后的确认在 service 内被忽略；同时按确认数分组更新。
            affected: list = []
            for tx_hash, log_index, confirmations in ordered_events:
                if confirmations < 0:
                    raise ValidationError(
                        "confirmations must be >= 0",
                        details={"key": [tx_hash, log_index]},
                    )
                affected.extend(
                    self.service.apply_confirmations(
                        principal,
                        [(tx_hash, log_index)],
                        confirmations=confirmations,
                        tip_height=tip_height,
                        required_confirmations=self.policy.required_confirmations,
                        finalization_depth=self.policy.finalization_depth,
                    )
                )

            self._last_tip = tip_height
            result = {
                "status": "ok",
                "tip_height": tip_height,
                "reorged": [
                    {"key": list(r.event.natural_key), "asset": r.event.asset}
                    for r in reorged
                ],
                "affected": [
                    {"key": list(r.event.natural_key),
                     "asset": r.event.asset, "status": r.status.value}
                    for r in affected
                ],
            }
            log.info(
                "scan ok tip=%d events=%d reorged=%d confirmed_hits=%d",
                tip_height, len(ordered_events), len(reorged), len(affected),
            )
            return result
        finally:
            self._limiter.release()
