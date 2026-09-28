"""账本领域服务：资金结论的唯一权威来源。

多资产
------
- 所有资金变动按 ``(钱包地址, 资产)`` 落账；请求不带资产时按 ``DEFAULT_ASSET``
  处理，旧的单资产用法不受影响。
- 不同资产有独立的余额桶与流水标记，互不抵扣、互不串账；同一钱包锁下串行，
  并发操作不同资产不会丢更新或半更新。

并发与一致性
------------
- 每个钱包地址一把 RLock；跨钱包写操作按地址排序后依次加锁，避免死锁。
- 余额变更与流水追加在同一临界区完成，保证不出现半更新与丢失更新。
- 事件状态上有 confirmed/reorged/finalized 幂等闸门，重复扫描/重复回撤安全。

回撤优先（确定性）
------------------
同一批次（或并发同时到达）里，确认与回撤都到达时：先统一执行回撤，
再执行确认。已回撤事件上的确认一律忽略，因此结论唯一可复现。

资金守恒（按资产分别成立）
--------------------------
- DEPOSIT 进入 pending_in；确认时从 pending_in 转入 available。
- 充值回撤：先退 pending_in；若已确认，则从 available 扣回，available 不足部分
  计入 ``reorg_debt``（系统损失汇账户），available 被钳制为 0，永不为负，
  账面对“外部已花出”的资金做了显式平账。
- 链上转出事件观察即按既成事实扣款：从该资产 available 实扣（不足钳 0），
  缺口计入该资产 ``reorg_debt``，事件记录上留存 ``debited_amount`` /
  ``shortfall_amount`` 原始拆分。该事件被重组回撤时恢复到转出发生之前：
  只把实扣金额退回 available、只把缺口从 reorg_debt 冲销（损失汇账户追加
  反向流水），绝不按整笔金额回退而凭空抬高可用余额。
- 用户主动转出/提现即时扣 available，余额不足显式拒绝。
"""

from __future__ import annotations

from contextlib import contextmanager

