"""账本核心服务。

并发模型：所有公开方法都在 store 的同一把 RLock 内执行"读-判定-写"，
对同一钱包的并发入账/转出/确认/回退严格串行，杜绝丢失更新、重复入账、
负余额与半更新状态。

追加式账本（LedgerEntry.kind）：
  in_deposit          链上充值达到确认数（available += amount）
  out_confirm         链上转出事件达到确认数（记账核对，内部操作已先行扣款）
  clawback            已确认充值被重组，从可用余额实扣部分（available -= amount）
  clawback_shortfall  回退时可用不足、无法实扣的挂账（available 不变，clawback_due += amount）
  clawback_repay      后续入账资金优先冲抵挂账（available 不变，clawback_due -= amount）
  transfer_out        内部转账扣款（available -= amount）
  transfer_in         内部转转入账（available += amount）
  withdrawal_hold     提现冻结（available -= amount, holds += amount）
  withdrawal_settle   提现结算出库（holds -= amount，资金离开系统）
  withdrawal_release  提现撤销解冻（holds -= amount, available += amount）
  out_revert          链上转出事件被回退的中性核对条目

余额投影（从事件状态聚合，pending 与 available 严格区分）：
  pending_in = Σ PENDING 的 deposit 金额
  available = Σ 账本正向条目 - Σ 负向条目（见上）
  holds      = Σ 冻结未结算
  clawback_due = Σ 回退挂账 - Σ 已冲抵
"""
from __future__ import annotations

import uuid
from typing import Any

from app.config import Settings, get_settings
from app.errors import (
    ConflictState,
    FinalizedReorg,
    InsufficientFunds,
    NotFound,
    ValidationError,
)
from app.models import (
    BalanceView,
    ChainEvent,
    EventKind,
    EventStatus,
    LedgerEntry,
    Wallet,
)
from app.store import Store

ENTRY_KIN_POSITIVE = {
    "in_deposit", "transfer_in", "withdrawal_release",
}
ENTRY_KIN_NEGATIVE = {
    "transfer_out", "withdrawal_hold", "clawback",
}
ENTRY_KIN_NEUTRAL = {
    "out_confirm", "clawback_shortfall", "clawback_repay",
    "withdrawal_settle", "out_revert",
}


