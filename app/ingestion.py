"""模拟链事件摄入：归一化校验、批量确认扫描与限流。

归一化规则（错误不静默丢弃、不透出底层异常，全部进死信并给出可区分 reason_code）：
- missing_field     必填字段缺失或为空
- invalid_kind      事件类型不是 deposit/out
- invalid_amount    金额缺失、非数字、<=0、NaN/Inf
- invalid_height    区块高度缺失或非正整数
- invalid_tx        交易哈希缺失或非字符串
- future_block      事件高度超过当前链高度（乱序），拒绝收录
- unknown_address   地址未绑定任何钱包（记录但不入账）
- duplicate         同 event_id 已收录（幂等跳过，不算错误）
- conflict          同 event_id 但载荷不同（明确拒绝）

扫描限流：令牌桶；令牌不足时返回 rate_limited（429），调用方按策略延迟重试；
扫描是幂等的，重试不会重复入账。
"""
from __future__ import annotations

import math
from typing import Any

from app.config import Settings, get_settings
from app.errors import RateLimited
from app.chain import MockChain
from app.ledger import LedgerService
from app.models import ChainEvent, EventKind
from app.store import Store

REQUIRED_FIELDS = ("event_id", "address", "kind", "amount", "block_height", "tx_hash")
DEAD_REASON_CODES = {
    "missing_field", "invalid_kind", "invalid_amount", "invalid_height",
    "invalid_tx", "future_block", "unknown_address",
}


