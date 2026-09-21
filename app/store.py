"""线程安全的内存存储。

所有可变状态都由同一把 RLock 保护；LedgerService / IngestionService 的
"读-判定-写"序列必须在这把锁内完成，从而保证同一钱包并发访问下
不出现丢失更新、重复入账、负余额或半更新状态。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from app.models import ChainEvent, LedgerEntry, Wallet


@dataclass
class IdempotencyRecord:
    request_id: str
    scope: str               # 作用域，如 "wallet:{id}:withdraw"
    fingerprint: str         # 请求语义指纹
    status: str              # "completed" | "in_flight"
    response: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0


@dataclass
class DeadLetter:
    raw: Any
    reason_code: str
    reason: str
    at: float


@dataclass
class RateBucket:
    tokens: float
    updated_at: float


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.wallets: dict[str, Wallet] = {}
        self.address_index: dict[str, str] = {}       # address -> wallet_id
        self.events: dict[str, ChainEvent] = {}       # event_id -> event
        self.event_at_address: dict[str, list[str]] = {}  # address -> [event_id]
        self.entries: list[LedgerEntry] = []
        self.requests: dict[str, IdempotencyRecord] = {}
        self.dead_letters: list[DeadLetter] = []
        self.chain_height: int = 0
        self.scan_cursor: str | None = None
        self.scan_bucket: RateBucket = RateBucket(tokens=0.0, updated_at=0.0)
        self.clock_offset: float = 0.0  # 测试用时钟偏移

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def now(self) -> float:
        return time.time() + self.clock_offset

    # ---- wallet -----------------------------------------------------------
    def put_wallet(self, wallet: Wallet) -> None:
        with self._lock:
            self.wallets[wallet.wallet_id] = wallet
            self.address_index[wallet.address] = wallet.wallet_id

    def get_wallet(self, wallet_id: str) -> Wallet | None:
        with self._lock:
            return self.wallets.get(wallet_id)

    def wallet_by_address(self, address: str) -> Wallet | None:
        with self._lock:
            wid = self.address_index.get(address)
            return self.wallets[wid] if wid else None

    def owner_of(self, wallet_id: str) -> str | None:
        """属主查询；不存在返回 None（供权限层使用）。"""
        with self._lock:
            w = self.wallets.get(wallet_id)
            return w.owner_principal if w else None

    # ---- events -----------------------------------------------------------
    def put_event(self, event: ChainEvent) -> bool:
        """收录事件；event_id 重复返回 False（不覆盖）。"""
        with self._lock:
            if event.event_id in self.events:
                return False
            self.events[event.event_id] = event
            self.event_at_address.setdefault(event.address, []).append(event.event_id)
            return True

    def get_event(self, event_id: str) -> ChainEvent | None:
        with self._lock:
            return self.events.get(event_id)

    def list_events_by_address(self, address: str) -> list[ChainEvent]:
        with self._lock:
            ids = self.event_at_address.get(address, [])
            return [self.events[i] for i in ids]

    def all_events(self) -> list[ChainEvent]:
        with self._lock:
            return list(self.events.values())

    # ---- ledger -----------------------------------------------------------
    def append_entry(self, entry: LedgerEntry) -> None:
        with self._lock:
            self.entries.append(entry)

    def entries_for_wallet(self, wallet_id: str) -> list[LedgerEntry]:
        with self._lock:
            return [e for e in self.entries if e.wallet_id == wallet_id]

    # ---- idempotency ------------------------------------------------------
    def get_request(self, request_id: str) -> IdempotencyRecord | None:
        with self._lock:
            return self.requests.get(request_id)

    def put_request_if_absent(
        self, request_id: str, scope: str, fingerprint: str
    ) -> IdempotencyRecord | None:
        """原子占位。已存在返回已有记录；占位成功返回 None。"""
        with self._lock:
            existing = self.requests.get(request_id)
            if existing is not None:
                return existing
            rec = IdempotencyRecord(
                request_id=request_id, scope=scope, fingerprint=fingerprint,
                status="in_flight", created_at=self.now(),
            )
            self.requests[request_id] = rec
            return None

    def complete_request(self, request_id: str, response: dict[str, Any]) -> None:
        with self._lock:
            rec = self.requests.get(request_id)
            if rec is not None:
                rec.status = "completed"
                rec.response = response

    def fail_request(self, request_id: str) -> None:
        """业务失败（如余额不足）时释放占位，允许同一 request_id 稍后重试。
        语义冲突/重复提交不调用此方法。"""
        with self._lock:
            rec = self.requests.get(request_id)
            if rec is not None and rec.status == "in_flight":
                del self.requests[request_id]

    # ---- dead letter ------------------------------------------------------
    def add_dead_letter(self, raw: Any, reason_code: str, reason: str) -> None:
        with self._lock:
            self.dead_letters.append(
                DeadLetter(raw=raw, reason_code=reason_code, reason=reason, at=self.now())
            )

    # ---- chain cursor -----------------------------------------------------
    def set_chain_height(self, height: int) -> None:
        with self._lock:
            self.chain_height = height

    def set_scan_cursor(self, cursor: str | None) -> None:
        with self._lock:
            self.scan_cursor = cursor

    def reset(self) -> None:
        """测试辅助：清空全部状态。"""
        with self._lock:
            self.wallets.clear()
            self.address_index.clear()
            self.events.clear()
            self.event_at_address.clear()
            self.entries.clear()
            self.requests.clear()
            self.dead_letters.clear()
            self.chain_height = 0
            self.scan_cursor = None
            self.scan_bucket = RateBucket(tokens=0.0, updated_at=0.0)
            self.clock_offset = 0.0
