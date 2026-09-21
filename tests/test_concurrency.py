"""并发安全：同一钱包并发写结果一致，资金守恒，不负余额、不重复入账。

直接驱动服务层（绕过 HTTP），用线程池制造对同一钱包的竞争；
每次操作后断言余额恒等式与非负约束。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from app.config import Settings
from app.models import ChainEvent, EventKind
from app.errors import (
    IdempotencyConflict, IdempotencyReplay, InsufficientFunds,
)


@pytest.fixture
def env(svc):
    # 复用全局 svc（autouse 的流水打印依赖它），再用确定性配置覆盖其内部组件
    from app.chain import MockChain
    from app.ingestion import IngestionService
    from app.ledger import LedgerService
    settings = Settings(confirmations_required=1, finality_depth=1000,
                        rate_limit_capacity=10_000)
    svc.settings = settings
    svc.store.reset()
    svc.chain = MockChain()
    svc.ledger = LedgerService(svc.store, settings)
    svc.ingestion = IngestionService(svc.store, svc.chain, svc.ledger, settings)
    svc.ledger.create_wallet("w1", "u1", "0xA")
    svc.ledger.create_wallet("w2", "u2", "0xB")
    return svc


def _assert_non_negative(env):
    for wid in ("w1", "w2"):
        b = env.ledger.get_balance(wid)
        assert b.available >= 0 and b.holds >= 0
        assert b.pending_in >= 0 and b.clawback_due >= 0


def _system_total(env):
    total = 0
    for wid in ("w1", "w2"):
        b = env.ledger.get_balance(wid)
        total += b.available + b.holds + b.pending_in - b.clawback_due
    return total


def test_concurrent_transfers_unique_keys_conserve_funds(env):
    # w1 预存 1000，50 个线程各尝试转 100（唯一 request_id）
    for i in range(10):
        env.ledger.ingest_event(
            ChainEvent(f"d{i}", "0xA", EventKind.DEPOSIT, 100, 1, f"t{i}"))
    env.ledger.confirm_events(1)
    assert env.ledger.get_balance("w1").available == 1000

    results = {"ok": 0, "insufficient": 0}
    barrier = threading.Barrier(32)

    def worker(i):
        barrier.wait()
        try:
            env.ledger.transfer("w1", "w2", 100, f"k-{i}")
            return "ok"
        except InsufficientFunds:
            return "insufficient"

    with ThreadPoolExecutor(max_workers=32) as ex:
        futs = [ex.submit(worker, i) for i in range(32)]
        for f in as_completed(futs):
            results[f.result()] += 1

    _assert_non_negative(env)
    b1 = env.ledger.get_balance("w1")
    b2 = env.ledger.get_balance("w2")
    # 资金守恒：总额 1000；成功笔数恰好为 10
    assert results["ok"] == 10
    assert results["insufficient"] == 22
    assert b1.available == 0 and b2.available == 1000
    assert _system_total(env) == 1000


def test_concurrent_same_idempotency_key_single_debit(env):
    env.ledger.ingest_event(
        ChainEvent("d0", "0xA", EventKind.DEPOSIT, 1000, 1, "t0"))
    env.ledger.confirm_events(1)
    barrier = threading.Barrier(20)
    outcomes = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            env.ledger.transfer("w1", "w2", 100, "SAME-KEY")
            with lock:
                outcomes.append("ok")
        except IdempotencyReplay:
            with lock:
                outcomes.append("replay")
        except IdempotencyConflict:
            with lock:
                outcomes.append("conflict")

    with ThreadPoolExecutor(max_workers=20) as ex:
        list(ex.map(lambda _: worker(), range(20)))

    _assert_non_negative(env)
    # 恰好一次成功；其余为重放或并发冲突；总额恰好移动 100
    assert outcomes.count("ok") == 1
    assert env.ledger.get_balance("w1").available == 900
    assert env.ledger.get_balance("w2").available == 100
    assert _system_total(env) == 1000


def test_concurrent_confirm_and_reorg_no_duplicate_credit(env):
    # 100 笔 pending 充值，确认与回退并发，最终不出现重复入账
    for i in range(100):
        env.ledger.ingest_event(
            ChainEvent(f"e{i}", "0xA", EventKind.DEPOSIT, 10, 1, f"tx{i}"))
    stop = threading.Event()

    def confirmer():
        while not stop.is_set():
            env.ledger.confirm_events(1)

    def reverter():
        ids = [f"e{i}" for i in range(100)]
        env.ledger.reorg(ids, current_height=1)

    t = threading.Thread(target=confirmer)
    t.start()
    reverter()
    stop.set()
    t.join()
    env.ledger.confirm_events(1)  # 回退后再确认，必须幂等
    _assert_non_negative(env)
    b = env.ledger.get_balance("w1")
    # 全部回退：无可用、无在途；若有冲账也是确定一致的
    assert b.available == 0 and b.pending_in == 0
    assert b.clawback_due == 0


def test_concurrent_withdrawals_never_overspend(env):
    env.ledger.ingest_event(
        ChainEvent("d0", "0xA", EventKind.DEPOSIT, 500, 1, "t0"))
    env.ledger.confirm_events(1)
    barrier = threading.Barrier(20)

    def worker(i):
        barrier.wait()
        try:
            env.ledger.withdraw("w1", 50, f"wd-{i}")
            return "ok"
        except InsufficientFunds:
            return "nsf"

    with ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(worker, range(20)))

    _assert_non_negative(env)
    b = env.ledger.get_balance("w1")
    ok = results.count("ok")
    assert ok == 10 and results.count("nsf") == 10
    assert b.available == 0 and b.holds == 500
    assert _system_total(env) == 500


def test_concurrent_deposits_confirmations_are_all_counted(env):
    # 并发收录 200 笔充值，再确认：无丢失更新
    def ingester(i):
        env.ledger.ingest_event(
            ChainEvent(f"p{i}", "0xB", EventKind.DEPOSIT, 1, 1, f"p{i}"))

    with ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(ingester, range(200)))
    env.ledger.confirm_events(1)
    b = env.ledger.get_balance("w2")
    assert b.available == 200 and b.pending_in == 0
    assert _system_total(env) == 200
