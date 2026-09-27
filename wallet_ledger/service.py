"""账本领域服务：资金结论的唯一权威来源。

多资产
------
- 每个钱包地址下按 ``asset`` 分别持有分口径余额；资金变动（确认入账、链上
  提现、转账、回撤平账）一律落到请求/事件指定的资产上，不同资产之间绝不
  互相抵扣或串账。
- 不携带资产的调用按默认资产处理，资金结论与单资产时代完全一致。

并发与一致性
------------
- 每个钱包地址一把 RLock；跨钱包写操作按地址排序后依次加锁，避免死锁。
- 同一地址不同资产的写操作在同一把锁内串行，杜绝资产间丢失更新与半更新。
- 余额变更与流水追加在同一临界区完成；流水带资产标记。
- 事件状态上有 confirmed/reorged/finalized 幂等闸门，重复扫描/重复回撤安全。

回撤优先（确定性）
------------------
同一批次（或并发同时到达）里，确认与回撤都到达时：先统一执行回撤，
再执行确认。已回撤事件上的确认一律忽略，因此结论唯一可复现。

资金守恒（按资产独立成立）
-------------------------
- DEPOSIT 进入该资产 pending_in；确认时从 pending_in 转入 available。
- 回撤：先退该资产 pending_in；若已确认，则从该资产 available 扣回，
  available 不足部分计入该资产的 ``reorg_debt``（系统损失汇账户），
  available 被钳制为 0，永不为负，账面对“外部已花出”的资金做了显式平账。
- 转出/提现即时扣对应资产的 available，该资产余额不足显式拒绝。
"""

from __future__ import annotations

from contextlib import contextmanager

from .audit import get_logger
from .domain import (
    DEFAULT_ASSET,
    AssetBalance,
    Balance,
    ChainEvent,
    ChainEventRecord,
    ChainEventType,
    EventStatus,
    IdempotencyRecord,
    LedgerEntry,
    LedgerTxnType,
    Principal,
    PrincipalRole,
)
from .errors import (
    DuplicateEventError,
    IdempotencyConflictError,
    InsufficientFundsError,
    ResourceNotFoundError,
)
from .idempotency import namespaced_key, request_fingerprint
from .normalizer import normalize_asset, parse_amount
from .repository import LedgerRepository
from .security import authorize_wallet, require_role

log = get_logger("wallet_ledger.service")

# 系统损失汇账户（回撤时可用不足的平账去向）。
LOSS_SINK = "@loss-sink"


