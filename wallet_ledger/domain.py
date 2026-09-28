"""领域类型：金额一律以最小货币单位的非负整数表示（避免浮点误差）。

多资产语义：
- 账本按 ``(钱包地址, 资产)`` 维度记账，不同资产之间不互相抵扣、不串账。
- 请求不带资产信息时按 ``DEFAULT_ASSET`` 处理，兼容旧的单资产用法。

确认数语义：
- 事件在 ``confirmations >= required`` 后达到最终性（CONFIRMED），金额计入
  ``confirmed_balance``（可用余额）。
- 未达到确认数的充值记为 PENDING，只计入 ``pending_in``，与可用余额分开。
- REORGED 表示事件被链重组回撤：
  充值事件若金额已计入可用余额则扣回（不足部分计入 reorg_debt，资金平账到
  系统损失汇账户），不得使余额为负；
  链上转出事件则恢复到转出发生之前——只退回当初真正从可用余额扣掉的部分
  （``debited_amount``），当初因可用不足而记下的缺口（``shortfall_amount``）
  一并从 reorg_debt 冲销，既不负余额，也不虚增可用。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

#: 未携带资产信息的请求落到的默认资产（兼容单资产用法）。
DEFAULT_ASSET = "COIN"

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
    REORG_SHORTFALL_REVERSAL = "reorg_shortfall_reversal"  # 回撤时冲销损失汇缺口
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
    asset: object = None  # 缺省时按 DEFAULT_ASSET 处理


@dataclass(frozen=True, slots=True)
class ChainEvent:
    """归一化、校验通过后的链事件。"""

    tx_hash: str
    log_index: int
    type: ChainEventType
    address: str
    amount: int  # 最小货币单位，正整数
    block_height: int
    asset: str = DEFAULT_ASSET

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
    #: 链上转出事件发生时，真正从可用余额扣掉的金额（余额被钳到 0 前的部分）。
    debited_amount: int = 0
    #: 链上转出事件发生时，因可用不足而平账到 reorg_debt 的缺口。
    shortfall_amount: int = 0


@dataclass(slots=True)
class LedgerEntry:
    """一笔资金流水（只追加不改写）。"""

    seq: int
    txn_type: LedgerTxnType
    address: str
    amount: int
    ref_key: str  # 关联事件键或幂等键
    memo: str = ""
    asset: str = DEFAULT_ASSET


@dataclass(slots=True)
class Balance:
    """单个 ``(钱包, 资产)`` 的余额视图。三个口径分别展示，不互相掩盖。"""

    address: str
    asset: str = DEFAULT_ASSET
    available: int = 0  # 可用余额：已最终性入账减去转出/提现/回撤扣回
    pending_in: int = 0  # 未达确认数的入账金额
    pending_out: int = 0  # 保留：尚未广播的链上提现在途（本期为 0）
    reorg_debt: int = 0  # 回撤时可用不足而平账到损失汇账户的累计额

    def to_dict(self) -> dict:
        # 保持旧版五键结构不变（单资产兼容）；资产维度由 WalletBalance 视图给出。
        return {
            "address": self.address,
            "available": self.available,
            "pending_in": self.pending_in,
            "pending_out": self.pending_out,
            "reorg_debt": self.reorg_debt,
        }


@dataclass(slots=True)
class WalletBalance:
    """一个钱包下全部资产的余额汇总视图（一次看清每个资产各自口径）。"""

    address: str
    assets: dict[str, Balance] = field(default_factory=dict)

    def to_dict(self) -> dict:
        # 顶层字段保持旧版结构（默认资产口径），``assets`` 给出每个资产的分口径。
        default = self.assets.get(DEFAULT_ASSET, Balance(address=self.address))
        return {
            **default.to_dict(),
            "assets": {asset: bal.to_dict() for asset, bal in sorted(self.assets.items())},
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