class IngestionService:
    def __init__(
        self, store: Store, chain: MockChain, ledger: LedgerService,
        settings: Settings | None = None,
    ) -> None:
        self.store = store
        self.chain = chain
        self.ledger = ledger
        self.settings = settings or get_settings()

    # ------------------------------------------------------------ normalize
    def normalize(self, raw: dict[str, Any]) -> ChainEvent:
        """把原始事件归一化为 ChainEvent；非法输入抛 ValidationError 并带 reason_code。"""
        from app.errors import ValidationError

        if not isinstance(raw, dict):
            raise ValidationError("event must be an object", reason="missing_field")

        for f in REQUIRED_FIELDS:
            v = raw.get(f)
            if v is None or (isinstance(v, str) and not v.strip()):
                raise ValidationError(f"missing field: {f}", reason="missing_field")

        kind_raw = str(raw["kind"]).strip().lower()
        try:
            kind = EventKind(kind_raw)
        except ValueError:
            raise ValidationError(
                f"invalid kind: {raw['kind']!r}", reason="invalid_kind"
            )

        amount = self._normalize_amount(raw["amount"])
        height = self._normalize_height(raw["block_height"])

        tx_hash = raw["tx_hash"]
        if not isinstance(tx_hash, str) or not tx_hash.strip():
            raise ValidationError("invalid tx_hash", reason="invalid_tx")
        address = str(raw["address"]).strip()
        event_id = str(raw["event_id"]).strip()

        if height > self.chain.height():
            raise ValidationError(
                f"event block_height {height} is ahead of chain height "
                f"{self.chain.height()}",
                reason="future_block",
            )

        return ChainEvent(
            event_id=event_id,
            address=address,
            kind=kind,
            amount=amount,
            block_height=height,
            tx_hash=tx_hash.strip(),
            seen_at=self.store.now(),
            raw=raw,
        )

    @staticmethod
    def _normalize_amount(v: Any) -> int:
        from app.errors import ValidationError

        if isinstance(v, bool):
            raise ValidationError("invalid amount", reason="invalid_amount")
        if isinstance(v, int):
            amount = v
        elif isinstance(v, float):
            if math.isnan(v) or math.isinf(v) or not v.is_integer():
                raise ValidationError("invalid amount", reason="invalid_amount")
            amount = int(v)
        elif isinstance(v, str):
            s = v.strip()
            try:
                # 允许 "100" / "100.0"，拒绝小数与非数字
                f = float(s)
                if math.isnan(f) or math.isinf(f) or not f.is_integer():
                    raise ValueError
                amount = int(f)
            except ValueError:
                raise ValidationError("invalid amount", reason="invalid_amount")
        else:
            raise ValidationError("invalid amount", reason="invalid_amount")
        if amount <= 0:
            raise ValidationError("amount must be positive", reason="invalid_amount")
        return amount

    @staticmethod
    def _normalize_height(v: Any) -> int:
        from app.errors import ValidationError

        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise ValidationError("invalid block_height", reason="invalid_height")
        return v

    # -------------------------------------------------------- admin ingest
    def ingest_raw(self, raw_events: list[dict[str, Any]]) -> dict[str, Any]:
        """管理员直接投喂原始事件：归一化 -> 收录。失败进死信，不静默丢弃。"""
        accepted: list[str] = []
        duplicates: list[str] = []
        conflicts: list[str] = []
        rejected: list[dict[str, str]] = []
        unknown: list[str] = []

        with self.store.lock:
            chain_height = self.chain.height()
            for raw in raw_events:
                if not isinstance(raw, dict):
                    self.store.add_dead_letter(
                        raw, "missing_field", "event must be an object")
                    rejected.append(
                        {"event_id": None, "reason": "missing_field"})
                    continue
                try:
                    event = self.normalize(raw)
                except Exception as ex:  # 归一化错误统一收口，不外泄底层异常
                    code = getattr(ex, "reason", None) or "invalid_request"
                    self.store.add_dead_letter(raw, code, str(ex))
                    rejected.append({"event_id": str(raw.get("event_id")), "reason": code})
                    continue
                if event.block_height > chain_height:
                    self.store.add_dead_letter(raw, "future_block", "height ahead")
                    rejected.append({"event_id": event.event_id, "reason": "future_block"})
                    continue
                if self.store.wallet_by_address(event.address) is None:
                    # 未绑定地址：死信留痕（不静默丢弃），不入账
                    self.store.add_dead_letter(
                        raw, "unknown_address", f"address {event.address} not bound"
                    )
                    unknown.append(event.event_id)
                    rejected.append(
                        {"event_id": event.event_id, "reason": "unknown_address"}
                    )
                    continue
                try:
                    outcome = self.ledger.ingest_event(event)
                except Exception as ex:
                    code = getattr(ex, "code", "conflict_state")
                    self.store.add_dead_letter(raw, code, str(ex))
                    conflicts.append(event.event_id)
                    rejected.append({"event_id": event.event_id, "reason": code})
                    continue
                if outcome == "stored":
                    accepted.append(event.event_id)
                else:
                    duplicates.append(event.event_id)

            # 收录后立即按当前高度尝试一次确认推进（与扫描入口结论一致）
            self.ledger.confirm_events(chain_height)

        return {
            "accepted": sorted(accepted),
            "duplicates": sorted(duplicates),
            "unknown_address": sorted(unknown),
            "rejected": rejected,
            "chain_height": chain_height,
        }

    # ------------------------------------------------------- scan + throttle
    def _take_token(self, cost: int = 1) -> None:
        """令牌桶：容量/补充速率来自配置；桶内时间按服务时钟推进。"""
        cap = self.settings.rate_limit_capacity
        refill = self.settings.rate_limit_refill_per_sec
        now = self.store.now()
        bucket = self.store.scan_bucket
        if bucket.updated_at == 0.0:
            bucket.tokens = float(cap)
            bucket.updated_at = now
        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(float(cap), bucket.tokens + elapsed * refill)
        bucket.updated_at = now
        if bucket.tokens < cost:
            retry_after = (cost - bucket.tokens) / refill if refill > 0 else 1.0
            raise RateLimited(
                "scan rate limit exceeded",
                details={"retry_after_ms": int(retry_after * 1000)},
            )
        bucket.tokens -= cost

    def scan_once(self, max_events: int | None = None) -> dict[str, Any]:
        """从模拟链游标拉取一批事件 -> 归一化收录 -> 按当前高度推进确认。

        - 全流程在存储锁内完成，与回退/并发扫描串行；
        - 限流不足抛 rate_limited，不推进游标（重试安全、幂等）；
        - 单批最多 scan_max_events，避免无界占用资源。
        """
        limit = min(max_events or self.settings.scan_max_events,
                    self.settings.scan_max_events)
        with self.store.lock:
            self._take_token()
            current_height = self.chain.height()
            raws, next_cursor = self.chain.events_since(self.store.scan_cursor, limit)

            accepted: list[str] = []
            duplicates: list[str] = []
            rejected: list[dict[str, str]] = []
            for raw in raws:
                if not isinstance(raw, dict):
                    self.store.add_dead_letter(
                        raw, "missing_field", "event must be an object")
                    rejected.append({"event_id": None, "reason": "missing_field"})
                    continue
                try:
                    event = self.normalize(raw)
                except Exception as ex:
                    code = getattr(ex, "reason", None) or "invalid_request"
                    self.store.add_dead_letter(raw, code, str(ex))
                    rejected.append({"event_id": str(raw.get("event_id")), "reason": code})
                    continue
                if self.store.wallet_by_address(event.address) is None:
                    self.store.add_dead_letter(raw, "unknown_address", "address not bound")
                    rejected.append(
                        {"event_id": event.event_id, "reason": "unknown_address"}
                    )
                    continue
                try:
                    outcome = self.ledger.ingest_event(event)
                except Exception as ex:
                    code = getattr(ex, "code", "conflict_state")
                    self.store.add_dead_letter(raw, code, str(ex))
                    rejected.append({"event_id": event.event_id, "reason": code})
                    continue
                (accepted if outcome == "stored" else duplicates).append(event.event_id)

            confirmation = self.ledger.confirm_events(current_height)
            # 仅当整批处理完成后才推进游标（失败可安全重放）
            self.store.set_scan_cursor(next_cursor)
            self.store.set_chain_height(current_height)

            return {
                "fetched": len(raws),
                "accepted": sorted(accepted),
                "duplicates": sorted(duplicates),
                "rejected": rejected,
                "confirmed": confirmation["confirmed"],
                "finalized": confirmation["finalized"],
                "chain_height": current_height,
                "next_cursor": next_cursor,
            }
