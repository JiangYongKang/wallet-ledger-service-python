"""幂等：重复提交不重复扣款/入账；等价重复与语义冲突可区分。"""

from __future__ import annotations

import pytest

from wallet_ledger.errors import IdempotencyConflictError
from wallet_ledger.normalizer import normalize_event


def test_transfer_duplicate_does_not_double_debit(env):
    s, alice = env["service"], env["alice"]
    # 先充值并确认 100
    idx = env["indexer"]
    ev = normalize_event({"tx_hash": "t1", "log_index": 0, "type": "deposit",
                          "address": "wA", "amount": "100", "block_height": 1})
    rec = s.ingest_event(idx, ev)
    s.apply_confirmations(idx, [rec.event.natural_key], 3, 4, 3)

    body1 = s.transfer(alice, "wA", "wB", 30, "key-1")
    assert body1.get("replay") is None
    body2 = s.transfer(alice, "wA", "wB", 30, "key-1")
    assert body2.get("replay") is True  # 等价重复：可区分标记
    # 只扣了一次
    assert s.get_balance(alice, "wA").available == 70
    assert s.get_balance(alice, "wB").available == 30


def test_same_key_different_body_is_conflict(env):
    s, alice, idx = env["service"], env["alice"], env["indexer"]
    ev = normalize_event({"tx_hash": "tk", "log_index": 0, "type": "deposit",
                          "address": "wA", "amount": "100", "block_height": 1})
    rec = s.ingest_event(idx, ev)
    s.apply_confirmations(idx, [rec.event.natural_key], 3, 4, 3)
    s.transfer(alice, "wA", "wB", 10, "key-x")
    with pytest.raises(IdempotencyConflictError) as ei:
        s.transfer(alice, "wA", "wB", 11, "key-x")
    assert ei.value.code == "IDEMPOTENCY_CONFLICT"


def test_withdraw_duplicate_does_not_double_debit(env):
    s, alice = env["service"], env["alice"]
    idx = env["indexer"]
    ev = normalize_event({"tx_hash": "t1", "log_index": 0, "type": "deposit",
                          "address": "wA", "amount": "50", "block_height": 1})
    rec = s.ingest_event(idx, ev)
    s.apply_confirmations(idx, [rec.event.natural_key], 3, 4, 3)

    s.withdraw(alice, "wA", 20, "wd-1")
    replay = s.withdraw(alice, "wA", 20, "wd-1")
    assert replay.get("replay") is True
    assert s.get_balance(alice, "wA").available == 30


def test_idempotency_keys_isolated_per_principal(env):
    """不同主体使用相同 key 字符串互不影响。"""
    s, alice, bob, idx = (
        env["service"], env["alice"], env["bob"], env["indexer"])
    # 给 alice 与 bob 各自充值
    for who, addr, tx in [(alice, "wA", "tA"), (bob, "wC", "tC")]:
        ev = normalize_event({"tx_hash": tx, "log_index": 0, "type": "deposit",
                              "address": addr, "amount": "10", "block_height": 1})
        rec = s.ingest_event(idx, ev)
        s.apply_confirmations(idx, [rec.event.natural_key], 3, 4, 3)

    s.transfer(alice, "wA", "wB", 1, "shared")
    # bob 用完全相同的 key 字符串：键空间隔离，应作为新操作成功而非冲突。
    body = s.transfer(bob, "wC", "wC", 1, "shared")
    assert body["status"] == "ok" and body.get("replay") is None


def test_duplicate_chain_event_rejected_distinct_code(env):
    s, idx = env["service"], env["indexer"]
    payload = {"tx_hash": "t1", "log_index": 0, "type": "deposit",
               "address": "wA", "amount": "10", "block_height": 1}
    s.ingest_event(idx, normalize_event(payload))
    from wallet_ledger.errors import DuplicateEventError
    with pytest.raises(DuplicateEventError) as ei:
        s.ingest_event(idx, normalize_event(payload))
    assert ei.value.code == "DUPLICATE_EVENT"
    # 重复投喂不重复增加待入账
    assert s.get_balance(env["alice"], "wA").pending_in == 10
