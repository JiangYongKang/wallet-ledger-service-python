"""领域类型：金额一律以最小货币单位的非负整数表示（避免浮点误差）。

确认数语义：
- 事件在 ``confirmations >= required`` 后达到最终性（CONFIRMED），金额计入
  ``confirmed_balance``（可用余额）。
- 未达到确认数的充值记为 PENDING，只计入 ``pending_in``，与可用余额分开。
- REORGED 表示事件被链重组回撤：若金额已计入可用余额则扣回（不足部分计入
  reorg_debt，资金平账到系统损失汇账户），不得使余额为负。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

# ---- 枚举 ----


class ChainEventType(str, enum.Enum):
    DEPOSIT = "deposit"  # 入账：address 收到 amount
    WITHDRAWAL = "withdrawal"  # 链上转出事件：address 支出 amount


class EventStatus(str, enum.Enum):
    PENDING = "pending"  # 已观察、未达最终性
    CONFIRMED = "confirmed"  # 达到约定确认数，已计入可用余额
    REORGED = "reorged"  # 被重组回撤
    FINALIZED = "finalized"  # 超过最终性深度，永不可回撤


class LedgerTxnType(str, enum.Enum):
    DEPOSIT_PENDING = "deposit_pending"  # 入账登记（资金来源=链）
    DEPOSIT_CONFIRM = "deposit_confirm"  # 确认：待入账 -> 可用
    REORG_CLAWBACK = "reorg_clawback"  # 重组回撤
    TRANSFER_OUT = "transfer_out"  # 钱包内转出（即时扣可用）
    TRANSFER_IN = "transfer_in"  # 钱包内转入
    WITHDRAW_OUT = "withdraw_out"  # 提现（即时扣可用）


class PrincipalRole(str, enum.Enum):
    USER = "user"
    INDEXER = "indexer"  # 模拟链事件投喂方
    ADMIN = "admin"


# ---- 链事件（外部输入 -> 归一化后的领域对象）----


@dataclass(frozen=True, slots=True)
class RawChainEvent:
    """来自模拟链的原始事件（字段可能缺失/乱序/重复/非法）。"""

    tx_hash: object = None
    log_index: object = None
    type: object = None
    address: object = None
    amount: object = None
    block_height: object = None


@dataclass(frozen=True, slots=True)
class ChainEvent:
    """归一化、校验通过后的链事件。"""

    tx_hash: str
    log_index: int
    type: ChainEventType
    address: str
    amount: int  # 最小货币单位，正整数
    block_height: int

    @property
    def natural_key(self) -> tuple[str, int]:
        return self.tx_hash, self.log_index


# ---- 账本状态 ----


@dataclass(slots=True)
class ChainEventRecord:
    """事件在账本中的处理状态。"""

    event: ChainEvent
    status: EventStatus = EventStatus.PENDING
    seen_height: int = 0  # 投喂时事件所在区块高度
    confirmations: int = 0  # 最近一次观察到的确认数
    confirmed: bool = False  # 是否已经把确认入账（幂等闸门）
    reorged: bool = False  # 是否已经回撤（幂等闸门）
    finalized: bool = False


@dataclass(slots=True)
class LedgerEntry:
    """一笔资金流水（只追加，不改写）。"""

    seq: int
    txn_type: LedgerTxnType
    address: str
    amount: int
    ref_key: str  # 关联事件键或幂等键
    memo: str = ""


@dataclass(slots=True)
class Balance:
    """钱包余额视图。三个口径分别展示，不互相掩盖。"""

    address: str
    available: int = 0  # 可用余额：已最终性入账减去转出/提现/回撤扣回
    pending_in: int = 0  # 未达确认数的入账金额
    pending_out: int = 0  # 保留：尚未广播的链上提现在途（本期为 0）
    reorg_debt: int = 0  # 回撤时可用不足而平账到损失汇账户的累计额

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "available": self.available,
            "pending_in": self.pending_in,
            "pending_out": self.pending_out,
            "reorg_debt": self.reorg_debt,
        }


# ---- 调用方身份与幂等记录 ----


@dataclass(frozen=True, slots=True)
class Principal:
    principal_id: str
    role: PrincipalRole
    # 该主体可访问的钱包地址集合；资源访问超出集合 => 统一 404
    wallets: frozenset[str] = field(default_factory=frozenset)


@dataclass(slots=True)
class IdempotencyRecord:
    key: str
    request_fingerprint: str
    status_code: int
    body: dict