class LedgerService:
    def __init__(self, store: Store, settings: Settings | None = None) -> None:
        self.store = store
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------ util
    def _new_entry(
        self, wallet_id: str, amount: int, kind: str,
        ref_event_id: str | None = None, ref_request_id: str | None = None,
    ) -> LedgerEntry:
        return LedgerEntry(
            entry_id=f"en_{uuid.uuid4().hex[:16]}",
            wallet_id=wallet_id,
            amount=amount,
            kind=kind,
            ref_event_id=ref_event_id,
            ref_request_id=ref_request_id,
            created_at=self.store.now(),
        )

    def _emit(self, entry: LedgerEntry) -> None:
        prev = self._snapshot(entry.wallet_id)
        delta = entry.amount if entry.kind in ENTRY_KIN_POSITIVE else (
            -entry.amount if entry.kind in ENTRY_KIN_NEGATIVE else 0
        )
        entry.balance_after = prev + delta
        self.store.append_entry(entry)

    def _snapshot(self, wallet_id: str) -> int:
        total = 0
        for e in self.store.entries_for_wallet(wallet_id):
            if e.kind in ENTRY_KIN_POSITIVE:
                total += e.amount
            elif e.kind in ENTRY_KIN_NEGATIVE:
                total -= e.amount
        return total

    def _require_wallet(self, wallet_id: str) -> Wallet:
        w = self.store.get_wallet(wallet_id)
        if w is None:
            raise NotFound("wallet not found")
        return w

    # --------------------------------------------------------------- wallets
    def create_wallet(self, wallet_id: str, principal_id: str, address: str) -> Wallet:
        with self.store.lock:
            if self.store.get_wallet(wallet_id) is not None:
                raise ConflictState("wallet already exists")
            if self.store.wallet_by_address(address) is not None:
                raise ConflictState("address already bound to a wallet")
            wallet = Wallet(
                wallet_id=wallet_id,
                owner_principal=principal_id,
                address=address,
                created_at=self.store.now(),
            )
            self.store.put_wallet(wallet)
            return wallet

    def get_balance(self, wallet_id: str) -> BalanceView:
        with self.store.lock:
            self._require_wallet(wallet_id)
            return self._project(wallet_id)

    def deposit_address(self, wallet_id: str) -> str:
        with self.store.lock:
            return self._require_wallet(wallet_id).address

    # -------------------------------------------------------------- projection
    def _project(self, wallet_id: str) -> BalanceView:
        wallet = self.store.get_wallet(wallet_id)
        assert wallet is not None
        available = self._snapshot(wallet_id)

        pending_in = 0
        for ev in self.store.list_events_by_address(wallet.address):
            if ev.kind is EventKind.DEPOSIT and ev.status is EventStatus.PENDING:
                pending_in += ev.amount

        holds = 0
        clawback_due = 0
        for e in self.store.entries_for_wallet(wallet_id):
            if e.kind == "withdrawal_hold":
                holds += e.amount
            elif e.kind == "withdrawal_settle" or e.kind == "withdrawal_release":
                holds -= e.amount
            elif e.kind == "clawback_shortfall":
                clawback_due += e.amount
            elif e.kind == "clawback_repay":
                clawback_due -= e.amount
        return BalanceView(
            wallet_id=wallet_id,
            available=available,
            pending_in=pending_in,
            holds=holds,
            clawback_due=clawback_due,
            currency=self.settings.currency,
        )

    # --------------------------------------------------------- fund movement
    def _idempotent_begin(self, request_id: str, scope: str, fingerprint: str) -> None:
        """幂等占位：
        - 首次：占位成功；
        - 同 request_id + 同指纹：返回已完成响应（重放）；in_flight 视为并发冲突；
        - 同 request_id + 不同指纹（语义冲突）：409 明确拒绝。
        """
        from app.errors import IdempotencyConflict, IdempotencyReplay

        existing = self.store.put_request_if_absent(request_id, scope, fingerprint)
        if existing is None:
            return
        if existing.fingerprint != fingerprint or existing.scope != scope:
            raise IdempotencyConflict(
                "request_id reused with a different request payload"
            )
        if existing.status == "in_flight":
            raise IdempotencyConflict("request is already being processed")
        replay = dict(existing.response)
        replay["idempotent_replay"] = True
        raise IdempotencyReplay(
            reason="duplicate request", details={"response": replay}
        )

    def _complete(self, request_id: str, response: dict[str, Any]) -> dict[str, Any]:
        self.store.complete_request(request_id, response)
        response = dict(response)
        response["idempotent_replay"] = False
        return response

    @staticmethod
    def _validate_amount(amount: Any) -> int:
        if isinstance(amount, bool):
            raise ValidationError("amount must be a positive integer")
        if isinstance(amount, int):
            value = amount
        elif isinstance(amount, float) and amount.is_integer():
            value = int(amount)
        elif isinstance(amount, str):
            try:
                value = int(amount.strip())
            except ValueError:
                raise ValidationError("amount must be a positive integer")
        else:
            raise ValidationError("amount must be a positive integer")
        if value <= 0:
            raise ValidationError("amount must be a positive integer")
        return value

    def transfer(
        self, wallet_id: str, to_wallet_id: str, amount: int, request_id: str
    ) -> dict[str, Any]:
        amount = self._validate_amount(amount)
        if not request_id or not str(request_id).strip():
            raise ValidationError("request_id is required")
        with self.store.lock:
            src = self._require_wallet(wallet_id)
            dst = self.store.get_wallet(to_wallet_id)
            if dst is None:
                raise NotFound("recipient wallet not found")
            if src.wallet_id == dst.wallet_id:
                raise ValidationError("cannot transfer to the same wallet")
            scope = f"transfer:{src.wallet_id}->{dst.wallet_id}"
            fp = f"{scope}|{amount}"
            self._idempotent_begin(request_id, scope, fp)

            view = self._project(src.wallet_id)
            if view.available < amount:
                self.store.fail_request(request_id)
                raise InsufficientFunds(
                    f"available {view.available} < required {amount}"
                )
            self._emit(self._new_entry(src.wallet_id, amount, "transfer_out",
                                       ref_request_id=request_id))
            self._emit(self._new_entry(dst.wallet_id, amount, "transfer_in",
                                       ref_request_id=request_id))
            result = {
                "status": "completed",
                "request_id": request_id,
                "from": src.wallet_id,
                "to": dst.wallet_id,
                "amount": amount,
                "currency": self.settings.currency,
            }
            return self._complete(request_id, result)

    def withdraw(self, wallet_id: str, amount: int, request_id: str) -> dict[str, Any]:
        amount = self._validate_amount(amount)
        if not request_id or not str(request_id).strip():
            raise ValidationError("request_id is required")
        with self.store.lock:
            wallet = self._require_wallet(wallet_id)
            scope = f"withdraw:{wallet.wallet_id}"
            fp = f"{scope}|{amount}"
            self._idempotent_begin(request_id, scope, fp)

            view = self._project(wallet.wallet_id)
            if view.available < amount:
                self.store.fail_request(request_id)
                raise InsufficientFunds(
                    f"available {view.available} < required {amount}"
                )
            self._emit(self._new_entry(wallet.wallet_id, amount, "withdrawal_hold",
                                       ref_request_id=request_id))
            result = {
                "status": "held",
                "request_id": request_id,
                "wallet_id": wallet.wallet_id,
                "amount": amount,
                "currency": self.settings.currency,
            }
            return self._complete(request_id, result)

    # ------------------------------------------------- withdrawal lifecycle
    def settle_withdrawal(self, request_id: str) -> dict[str, Any]:
        """提现出库：冻结资金离开系统（由链上 out 事件确认触发/管理员触发）。"""
        with self.store.lock:
            rec = self.store.get_request(request_id)
            if rec is None or not rec.scope.startswith("withdraw:"):
                raise NotFound("withdrawal request not found")
            if rec.status != "completed":
                raise ConflictState("withdrawal is not held")
            wallet_id = rec.scope.split(":", 1)[1]
            amount = int(rec.response["amount"])
            # 幂等：同一 request 只结算一次
            if any(
                e.kind == "withdrawal_settle" and e.ref_request_id == request_id
                for e in self.store.entries_for_wallet(wallet_id)
            ):
                return {"status": "settled", "request_id": request_id, "idempotent_replay": True}
            self._emit(self._new_entry(wallet_id, amount, "withdrawal_settle",
                                       ref_request_id=request_id))
            return {"status": "settled", "request_id": request_id, "idempotent_replay": False}

    def release_withdrawal(self, request_id: str) -> dict[str, Any]:
        """提现失败/回退：解冻退回可用余额。"""
        with self.store.lock:
            rec = self.store.get_request(request_id)
            if rec is None or not rec.scope.startswith("withdraw:"):
                raise NotFound("withdrawal request not found")
            wallet_id = rec.scope.split(":", 1)[1]
            amount = int(rec.response["amount"])
            kinds = {(e.kind, e.ref_request_id)
                     for e in self.store.entries_for_wallet(wallet_id)}
            if ("withdrawal_release", request_id) in kinds or \
               ("withdrawal_settle", request_id) in kinds:
                return {"status": "released", "request_id": request_id, "idempotent_replay": True}
            self._emit(self._new_entry(wallet_id, amount, "withdrawal_release",
                                       ref_request_id=request_id))
            return {"status": "released", "request_id": request_id, "idempotent_replay": False}

    # ------------------------------------------------------------- chain events
    def ingest_event(self, event: ChainEvent) -> str:
        """收录已归一化的链上事件。返回 "stored" | "duplicate" |
        "conflict"（同 id 但语义不同）。PENDING 事件立即在余额中以 pending_in 展示。"""
        with self.store.lock:
            existing = self.store.get_event(event.event_id)
            if existing is not None:
                if (existing.address, existing.kind, existing.amount,
                        existing.block_height, existing.tx_hash) != (
                        event.address, event.kind, event.amount,
                        event.block_height, event.tx_hash):
                    raise ConflictState(
                        f"event_id {event.event_id} reused with different payload"
                    )
                return "duplicate"
            self.store.put_event(event)
            return "stored"

    def _depth(self, ev: ChainEvent, current_height: int) -> int:
        return current_height - ev.block_height + 1

    def _status_for(self, ev: ChainEvent, current_height: int) -> EventStatus:
        if ev.status is EventStatus.REVERTED:
            return EventStatus.REVERTED
        depth = self._depth(ev, current_height)
        if depth > self.settings.finality_depth:
            return EventStatus.FINALIZED
        if depth >= self.settings.confirmations_required:
            return EventStatus.CONFIRMED
        return EventStatus.PENDING

    def _apply_confirmation(self, ev: ChainEvent) -> list[str]:
        """PENDING -> CONFIRMED/FINALIZED 的入账/扣款投影。"""
        wallet = self.store.wallet_by_address(ev.address)
        emitted: list[str] = []
        if wallet is None:
            return emitted
        if ev.kind is EventKind.DEPOSIT:
            # 已确认入账先冲抵挂账，剩余进入可用余额；不产生负余额
            remaining = ev.amount
            view = self._project(wallet.wallet_id)
            if view.clawback_due > 0:
                repay = min(view.clawback_due, remaining)
                if repay > 0:
                    entry = self._new_entry(wallet.wallet_id, repay, "clawback_repay",
                                            ref_event_id=ev.event_id)
                    self._emit(entry)
                    emitted.append(entry.entry_id)
                    remaining -= repay
            if remaining > 0:
                entry = self._new_entry(wallet.wallet_id, remaining, "in_deposit",
                                        ref_event_id=ev.event_id)
                self._emit(entry)
                emitted.append(entry.entry_id)
        elif ev.kind is EventKind.OUT:
            # 链上转出事件：内部提现已先行冻结/扣款，这里只追加核对条目，不重复扣款
            entry = self._new_entry(wallet.wallet_id, ev.amount, "out_confirm",
                                    ref_event_id=ev.event_id)
            self._emit(entry)
            emitted.append(entry.entry_id)
        return emitted

    def confirm_events(self, current_height: int) -> dict[str, Any]:
        """按确定序扫描全部事件并推进状态（PENDING -> CONFIRMED -> FINALIZED）。
        与回退并发时：REVERTED 优先（见 _status_for），回退后再确认不会重复入账。"""
        confirmed: list[str] = []
        finalized: list[str] = []
        with self.store.lock:
            ordered = sorted(
                self.store.all_events(),
                key=lambda e: (e.block_height, e.event_id),
            )
            for ev in ordered:
                if ev.status is EventStatus.REVERTED:
                    continue
                target = self._status_for(ev, current_height)
                if target is EventStatus.CONFIRMED and ev.status is EventStatus.PENDING:
                    self._apply_confirmation(ev)
                    ev.status = EventStatus.CONFIRMED
                    confirmed.append(ev.event_id)
                elif target is EventStatus.FINALIZED and ev.status in (
                    EventStatus.PENDING, EventStatus.CONFIRMED,
                ):
                    if ev.status is EventStatus.PENDING:
                        self._apply_confirmation(ev)
                    ev.status = EventStatus.FINALIZED
                    finalized.append(ev.event_id)
        return {"confirmed": confirmed, "finalized": finalized}

    def reorg(self, reverted_event_ids: list[str], current_height: int) -> dict[str, Any]:
        """重组回退。已 FINALIZED 的事件拒绝回退（finalized_reorg）。
        - PENDING 事件：直接标记 REVERTED（从 pending_in 移除，未触碰可用余额）。
        - CONFIRMED 的 deposit：追加 clawback 冲账，可用不足部分挂账，
          保证 available 永不为负且账目恒等。
        - CONFIRMED 的 out：解除其核对作用；若有关联冻结提现则解冻退回。
        回退与确认同时到达：先判最终性，REVERTED 在确认投影之前短路，结论确定可复现。"""
        reverted: list[str] = []
        with self.store.lock:
            targets = [
                self.store.get_event(eid)
                for eid in sorted(set(reverted_event_ids))
            ]
            # 预校验阶段：任何已最终化事件都使整批回退拒绝（原子，无半更新）
            finalized_refused = [
                ev.event_id for ev in targets
                if ev is not None
                and (ev.status is EventStatus.FINALIZED
                     or self._depth(ev, current_height) > self.settings.finality_depth)
            ]
            if finalized_refused:
                raise FinalizedReorg(
                    "refuse to revert finalized events",
                    details={"event_ids": finalized_refused},
                )
            for ev in targets:
                if ev is None or ev.status is EventStatus.REVERTED:
                    continue  # 未知事件忽略；重复回退幂等
                if ev.status is EventStatus.CONFIRMED:
                    wallet = self.store.wallet_by_address(ev.address)
                    if wallet is not None and ev.kind is EventKind.DEPOSIT:
                        # 已存在冲账条目则不重复（幂等保护）
                        already = any(
                            e.kind == "clawback" and e.ref_event_id == ev.event_id
                            for e in self.store.entries_for_wallet(wallet.wallet_id)
                        )
                        if not already:
                            view = self._project(wallet.wallet_id)
                            deduct = min(view.available, ev.amount)
                            if deduct > 0:
                                # 实扣部分：减少可用余额（恒有 deduct <= available，不为负）
                                self._emit(self._new_entry(
                                    wallet.wallet_id, deduct, "clawback",
                                    ref_event_id=ev.event_id))
                            shortfall = ev.amount - deduct
                            if shortfall > 0:
                                # 不足部分：挂账，账目恒等（资金已被转出/提现）
                                self._emit(self._new_entry(
                                    wallet.wallet_id, shortfall, "clawback_shortfall",
                                    ref_event_id=ev.event_id))
                    elif wallet is not None and ev.kind is EventKind.OUT:
                        # out 回退不影响可用余额（提现冻结的解除由 release_withdrawal 处理），
                        # 仅记录中性核对条目
                        self._emit(self._new_entry(
                            wallet.wallet_id, ev.amount, "out_revert",
                            ref_event_id=ev.event_id))
                ev.status = EventStatus.REVERTED
                ev.reverted_by = f"reorg@{current_height}"
                reverted.append(ev.event_id)
        return {"reverted": reverted, "finalized_refused": []}
