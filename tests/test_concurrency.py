"""同一钱包并发入账/转出/确认/回撤：结果一致、无丢失更新/重复入账/负余额/半更新。"""

from __future__ import annotations

import threading

from wallet_ledger.normalizer import normalize_event


def _fund(service, indexer, tx, address, amount, height, li=0):
    ev = normalize_event(
        {"tx_hash": tx, "log_index": li, "type": "deposit",
         "address": address, "amount": str(amount), "block_height": height}
    )
    return service.ingest_event(indexer, ev)


def test_concurrent_withdrawals_never_overdraw(env):
    s, alice, idx = env["service"], env["alice"], env["indexer"]
    rec = _fund(s, idx, "seed", "wA", 1000, 1)
    s.apply_confirmations(idx, [rec.event.natural_key], 3, 4, 3)

    n = 50
    results: list[bool] = []
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()
        try:
            s.withdraw(alice, "wA", 20, f"wd-{i}")
            results.append(True)
        except Exception:
            results.append(False)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = sum(results)
    bal = s.get_balance(alice, "wA")
    assert successes == 50  # 1000 / 20 恰好全部成功
    assert bal.available == 0
    assert bal.available >= 0  # 绝不为负
    # 每条成功流水唯一；无重复扣款
    outs = [e for e in s.list_entries(alice, "wA")
            if e.txn_type.value == "withdraw_out"]
    assert len(outs) == 50
    assert len({e.ref_key for e in outs}) == 50


def test_concurrent_duplicate_idempotency_single_effect(env):
    """同一幂等键并发提交：恰好一次生效。"""
    s, alice, idx = env["service"], env["alice"], env["indexer"]
    rec = _fund(s, idx, "seed", "wA", 100, 1)
    s.apply_confirmations(idx, [rec.event.natural_key], 3, 4, 3)

    n = 30
    statuses: list[str] = []
    barrier = threading.Barrier(n)
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            body = s.withdraw(alice, "wA", 10, "same-key")
            with lock:
                statuses.append("replay" if body.get("replay") else "ok")
        except Exception as exc:  # 任何冲突/余额错误都记录
            with lock:
                statuses.append(type(exc).__name__)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert statuses.count("ok") == 1
    assert statuses.count("replay") == n - 1
    assert s.get_balance(alice, "wA").available == 90  # 只扣一次


def test_concurrent_confirm_and_reorg_deterministic_nonnegative(env):
    """确认与回撤并发交错：最终余额唯一确定且不为负，不重复入账。"""
    s, idx = env["service"], env["indexer"]
    key = ("tbig", 0)
    _fund(s, idx, "tbig", "wA", 500, 10)

    errors: list[Exception] = []

    def confirm():
        try:
            s.apply_confirmations(idx, [key], 5, 15, 3)
        except Exception as exc:
            errors.append(exc)

    def reorg():
        try:
            s.apply_reorg(idx, [key], 15)
        except Exception as exc:
            errors.append(exc)

    # 反复并发跑多轮，每轮重建事件不可行（键唯一），因此单事件多线程交错。
    threads = [
        threading.Thread(target=confirm),
        threading.Thread(target=reorg),
        threading.Thread(target=confirm),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    bal = s.get_balance(env["alice"], "wA")
    record = s.repo.get_event(key)
    assert bal.available >= 0 and bal.pending_in >= 0
    # 确认至多入账一次
    confirms = [e for e in s.list_entries(env["alice"], "wA")
                if e.txn_type.value == "deposit_confirm"]
    assert len(confirms) <= 1
    # 若已回撤，则确认后到必须被忽略；状态只能是 reorged 或 confirmed 之一
    assert record.status.value in {"reorged", "confirmed"}
    if record.reorged:
        assert bal.available == 0 and bal.pending_in == 0


def test_concurrent_ingest_unique_natural_key(env):
    """并发投喂同一自然键：仅一条入账。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    n = 20
    barrier = threading.Barrier(n)
    accepted = {"n": 0}
    rejected = {"n": 0}
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            _fund(s, idx, "dup", "wA", 10, 1)
            with lock:
                accepted["n"] += 1
        except Exception:
            with lock:
                rejected["n"] += 1

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert accepted["n"] == 1 and rejected["n"] == n - 1
    assert s.get_balance(alice, "wA").pending_in == 10
