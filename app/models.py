"""领域模型：钱包、链上事件、账本条目与余额视图。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventKind(str, Enum):
    DEPOSIT = "deposit"       # 入账（充值）
    OUT = "out"               # 转出（提现）


class EventStatus(str, Enum):
    PENDING = "pending"       # 已收录，未达确认数
    CONFIRMED = "confirmed"   # 达到确认数，计入可用
    REVERTED = "reverted"     # 被重组回退
    FINALIZED = "finalized"   # 达到最终性，不可回退


@dataclass
class Wallet:
    wallet_id: str
    owner_principal: str
    address: str
    created_at: float = 0.0


@dataclass
class ChainEvent:
    event_id: str
    address: str
    kind: EventKind
    amount: int
    block_height: int
    tx_hash: str
    seen_at: float = 0.0
    status: EventStatus = EventStatus.PENDING
    reverted_by: str | None = None
    reconfirms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LedgerEntry:
    """追加式账本条目（immutable append-only）。"""
    entry_id: str
    wallet_id: str
    amount: int
    kind: str
    ref_event_id: str | None
    ref_request_id: str | None
    created_at: float
    balance_after: int = 0


@dataclass
class BalanceView:
    wallet_id: str
    available: int          # 可用余额
    pending_in: int         # 未达确认数的入账
    holds: int              # 提现冻结
    clawback_due: int       # 已确认入账被回退但余额不足的挂账
    currency: str = "CENT"

    def as_dict(self) -> dict[str, Any]:
        return {
            "wallet_id": self.wallet_id,
            "currency": self.currency,
            "available": self.available,
            "pending_in": self.pending_in,
            "holds": self.holds,
            "clawback_due": self.clawback_due,
            # 恒等式（每钱包资金账实一致）：
            # available + holds + pending_in - clawback_due == 净本金(累计充值-累计提现/回退)
            "identity_check": self.available + self.holds
            + self.pending_in - self.clawback_due,
        }