class LedgerService:
    def __init__(self, repo: LedgerRepository) -> None:
        self.repo = repo

    # ------------------------------------------------------------------ #
    # 锁工具
    # ------------------------------------------------------------------ #

    @contextmanager
    def _locked(self, *addresses: str):
        ordered = sorted({a for a in addresses if a})
        locks = [self.repo.lock_for(a) for a in ordered]
        for lock in locks:
            lock.acquire()
        try:
            yield
        finally:
            for lock in reversed(locks):
                lock.release()

    def _authorized_wallet(self, principal: Principal, address: str) -> Balance:
        authorize_wallet(principal, address)
        bal = self.repo.get_balance(address)
        if bal is None:
            # 鉴权通过但钱包未创建：仍返回同一 404，不区分“无权限/不存在”。
            raise ResourceNotFoundError("wallet not found",
                                        details={"address": address})
        return bal

    # ------------------------------------------------------------------ #
    # 幂等
    # ------------------------------------------------------------------ #

    def _idempotency_begin(
        self, principal: Principal, key: str, payload: dict
    ) -> IdempotencyRecord | None:
        """首次提交返回 None；重复提交返回已存记录（安全回放）；
        同一键对应不同请求体 -> IdempotencyConflictError（409）。
        """
        ns_key = namespaced_key(principal.principal_id, key)
        record = self.repo.get_idempotency(ns_key)
        fingerprint = request_fingerprint(payload)
        if record is not None:
            if record.request_fingerprint != fingerprint:
                log.warning("idempotency conflict key=%s fp=%s != %s",
                            key, fingerprint, record.request_fingerprint)
                raise IdempotencyConflictError(
                    "same idempotency key reused with different request",
                    details={"idempotency_key": key},
                )
            log.info("duplicate request replayed key=%s", key)
        return record

    def _idempotency_commit(
        self, principal: Principal, key: str, payload: dict,
        status_code: int, body: dict,
    ) -> None:
        ns_key = namespaced_key(principal.principal_id, key)
        self.repo.put_idempotency(
            IdempotencyRecord(
                key=ns_key,
                request_fingerprint=request_fingerprint(payload),
                status_code=status_code,
                body=body,
            )
        )

    # ------------------------------------------------------------------ #
    # 钱包
    # ------------------------------------------------------------------ #

    def create_wallet(self, principal: Principal, address: str) -> Balance:
        authorize_wallet(principal, address)
        with self._locked(address):
            bal = self.repo.ensure_wallet(address, principal.principal_id)
        log.info("wallet ensured address=%s owner=%s", address, principal.principal_id)
        return bal

    def get_balance(
        self, principal: Principal, address: str, asset: str = DEFAULT_ASSET
    ) -> Balance:
        bal = self._authorized_wallet(principal, address)
        sub = bal.get(asset)
        log.info(
            "balance query address=%s asset=%s available=%d pending_in=%d reorg_debt=%d",
            address, asset, sub.available, sub.pending_in, sub.reorg_debt,
        )
        return bal

    def get_asset_balance(
        self, principal: Principal, address: str, asset: str = DEFAULT_ASSET
    ) -> AssetBalance:
        """指定资产的分口径余额。"""
        bal = self._authorized_wallet(principal, address)
        return bal.get(asset)

    # ------------------------------------------------------------------ #
    # 链上事件投喂（INDEXER）
    # ------------------------------------------------------------------ #

    def ingest_event(
        self, principal: Principal, event: ChainEvent
    ) -> ChainEventRecord:
        require_role(principal, PrincipalRole.INDEXER)
        key = event.natural_key
        # 事件涉及的钱包可能尚未创建；先保证钱包存在（归属保持开放，由
        # 用户后续 create_wallet 认领同一地址，余额即可见）。
        with self._locked(event.address):
            self.repo.ensure_wallet(event.address, principal.principal_id)
            if self.repo.get_event(key) is not None:
                log.warning("duplicate chain event key=%s rejected", key)
                raise DuplicateEventError(
                    "chain event already ingested",
                    details={"tx_hash": event.tx_hash, "log_index": event.log_index},
                )

            if event.type is ChainEventType.WITHDRAWAL:
                # 链上转出事件：观察即从该资产可用余额冻结/扣减（模拟链已确认扣款）。
                # 记为 CONFIRMED（confirmed 闸门防止重复），但不是 FINALIZED：
                # 与充值事件入口对齐，处在最终性深度之内时仍可被重组回撤，
                # 回撤把资金原路退回该资产。
                bal = self.repo.get_balance(event.address)
                assert bal is not None
                sub = bal.ensure(event.asset)
                record = ChainEventRecord(
                    event=event, status=EventStatus.CONFIRMED,
                    seen_height=event.block_height,
                    confirmations=2**31 - 1, confirmed=True, finalized=False,
                )
                self.repo.put_event_if_absent(record)
                self._debit_available(
                    sub, event.amount, LedgerTxnType.WITHDRAW_OUT,
                    address=event.address,
                    ref_key=f"{event.tx_hash}:{event.log_index}",
                    asset=event.asset,
                    memo="on-chain withdrawal observed",
                )
                log.info("withdrawal event applied address=%s asset=%s amount=%d key=%s",
                         event.address, event.asset, event.amount, key)
                return record

            record = ChainEventRecord(
                event=event, status=EventStatus.PENDING,
                seen_height=event.block_height,
            )
            stored = self.repo.put_event_if_absent(record)
            if stored is record:
                bal = self.repo.get_balance(event.address)
                assert bal is not None
                sub = bal.ensure(event.asset)
                sub.pending_in += event.amount
                self.repo.append_entry(
                    LedgerTxnType.DEPOSIT_PENDING, event.address, event.amount,
                    ref_key=f"{event.tx_hash}:{event.log_index}",
                    asset=event.asset,
                    memo=f"deposit observed @height={event.block_height}",
                )
                log.info(
                    "deposit pending address=%s asset=%s amount=%d key=%s pending_in=%d",
                    event.address, event.asset, event.amount, key, sub.pending_in,
                )
            return stored

    # ------------------------------------------------------------------ #
    # 确认扫描 / 重组
    # ------------------------------------------------------------------ #

    def apply_confirmations(
        self,
        principal: Principal,
        event_keys: list[tuple[str, int]],
        confirmations: int,
        tip_height: int,
        required_confirmations: int,
        finalization_depth: int = 64,
    ) -> list[ChainEventRecord]:
        require_role(principal, PrincipalRole.INDEXER)
        records = self._resolve_records(event_keys)
        addresses = {r.event.address for r in records}
        affected: list[ChainEventRecord] = []
        with self._locked(*addresses):
            for record in records:
                if record.reorged:
                    # 回撤优先：回撤后的确认一律忽略（确定性规则）。
                    log.info("confirm ignored on reorged key=%s",
                             record.event.natural_key)
                    continue
                record.confirmations = max(record.confirmations, confirmations)
                if not record.confirmed and confirmations >= required_confirmations:
                    self._confirm_deposit(record)
                if (
                    not record.finalized
                    and confirmations >= finalization_depth
                ):
                    record.finalized = True
                    record.status = EventStatus.FINALIZED
                affected.append(record)
        return affected

    def apply_reorg(
        self,
        principal: Principal,
        event_keys: list[tuple[str, int]],
        detached_height: int,
    ) -> list[ChainEventRecord]:
        require_role(principal, PrincipalRole.INDEXER)
        records = self._resolve_records(event_keys)
        addresses = {r.event.address for r in records}
        affected: list[ChainEventRecord] = []
        with self._locked(*addresses):
            for record in records:
                if record.finalized:
                    # 超过最终性深度的事件不可回撤，显式拒绝而非静默。
                    log.warning("reorg rejected: finalized key=%s",
                                record.event.natural_key)
                    continue
                if record.reorged:
                    log.info("reorg already applied key=%s (idempotent)",
                             record.event.natural_key)
                    continue
                self._reorg_record(record, detached_height)
                affected.append(record)
        return affected

    def _resolve_records(
        self, event_keys: list[tuple[str, int]]
    ) -> list[ChainEventRecord]:
        missing = [k for k in event_keys if self.repo.get_event(k) is None]
        if missing:
            raise ResourceNotFoundError(
                "unknown chain event(s)", details={"keys": [list(k) for k in missing]}
            )
        return [self.repo.get_event(k) for k in event_keys]  # type: ignore[list-item]

    def _confirm_deposit(self, record: ChainEventRecord) -> None:
        event = record.event
        bal = self.repo.get_balance(event.address)
        assert bal is not None
        sub = bal.ensure(event.asset)
        record.confirmed = True
        record.status = EventStatus.CONFIRMED
        # pending_in -> available（该资产 pending_in 一定包含该金额）。
        sub.pending_in -= event.amount
        sub.available += event.amount
        self.repo.append_entry(
            LedgerTxnType.DEPOSIT_CONFIRM, event.address, event.amount,
            ref_key=f"{event.tx_hash}:{event.log_index}",
            asset=event.asset,
            memo=f"confirmations={record.confirmations} reached finality",
        )
        log.info(
            "deposit confirmed key=%s address=%s asset=%s amount=%d available=%d",
            event.natural_key, event.address, event.asset,
            event.amount, sub.available,
        )

    def _reorg_record(self, record: ChainEventRecord, detached_height: int) -> None:
        event = record.event
        bal = self.repo.get_balance(event.address)
        assert bal is not None
        sub = bal.ensure(event.asset)
        record.reorged = True
        record.status = EventStatus.REORGED
        ref = f"{event.tx_hash}:{event.log_index}"

        if event.type is ChainEventType.WITHDRAWAL:
            # 链上转出事件被回撤：钱回到该资产可用余额。
            sub.available += event.amount
            self.repo.append_entry(
                LedgerTxnType.REORG_CLAWBACK, event.address, event.amount,
                ref_key=ref, asset=event.asset,
                memo="withdrawal reorged, funds returned",
            )
            log.info("withdrawal reorged funds-returned key=%s asset=%s amount=%d",
                     event.natural_key, event.asset, event.amount)
            return

        # 充值回撤
        if not record.confirmed:
            # 尚未计入可用：仅退出该资产待入账。
            sub.pending_in -= event.amount
            memo = "unconfirmed deposit reorged"
        else:
            # 已计入可用：扣回；不足部分进入该资产 reorg_debt 平账，余额不为负。
            sub.available -= event.amount
            if sub.available < 0:
                shortfall = -sub.available
                sub.reorg_debt += shortfall
                sub.available = 0
                # 损失汇账户对应一条平账流水（资金去向可解释，按资产标记）。
                self.repo.append_entry(
                    LedgerTxnType.REORG_CLAWBACK, LOSS_SINK, shortfall,
                    ref_key=ref, asset=event.asset,
                    memo=f"clawback shortfall booked to loss sink ({event.asset})",
                )
                log.warning(
                    "clawback shortfall key=%s asset=%s shortfall=%d reorg_debt=%d",
                    event.natural_key, event.asset, shortfall, sub.reorg_debt,
                )
            memo = "confirmed deposit reorged and clawed back"
        self.repo.append_entry(
            LedgerTxnType.REORG_CLAWBACK, event.address, event.amount,
            ref_key=ref, asset=event.asset, memo=memo,
        )
        log.info(
            "deposit reorged key=%s address=%s asset=%s amount=%d available=%d pending_in=%d",
            event.natural_key, event.address, event.asset, event.amount,
            sub.available, sub.pending_in,
        )

    def _debit_available(
        self,
        sub: AssetBalance,
        amount: int,
        txn_type: LedgerTxnType,
        *,
        address: str,
        ref_key: str,
        asset: str = DEFAULT_ASSET,
        memo: str = "",
    ) -> None:
        """从指定资产的可用余额扣款；链上事件口径下余额不足时钳 0 并显式平账。

        用户操作（transfer/withdraw）走余额不足显式拒绝路径，不调用本方法；
        本方法仅用于“链上已确认扣款”这类必须反映链上事实的入口，保证该资产
        余额永不为负、资金去向可解释、守恒式仍然成立。
        """
        if sub.available < amount:
            shortfall = amount - sub.available
            sub.available = 0
            sub.reorg_debt += shortfall
            self.repo.append_entry(
                LedgerTxnType.REORG_CLAWBACK, LOSS_SINK, shortfall,
                ref_key=ref_key, asset=asset,
                memo=f"on-chain debit exceeds ledger balance ({asset})",
            )
            log.warning(
                "on-chain debit shortfall address=%s asset=%s shortfall=%d reorg_debt=%d",
                address, asset, shortfall, sub.reorg_debt,
            )
        else:
            sub.available -= amount
        self.repo.append_entry(
            txn_type, address, amount, ref_key=ref_key, asset=asset, memo=memo,
        )

    # ------------------------------------------------------------------ #
    # 用户资金操作（幂等）
    # ------------------------------------------------------------------ #

    def transfer(
        self,
        principal: Principal,
        from_address: str,
        to_address: str,
        amount: int,
        idempotency_key: str,
        asset: str = DEFAULT_ASSET,
    ) -> dict:
        amount = self._check_amount(amount)
        asset = normalize_asset(asset)
        self._authorized_wallet(principal, from_address)
        # 收款方若不在调用者钱包范围内：为不泄露存在性，对越权目标统一 404。
        authorize_wallet(principal, to_address)
        if self.repo.get_balance(to_address) is None:
            raise ResourceNotFoundError("wallet not found",
                                        details={"address": to_address})
        payload = {
            "op": "transfer", "from": from_address, "to": to_address,
            "amount": amount, "idempotency_key": idempotency_key,
            "asset": asset,
        }
        with self._locked(from_address, to_address):
            existing = self._idempotency_begin(principal, idempotency_key, payload)
            if existing is not None:
                return {**existing.body, "replay": True}
            src = self.repo.get_balance(from_address)
            dst = self.repo.get_balance(to_address)
            assert src is not None and dst is not None
            src_sub = src.ensure(asset)
            if src_sub.available < amount:
                log.warning(
                    "transfer rejected insufficient funds %s -> %s asset=%s have=%d need=%d",
                    from_address, to_address, asset, src_sub.available, amount,
                )
                raise InsufficientFundsError(
                    "available balance insufficient",
                    details={"asset": asset,
                             "available": src_sub.available, "requested": amount},
                )
            dst_sub = dst.ensure(asset)
            src_sub.available -= amount
            dst_sub.available += amount
            self.repo.append_entry(
                LedgerTxnType.TRANSFER_OUT, from_address, amount,
                ref_key=f"idem:{idempotency_key}", asset=asset,
                memo=f"transfer {asset} to {to_address}",
            )
            self.repo.append_entry(
                LedgerTxnType.TRANSFER_IN, to_address, amount,
                ref_key=f"idem:{idempotency_key}", asset=asset,
                memo=f"transfer {asset} from {from_address}",
            )
            body = {
                "status": "ok", "op": "transfer",
                "from": from_address, "to": to_address,
                "amount": amount, "asset": asset,
                "balances": {
                    from_address: src_sub.available,
                    to_address: dst_sub.available,
                },
            }
            self._idempotency_commit(
                principal, idempotency_key, payload, 200, body
            )
            log.info("transfer ok %s -> %s asset=%s amount=%d",
                     from_address, to_address, asset, amount)
            return body

    def withdraw(
        self,
        principal: Principal,
        address: str,
        amount: int,
        idempotency_key: str,
        asset: str = DEFAULT_ASSET,
    ) -> dict:
        amount = self._check_amount(amount)
        asset = normalize_asset(asset)
        self._authorized_wallet(principal, address)
        payload = {
            "op": "withdraw", "address": address,
            "amount": amount, "idempotency_key": idempotency_key,
            "asset": asset,
        }
        with self._locked(address):
            existing = self._idempotency_begin(principal, idempotency_key, payload)
            if existing is not None:
                return {**existing.body, "replay": True}
            bal = self.repo.get_balance(address)
            assert bal is not None
            sub = bal.ensure(asset)
            if sub.available < amount:
                log.warning(
                    "withdraw rejected insufficient funds address=%s asset=%s have=%d need=%d",
                    address, asset, sub.available, amount,
                )
                raise InsufficientFundsError(
                    "available balance insufficient",
                    details={"asset": asset,
                             "available": sub.available, "requested": amount},
                )
            sub.available -= amount
            self.repo.append_entry(
                LedgerTxnType.WITHDRAW_OUT, address, amount,
                ref_key=f"idem:{idempotency_key}", asset=asset,
                memo=f"off-chain withdraw {asset}",
            )
            body = {
                "status": "ok", "op": "withdraw",
                "address": address, "amount": amount, "asset": asset,
                "available": sub.available,
            }
            self._idempotency_commit(principal, idempotency_key, payload, 200, body)
            log.info("withdraw ok address=%s asset=%s amount=%d available=%d",
                     address, asset, amount, sub.available)
            return body

    # ------------------------------------------------------------------ #

    def list_entries(
        self,
        principal: Principal,
        address: str,
        asset: str | None = None,
    ) -> list[LedgerEntry]:
        self._authorized_wallet(principal, address)
        return self.repo.list_entries(address, asset)

    @staticmethod
    def _check_amount(amount: int) -> int:
        # 复用归一化的金额规则；非法金额直接抛 INVALID_AMOUNT。
        return parse_amount(amount)
