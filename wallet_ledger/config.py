"""服务配置（确认数、最终性深度、限流参数）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    required_confirmations: int = 3
    finalization_depth: int = 64
    rate_per_sec: float = 100.0
    burst: int = 200
    max_inflight: int = 8


settings = Settings()
