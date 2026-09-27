"""内存仓储：账本状态容器 + 并发原语。

设计要点：
- 每个钱包地址一把重入锁（``RLock``），同一钱包的所有写操作在 service 层
  持锁串行化，杜绝丢失更新、重复入账与半更新。
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
        self._balances: dict[str, Balance] = {}
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
        """幂等创建钱包：已存在时不改变归属，直接返回现有余额。"""
        with self._meta_lock:
            bal = self._balances.get(address)
            if bal is None:
                bal = Balance(address=address)
                self._balances[address] = bal
                self._wallet_owners[address] = owner_id
            return bal

    def wallet_exists(self, address: str) -> bool:
        return address in self._balances

    def get_wallet_owner(self, address: str) -> str | None:
        return self._wallet_owners.get(address)

    def get_balance(self, address: str) -> Balance | None:
        return self._balances.get(address)

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
        asset: str = DEFAULT_ASSET,
        memo: str = "",
    ) -> LedgerEntry:
        with self._meta_lock:
            self._seq += 1
            entry = LedgerEntry(
                seq=self._seq,
                txn_type=txn_type,
                address=address,
                amount=amount,
                ref_key=ref_key,
                asset=asset,
                memo=memo,
            )
            self._entries.append(entry)
            return entry

    def list_entries(
        self, address: str | None = None, asset: str | None = None
    ) -> list[LedgerEntry]:
        return [
            e
            for e in self._entries
            if (address is None or e.address == address)
            and (asset is None or e.asset == asset)
        ]

    # ---- 幂等 ----

    def get_idempotency(self, key: str) -> IdempotencyRecord | None:
        return self._idempotency.get(key)

    def put_idempotency(self, record: IdempotencyRecord) -> None:
        self._idempotency[record.key] = record