from .audit import get_logger
from .domain import (
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
    WalletBalance,
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
        self, principal: Principal, address: str, asset: str | None = None
    ) -> Balance:
        """单资产余额视图；``asset=None`` 时返回默认资产（兼容旧用法）。"""
        symbol = normalize_asset(asset)
        self._authorized_wallet(principal, address)
        bal = self.repo.get_balance(address, symbol)
        if bal is None:
            # 钱包存在但该资产尚无账本：返回零值视图（不创建，避免查询放大状态）。
            bal = Balance(address=address, asset=symbol)
        log.info(
            "balance query address=%s asset=%s available=%d pending_in=%d reorg_debt=%d",
            address, symbol, bal.available, bal.pending_in, bal.reorg_debt,
        )
        return bal

    def get_wallet_balance(self, principal: Principal, address: str) -> WalletBalance:
        """钱包级视图：一次返回每个资产各自的分口径余额。"""
        self._authorized_wallet(principal, address)
        view = WalletBalance(address=address,
                             assets=self.repo.get_asset_balances(address))
        log.info(
            "wallet balance query address=%s assets=%s",
            address,
            {a: b.available for a, b in sorted(view.assets.items())},
        )
        return view

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
                # 链上转出事件：观察即从可用余额扣减（模拟链已确认扣款）。
                # confirmed=True 表示扣款已生效；finalized 仍由确认数推进，
                # 未达最终性深度前可被重组回撤（只退回真正扣掉的部分）。
                bal = self.repo.ensure_balance(event.address, event.asset)
                record = ChainEventRecord(
                    event=event, status=EventStatus.CONFIRMED,
                    seen_height=event.block_height,
                    confirmations=0, confirmed=True, finalized=False,
                )
                self.repo.put_event_if_absent(record)
                debited, shortfall = self._debit_available(
                    bal, event.amount, LedgerTxnType.WITHDRAW_OUT,
                    ref_key=f"{event.tx_hash}:{event.log_index}",
                    memo="on-chain withdrawal observed",
                    asset=event.asset,
                )
                # 记录原始拆分：回撤时只退 debited、只冲销 shortfall，
                # 使账面恢复到这笔转出发生之前，不虚增可用、不留残缺口。
                record.debited_amount = debited
                record.shortfall_amount = shortfall
                log.info("withdrawal event applied address=%s asset=%s amount=%d key=%s debited=%d shortfall=%d",
                         event.address, event.asset, event.amount, key,
                         debited, shortfall)
                return record

            record = ChainEventRecord(
                event=event, status=EventStatus.PENDING,
                seen_height=event.block_height,
            )
            stored = self.repo.put_event_if_absent(record)
            if stored is record:
                bal = self.repo.ensure_balance(event.address, event.asset)
                bal.pending_in += event.amount
                self.repo.append_entry(
                    LedgerTxnType.DEPOSIT_PENDING, event.address, event.amount,
                    ref_key=f"{event.tx_hash}:{event.log_index}",
                    memo=f"deposit observed @height={event.block_height}",
                    asset=event.asset,
                )
                log.info(
                    "deposit pending address=%s asset=%s amount=%d key=%s pending_in=%d",
                    event.address, event.asset, event.amount, key, bal.pending_in,
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
        bal = self.repo.get_balance(event.address, event.asset)
        assert bal is not None
        record.confirmed = True
        record.status = EventStatus.CONFIRMED
        # pending_in -> available（pending_in 一定包含该金额）。
        bal.pending_in -= event.amount
        bal.available += event.amount
        self.repo.append_entry(
            LedgerTxnType.DEPOSIT_CONFIRM, event.address, event.amount,
            ref_key=f"{event.tx_hash}:{event.log_index}",
            memo=f"confirmations={record.confirmations} reached finality",
            asset=event.asset,
        )
        log.info(
            "deposit confirmed key=%s address=%s asset=%s amount=%d available=%d",
            event.natural_key, event.address, event.asset,
            event.amount, bal.available,
        )

    def _reorg_record(self, record: ChainEventRecord, detached_height: int) -> None:
        event = record.event
        bal = self.repo.get_balance(event.address, event.asset)
        assert bal is not None
        record.reorged = True
        record.status = EventStatus.REORGED
        ref = f"{event.tx_hash}:{event.log_index}"

        if event.type is ChainEventType.WITHDRAWAL:
            # 链上转出事件被回撤：账面恢复到这笔转出发生之前。
            # - 只把当初真正从可用余额扣掉的部分（debited）退回可用；
            # - 当初因可用不足而平账到 reorg_debt 的缺口（shortfall）一并冲销，
            #   并追加一条损失汇账户的反向流水。
            # 绝不能按整笔金额回退：否则缺口部分会凭空抬高可用余额。
            # 回退只作用于本事件的资产桶，不同资产互不串账。
            debited = record.debited_amount
            shortfall = record.shortfall_amount
            if debited:
                bal.available += debited
            if shortfall:
                bal.reorg_debt -= shortfall
                # 损失汇账户反向流水（与当初平账流水一一对应，资金去向可解释）。
                self.repo.append_entry(
                    LedgerTxnType.REORG_SHORTFALL_REVERSAL, LOSS_SINK, shortfall,
                    ref_key=ref,
                    memo="withdrawal reorged, debit shortfall reversed",
                    asset=event.asset,
                )
            self.repo.append_entry(
                LedgerTxnType.REORG_CLAWBACK, event.address, debited,
                ref_key=ref, memo="withdrawal reorged, debited funds returned",
                asset=event.asset,
            )
            log.info(
                "withdrawal reorged key=%s asset=%s debited=%d returned shortfall=%d reversed available=%d reorg_debt=%d",
                event.natural_key, event.asset, debited, shortfall,
                bal.available, bal.reorg_debt,
            )
            return

        # 充值回撤
        if not record.confirmed:
            # 尚未计入可用：仅退出待入账。
            bal.pending_in -= event.amount
            memo = "unconfirmed deposit reorged"
        else:
            # 已计入可用：扣回；不足部分进入 reorg_debt 平账，余额不为负。
            bal.available -= event.amount
            if bal.available < 0:
                shortfall = -bal.available
                bal.reorg_debt += shortfall
                bal.available = 0
                # 损失汇账户对应一条平账流水（资金去向可解释）。
                self.repo.append_entry(
                    LedgerTxnType.REORG_CLAWBACK, LOSS_SINK, shortfall,
                    ref_key=ref, memo="clawback shortfall booked to loss sink",
                    asset=event.asset,
                )
                log.warning(
                    "clawback shortfall key=%s asset=%s shortfall=%d reorg_debt=%d",
                    event.natural_key, event.asset, shortfall, bal.reorg_debt,
                )
            memo = "confirmed deposit reorged and clawed back"
        self.repo.append_entry(
            LedgerTxnType.REORG_CLAWBACK, event.address, event.amount,
            ref_key=ref, memo=memo,
            asset=event.asset,
        )
        log.info(
            "deposit reorged key=%s address=%s asset=%s amount=%d available=%d pending_in=%d",
            event.natural_key, event.address, event.asset, event.amount,
            bal.available, bal.pending_in,
        )

    def _debit_available(
        self,
        bal: Balance,
        amount: int,
        txn_type: LedgerTxnType,
        ref_key: str,
        memo: str,
        asset: str,
    ) -> tuple[int, int]:
        """链上事件扣款：从指定资产可用余额扣回；不足部分计入 reorg_debt 平账。

        与用户主动转账/提现不同，链上事件是既成事实（不能拒绝），因此余额永不为负，
        缺口落到系统损失汇账户，维持该资产上的资金守恒。

        返回 ``(实际从可用余额扣掉的金额, 计入 reorg_debt 的缺口)``，供该事件日后
        被重组回撤时按原始拆分精确冲销。
        """
        debited = min(bal.available, amount)
        shortfall = amount - debited
        bal.available -= debited
        if shortfall:
            bal.reorg_debt += shortfall
            self.repo.append_entry(
                LedgerTxnType.REORG_CLAWBACK, LOSS_SINK, shortfall,
                ref_key=ref_key, memo="on-chain debit shortfall booked to loss sink",
                asset=asset,
            )
            log.warning(
                "on-chain debit shortfall address=%s asset=%s shortfall=%d reorg_debt=%d",
                bal.address, asset, shortfall, bal.reorg_debt,
            )
        self.repo.append_entry(
            txn_type, bal.address, amount,
            ref_key=ref_key, memo=memo, asset=asset,
        )
        return debited, shortfall

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
        asset: str | None = None,
    ) -> dict:
        amount = self._check_amount(amount)
        symbol = normalize_asset(asset)
        self._authorized_wallet(principal, from_address)
        # 收款方若不在调用者钱包范围内：为不泄露存在性，对越权目标统一 404。
        authorize_wallet(principal, to_address)
        if not self.repo.wallet_exists(to_address):
            raise ResourceNotFoundError("wallet not found",
                                        details={"address": to_address})
        payload = {
            "op": "transfer", "from": from_address, "to": to_address,
            "amount": amount, "idempotency_key": idempotency_key,
            "asset": symbol,
        }
        with self._locked(from_address, to_address):
            existing = self._idempotency_begin(principal, idempotency_key, payload)
            if existing is not None:
                return {**existing.body, "replay": True}
            src = self.repo.get_balance(from_address, symbol)
            if src is None:
                # 付款方钱包存在但该资产无账本：余额视为 0，直接走不足拒绝。
                src = self.repo.ensure_balance(from_address, symbol)
            dst = self.repo.ensure_balance(to_address, symbol)
            if src.available < amount:
                log.warning("transfer rejected insufficient funds %s -> %s asset=%s have=%d need=%d",
                            from_address, to_address, symbol, src.available, amount)
                raise InsufficientFundsError(
                    "available balance insufficient",
                    details={
                        "asset": symbol,
                        "available": src.available, "requested": amount,
                    },
                )
            src.available -= amount
            dst.available += amount
            self.repo.append_entry(
                LedgerTxnType.TRANSFER_OUT, from_address, amount,
                ref_key=f"idem:{idempotency_key}",
                memo=f"transfer {symbol} to {to_address}", asset=symbol,
            )
            self.repo.append_entry(
                LedgerTxnType.TRANSFER_IN, to_address, amount,
                ref_key=f"idem:{idempotency_key}",
                memo=f"transfer {symbol} from {from_address}", asset=symbol,
            )
            body = {
                "status": "ok", "op": "transfer",
                "from": from_address, "to": to_address,
                "amount": amount, "asset": symbol,
                "balances": {
                    from_address: src.available, to_address: dst.available,
                },
            }
            self._idempotency_commit(
                principal, idempotency_key, payload, 200, body
            )
            log.info("transfer ok %s -> %s asset=%s amount=%d",
                     from_address, to_address, symbol, amount)
            return body

    def withdraw(
        self,
        principal: Principal,
        address: str,
        amount: int,
        idempotency_key: str,
        asset: str | None = None,
    ) -> dict:
        amount = self._check_amount(amount)
        symbol = normalize_asset(asset)
        self._authorized_wallet(principal, address)
        payload = {
            "op": "withdraw", "address": address,
            "amount": amount, "idempotency_key": idempotency_key,
            "asset": symbol,
        }
        with self._locked(address):
            existing = self._idempotency_begin(principal, idempotency_key, payload)
            if existing is not None:
                return {**existing.body, "replay": True}
            bal = self.repo.get_balance(address, symbol)
            if bal is None:
                bal = self.repo.ensure_balance(address, symbol)
            if bal.available < amount:
                log.warning("withdraw rejected insufficient funds address=%s asset=%s have=%d need=%d",
                            address, symbol, bal.available, amount)
                raise InsufficientFundsError(
                    "available balance insufficient",
                    details={
                        "asset": symbol,
                        "available": bal.available, "requested": amount,
                    },
                )
            bal.available -= amount
            self.repo.append_entry(
                LedgerTxnType.WITHDRAW_OUT, address, amount,
                ref_key=f"idem:{idempotency_key}",
                memo=f"off-chain withdraw {symbol}", asset=symbol,
            )
            body = {
                "status": "ok", "op": "withdraw",
                "address": address, "amount": amount, "asset": symbol,
                "available": bal.available,
            }
            self._idempotency_commit(principal, idempotency_key, payload, 200, body)
            log.info("withdraw ok address=%s asset=%s amount=%d available=%d",
                     address, symbol, amount, bal.available)
            return body

    # ------------------------------------------------------------------ #

    def list_entries(
        self,
        principal: Principal,
        address: str,
        asset: str | None = None,
    ) -> list[LedgerEntry]:
        self._authorized_wallet(principal, address)
        symbol = normalize_asset(asset) if asset is not None else None
        return self.repo.list_entries(address, asset=symbol)

    @staticmethod
    def _check_amount(amount: int) -> int:
        # 复用归一化的金额规则；非法金额直接抛 INVALID_AMOUNT。
        return parse_amount(amount)
