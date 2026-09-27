"""内存仓储：账本状态容器 + 并发原语。

设计要点：
- 余额按 ``(钱包地址, 资产)`` 维度存放，不同资产互不串账；缺省资产为
  ``DEFAULT_ASSET``，不带资产信息的旧用法落到默认资产。
- 每个钱包地址一把重入锁（``RLock``），同一钱包的所有写操作在 service 层
  持锁串行化，杜绝丢失更新、重复入账与半更新；不同资产的操作在同一钱包锁下
  串行但互不改写对方的资产桶。
- 余额与流水在同一临界区更新，维持资金守恒；流水只追加不改写。
- 进程内实现，所有结果可确定性复现，适合单测。
"""

from __future__ import annotations

import threading
from collections import defaultdict

from .domain import (
    DEFAULT_ASSET,
    Balance,
    ChainEventRecord,
    IdempotencyRecord,
    LedgerEntry,
    LedgerTxnType,
)


class LedgerRepository:
    def __init__(self) -> None:
        # address -> asset -> Balance（资产桶惰性创建）
        self._balances: dict[str, dict[str, Balance]] = {}
        self._events: dict[tuple[str, int], ChainEventRecord] = {}
        self._events_by_address: dict[str, set[tuple[str, int]]] = defaultdict(set)
        self._entries: list[LedgerEntry] = []
        self._seq = 0
        self._wallet_owners: dict[str, str] = {}
        self._idempotency: dict[str, IdempotencyRecord] = {}
        self._wallet_locks: dict[str, threading.RLock] = {}
        self._meta_lock = threading.RLock()

    # ---- 钱包 / 锁 ----

    def ensure_wallet(self, address: str, owner_id: str) -> Balance:
        """幂等创建钱包：已存在时不改变归属。返回默认资产余额（兼容旧用法）。"""
        with self._meta_lock:
            assets = self._balances.get(address)
            if assets is None:
                assets = {}
                self._balances[address] = assets
                self._wallet_owners[address] = owner_id
            return self._ensure_balance_locked(address, DEFAULT_ASSET)

    def _ensure_balance_locked(self, address: str, asset: str) -> Balance:
        assets = self._balances.setdefault(address, {})
        bal = assets.get(asset)
        if bal is None:
            bal = Balance(address=address, asset=asset)
            assets[asset] = bal
        return bal

    def ensure_balance(self, address: str, asset: str = DEFAULT_ASSET) -> Balance:
        """确保 ``(address, asset)`` 资产桶存在并返回（调用方须持钱包锁）。"""
        with self._meta_lock:
            return self._ensure_balance_locked(address, asset)

    def wallet_exists(self, address: str) -> bool:
        return address in self._balances

    def get_wallet_owner(self, address: str) -> str | None:
        return self._wallet_owners.get(address)

    def get_balance(
        self, address: str, asset: str = DEFAULT_ASSET
    ) -> Balance | None:
        """钱包或该资产桶不存在时返回 None。"""
        assets = self._balances.get(address)
        if assets is None:
            return None
        return assets.get(asset)

    def get_asset_balances(self, address: str) -> dict[str, Balance]:
        """钱包下全部资产桶（无钱包时为空 dict）。"""
        return dict(self._balances.get(address, {}))

    def lock_for(self, address: str) -> threading.RLock:
        # 锁字典在 meta_lock 保护下惰性创建，保证同一地址拿到同一把锁。
        with self._meta_lock:
            lock = self._wallet_locks.get(address)
            if lock is None:
                lock = threading.RLock()
                self._wallet_locks[address] = lock
            return lock

    # ---- 事件 ----

    def get_event(self, key: tuple[str, int]) -> ChainEventRecord | None:
        return self._events.get(key)

    def put_event_if_absent(self, record: ChainEventRecord) -> ChainEventRecord:
        """仅当自然键不存在时写入。返回最终存放的记录。"""
        with self._meta_lock:
            existing = self._events.get(record.event.natural_key)
            if existing is not None:
                return existing
            self._events[record.event.natural_key] = record
            self._events_by_address[record.event.address].add(
                record.event.natural_key
            )
            return record

    def all_event_records(self) -> list[ChainEventRecord]:
        return list(self._events.values())

    # ---- 流水 ----

    def append_entry(
        self,
        txn_type: LedgerTxnType,
        address: str,
        amount: int,
        ref_key: str,
        memo: str = "",
        asset: str = DEFAULT_ASSET,
    ) -> LedgerEntry:
        with self._meta_lock:
            self._seq += 1
            entry = LedgerEntry(
                seq=self._seq,
                txn_type=txn_type,
                address=address,
                amount=amount,
                ref_key=ref_key,
                memo=memo,
                asset=asset,
            )
            self._entries.append(entry)
            return entry

    def list_entries(
        self, address: str | None = None, asset: str | None = None
    ) -> list[LedgerEntry]:
        entries = self._entries
        if address is not None:
            entries = [e for e in entries if e.address == address]
        if asset is not None:
            entries = [e for e in entries if e.asset == asset]
        return list(entries)

    # ---- 幂等 ----

    def get_idempotency(self, key: str) -> IdempotencyRecord | None:
        return self._idempotency.get(key)

    def put_idempotency(self, record: IdempotencyRecord) -> None:
        self._idempotency[record.key] = record
