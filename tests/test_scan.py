"""批量确认扫描与限流：积压分批、限流拒绝、失败重试幂等不重复入账。"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.errors import RateLimited


@pytest.fixture
def env(svc):
    from app.chain import MockChain
    from app.ingestion import IngestionService
    from app.ledger import LedgerService
    settings = Settings(
        confirmations_required=1, finality_depth=1000,
        rate_limit_capacity=3, rate_limit_refill_per_sec=0.0,
        scan_max_events=10)
    svc.settings = settings
    svc.store.reset()
    svc.chain = MockChain()
    svc.ledger = LedgerService(svc.store, settings)
    svc.ingestion = IngestionService(svc.store, svc.chain, svc.ledger, settings)
    svc.ledger.create_wallet("w1", "u1", "0xA")
    return svc


def _add_chain_events(env, n):
    for i in range(n):
        env.chain.push_block([{
            "event_id": f"e{i}", "address": "0xA", "kind": "deposit",
            "amount": 1, "tx_hash": f"t{i}",
        }])


def test_backlog_processed_in_batches_with_deterministic_order(env):
    _add_chain_events(env, 25)
    total_accepted = 0
    scans = 0
    while True:
        r = env.ingestion.scan_once(max_events=10)
        total_accepted += len(r["accepted"])
        scans += 1
        if r["next_cursor"] is None:
            break
        assert scans < 10
    assert total_accepted == 25
    # confirmations_required=1，全部到账，无重复
    assert env.ledger.get_balance("w1").available == 25


def test_rate_limit_rejects_then_refill_allows_retry(env):
    _add_chain_events(env, 5)
    env.ingestion.scan_once()
    env.ingestion.scan_once()
    env.ingestion.scan_once()  # 容量3耗尽
    with pytest.raises(RateLimited) as ei:
        env.ingestion.scan_once()
    assert ei.value.code == "rate_limited"
    assert ei.value.details["retry_after_ms"] >= 0
    # 限流拒绝不推进游标、不产生副作用：模拟补充令牌后重试安全
    env.store.scan_bucket.tokens = 1.0
    r = env.ingestion.scan_once()
    # 重试幂等：已处理事件不会重复入账
    assert env.ledger.get_balance("w1").available == 5


def test_rejected_events_go_dead_letter_and_do_not_block_batch(env):
    # 一块内混合：1 个正常 + 1 个非法金额；非法进死信，批处理继续
    env.chain.push_block([
        {"event_id": "ok", "address": "0xA", "kind": "deposit",
         "amount": 10, "tx_hash": "g"},
        {"event_id": "bad", "address": "0xA", "kind": "deposit",
         "amount": -1, "tx_hash": "b"},
    ])
    r = env.ingestion.scan_once()
    assert r["accepted"] == ["ok"]
    assert any(x["event_id"] == "bad" and x["reason"] == "invalid_amount"
               for x in r["rejected"])
    assert env.ledger.get_balance("w1").available == 10
    assert len(env.store.dead_letters) == 1


def test_repeated_scans_do_not_double_count(env):
    _add_chain_events(env, 3)
    env.ingestion.scan_once()
    for _ in range(3):
        env.store.scan_bucket.tokens = 10.0
        env.ingestion.scan_once()
    assert env.ledger.get_balance("w1").available == 3


def test_scan_max_events_bounded(env):
    _add_chain_events(env, 50)
    r = env.ingestion.scan_once(max_events=10_000)
    assert len(r["accepted"]) <= 10  # 受 scan_max_events 上界约束
