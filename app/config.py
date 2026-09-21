"""全局配置：确认数、最终性深度、限流、货币单位。

确认与最终性规则（统一适用于所有入口）：
- 事件在 block_height 处被收录；当 chain_height - block_height + 1 >= confirmations_required
  时由 PENDING 转为 CONFIRMED，金额才计入可用余额（入账）或实际扣款（转出）。
- 当 chain_height - block_height + 1 > finality_depth（即区块埋深 >= finality_depth）时
  转为 FINALIZED，此后不接受重组回退；回退已最终化区块返回 finalized_reorg。
- 重组只能作用于未最终化的区块；未达确认数的事件回退只移除内存中的 PENDING 投影，
  已确认事件回退执行追加式冲账，结论见 ledger 模块。
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    confirmations_required: int = 3
    finality_depth: int = 6
    rate_limit_capacity: int = 10        # 扫描令牌桶容量
    rate_limit_refill_per_sec: float = 5.0
    scan_max_events: int = 100
    currency: str = "CENT"               # 最小货币单位，金额一律用整数表示


_cached: Settings | None = None


def get_settings() -> Settings:
    global _cached
    if _cached is None:
        def _int(name: str, default: int) -> int:
            raw = os.environ.get(name)
            return int(raw) if raw not in (None, "") else default

        def _float(name: str, default: float) -> float:
            raw = os.environ.get(name)
            return float(raw) if raw not in (None, "") else default

        _cached = Settings(
            confirmations_required=_int("CONFIRMATIONS_REQUIRED", 3),
            finality_depth=_int("FINALITY_DEPTH", 6),
            rate_limit_capacity=_int("RATE_LIMIT_CAPACITY", 10),
            rate_limit_refill_per_sec=_float("RATE_LIMIT_REFILL_PER_SEC", 5.0),
            scan_max_events=_int("SCAN_MAX_EVENTS", 100),
        )
    return _cached


def reset_settings_cache() -> None:
    """测试辅助：清空缓存以读取新环境变量。"""
    global _cached
    _cached = None
