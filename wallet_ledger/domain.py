"""领域类型：金额一律以最小货币单位的非负整数表示（避免浮点误差）。

多资产
------
- 每个钱包地址下按资产（``asset``，归一化为大写币种标识）分别记账；不同资产
  的可用/待入账/回撤平账口径完全独立，不能互相抵扣或串账。
- 旧的、不携带资产信息的请求一律按 ``DEFAULT_ASSET`` 处理，结论与单资产时代一致。

确认数语义：
- 事件在 ``confirmations >= required`` 后达到最终性（CONFIRMED），金额计入
  对应资产的 ``available``（可用余额）。
- 未达到确认数的充值记为 PENDING，只计入该资产的 ``pending_in``，与可用分开。
- REORGED 表示事件被链重组回撤：若金额已计入可用余额则扣回（不足部分计入该
  资产的 reorg_debt，资金平账到系统损失汇账户），不得使该资产余额为负。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

# 未携带资产信息的旧请求（单资产时代）统一落账到该默认资产，
# 使“不带 asset 的用法”与显式指定资产的新用法完全隔离但互不影响。
DEFAULT_ASSET = "DEFAULT"

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
    asset: object = None  # 缺省 -> DEFAULT_ASSET（兼容单资产投喂）


@dataclass(frozen=True, slots=True)
class ChainEvent:
    """归一化、校验通过后的链事件。"""

    tx_hash: str
    log_index: int
    type: ChainEventType
    address: str
    amount: int  # 最小货币单位，正整数
    block_height: int
    asset: str = DEFAULT_ASSET  # 归一化后的大写资产标识

    @property
    def natural_key(self) -> tuple[str, int]:
        # 自然键仍是链上唯一定位 (tx_hash, log_index)；资产是该事件的固有属性。
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
    asset: str = DEFAULT_ASSET  # 该笔变动所属资产，旧流水等价于默认资产
    memo: str = ""


@dataclass(slots=True)
class AssetBalance:
    """单个钱包下单个资产的分口径余额。各资产互不相抵。"""

    asset: str
    available: int = 0  # 可用余额：已最终性入账减去转出/提现/回撤扣回
    pending_in: int = 0  # 未达确认数的入账金额
    pending_out: int = 0  # 保留：尚未广播的链上提现在途（本期为 0）
    reorg_debt: int = 0  # 回撤时可用不足而平账到损失汇账户的累计额

    def to_dict(self) -> dict:
        return {
            "asset": self.asset,
            "available": self.available,
            "pending_in": self.pending_in,
            "pending_out": self.pending_out,
            "reorg_debt": self.reorg_debt,
        }


@dataclass(slots=True)
class Balance:
    """钱包余额视图：按资产分别持有分口径余额，不互相掩盖。

    ``available`` / ``pending_in`` / ``pending_out`` / ``reorg_debt`` 为
    默认资产（``DEFAULT_ASSET``）的便捷只读视图，保持单资产时代的访问方式。
    """

    address: str
    assets: dict[str, AssetBalance] = field(default_factory=dict)

    def get(self, asset: str = DEFAULT_ASSET) -> AssetBalance:
        """取指定资产口径；从未发生过该资产变动时返回全 0 视图（不落空账）。"""
        bal = self.assets.get(asset)
        if bal is None:
            bal = AssetBalance(asset=asset)
        return bal

    def ensure(self, asset: str = DEFAULT_ASSET) -> AssetBalance:
        """惰性创建并落账该资产口径。"""
        bal = self.assets.get(asset)
        if bal is None:
            bal = AssetBalance(asset=asset)
            self.assets[asset] = bal
        return bal

    @property
    def available(self) -> int:
        return self.get().available

    @property
    def pending_in(self) -> int:
        return self.get().pending_in

    @property
    def pending_out(self) -> int:
        return self.get().pending_out

    @property
    def reorg_debt(self) -> int:
        return self.get().reorg_debt

    def to_dict(self, asset: str = DEFAULT_ASSET) -> dict:
        """单资产口径视图（默认资产时保持与单资产时代完全相同的字段形状）。"""
        bal = self.get(asset)
        data = {
            "address": self.address,
            "available": bal.available,
            "pending_in": bal.pending_in,
            "pending_out": bal.pending_out,
            "reorg_debt": bal.reorg_debt,
        }
        if asset != DEFAULT_ASSET:
            data["asset"] = asset
        return data

    def assets_to_dict(self) -> dict:
        """一次看清该钱包下每个资产各自的分口径余额。"""
        return {asset: bal.to_dict() for asset, bal in sorted(self.assets.items())}


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
