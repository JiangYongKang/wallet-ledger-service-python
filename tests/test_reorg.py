"""链重组 / 事件回退：余额不为负、账目平衡、回撤与确认同时到达结论确定。"""

from __future__ import annotations

import pytest

from wallet_ledger.domain import LedgerTxnType
from wallet_ledger.normalizer import normalize_event


def _deposit(service, indexer, tx, address, amount, height, li=0):
    event = normalize_event(
        {"tx_hash": tx, "log_index": li, "type": "deposit",
         "address": address, "amount": str(amount), "block_height": height}
    )
    return service.ingest_event(indexer, event)


def _withdrawal(service, indexer, tx, address, amount, height, li=0):
    event = normalize_event(
        {"tx_hash": tx, "log_index": li, "type": "withdrawal",
         "address": address, "amount": str(amount), "block_height": height}
    )
    return service.ingest_event(indexer, event)


def test_reorg_unconfirmed_releases_pending_only(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    rec = _deposit(s, idx, "t1", "wA", 100, 10)
    s.apply_reorg(idx, [rec.event.natural_key], 10)
    bal = s.get_balance(alice, "wA")
    assert bal.available == 0 and bal.pending_in == 0 and bal.reorg_debt == 0


def test_reorg_confirmed_with_funds_claws_back(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    rec = _deposit(s, idx, "t1", "wA", 100, 10)
    s.apply_confirmations(idx, [rec.event.natural_key], 3, 13, 3)
    s.withdraw(alice, "wA", 40, "w-1")  # 花掉 40，剩 60
    s.apply_reorg(idx, [rec.event.natural_key], 13)
    bal = s.get_balance(alice, "wA")
    # 回撤 100，可用仅 60：扣到 0，缺口 40 平账，绝不为负
    assert bal.available == 0
    assert bal.reorg_debt == 40
    assert bal.pending_in == 0


def test_reorg_is_idempotent_and_deterministic(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    rec = _deposit(s, idx, "t1", "wA", 100, 10)
    s.apply_confirmations(idx, [rec.event.natural_key], 3, 13, 3)
    for _ in range(3):
        s.apply_reorg(idx, [rec.event.natural_key], 13)
    bal1 = s.get_balance(alice, "wA").to_dict()
    for _ in range(2):
        s.apply_reorg(idx, [rec.event.natural_key], 13)
    assert s.get_balance(alice, "wA").to_dict() == bal1
    # 回撤流水恰好一条
    clawbacks = [e for e in s.list_entries(alice, "wA")
                 if e.txn_type.value == "reorg_clawback"]
    assert len(clawbacks) == 1


def test_reorg_takes_precedence_over_confirmation_in_same_scan(env):
    """同一批确认与回撤都到达：回撤优先，确认被忽略，结论可复现。"""
    s, sc, idx, alice = env["service"], env["scanner"], env["indexer"], env["alice"]
    _deposit(s, idx, "t1", "wA", 100, 10)
    out1 = sc.scan_batch(
        idx, tip_height=13,
        events=[("t1", 0, 3)],
        reorg_keys=[("t1", 0)],
    )
    bal1 = s.get_balance(alice, "wA").to_dict()
    # 再跑一次完全相同批次：资金结论一致（操作回声可能因已应用而不同）
    sc.scan_batch(
        idx, tip_height=13,
        events=[("t1", 0, 3)],
        reorg_keys=[("t1", 0)],
    )
    bal2 = s.get_balance(alice, "wA").to_dict()
    assert out1["status"] == "ok"
    assert bal1 == bal2 == {
        "address": "wA", "available": 0, "pending_in": 0,
        "pending_out": 0, "reorg_debt": 0,
    }


def test_fund_conservation_under_reorg(env):
    """全局守恒：钱包余额 + 损失汇账户 = 净外部注入（充值-链上提现）。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    r1 = _deposit(s, idx, "t1", "wA", 100, 10)
    r2 = _deposit(s, idx, "t2", "wA", 50, 11)
    s.apply_confirmations(idx, [r1.event.natural_key, r2.event.natural_key], 3, 14, 3)
    s.transfer(alice, "wA", "wB", 70, "k1")  # 内部转账不改变总量
    s.apply_reorg(idx, [r1.event.natural_key], 14)  # 回撤 100，wA 剩 80
    total_wallets = (s.get_balance(alice, "wA").available
                     + s.get_balance(alice, "wB").available)
    debt = s.get_balance(alice, "wA").reorg_debt
    # 全局守恒：Σ可用 − Σ损失汇(平账吸收的无背书资金) = 净外部注入。
    # r1 被回撤后净注入仅剩 r2 的 50；内部转账不改变总量。
    assert debt == 20
    assert total_wallets - debt == 50


# ---- 链上转出事件被重组回退：恢复到转出发生之前，不虚增可用、不留残缺口 ----


def test_withdrawal_reorg_with_sufficient_funds_restores_available(env):
    """可用充足时的链上转出被回退：整笔退回可用，无缺口、无虚增。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    r = _deposit(s, idx, "d1", "wA", 100, 10)
    s.apply_confirmations(idx, [r.event.natural_key], 3, 13, 3)
    w = _withdrawal(s, idx, "w1", "wA", 40, 12)
    assert s.get_balance(alice, "wA").available == 60

    s.apply_reorg(idx, [w.event.natural_key], 13)
    bal = s.get_balance(alice, "wA")
    assert bal.available == 100
    assert bal.reorg_debt == 0
    # 流水自圆其说：退回 40，且没有缺口冲销流水
    entries = s.list_entries(alice, "wA")
    returned = [e for e in entries
                if e.txn_type is LedgerTxnType.REORG_CLAWBACK]
    assert [(e.address, e.amount) for e in returned] == [("wA", 40)]
    assert not [e for e in entries
                if e.txn_type is LedgerTxnType.REORG_SHORTFALL_REVERSAL]


def test_withdrawal_reorg_with_zero_balance_creates_no_money(env):
    """资产零余额时发生超额链上转出，回退后仍是 0/0，绝不凭空生钱。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    w = _withdrawal(s, idx, "w1", "wA", 100, 12)
    bal = s.get_balance(alice, "wA")
    assert bal.available == 0 and bal.reorg_debt == 100

    s.apply_reorg(idx, [w.event.natural_key], 13)
    bal = s.get_balance(alice, "wA")
    # 关键回归断言：旧实现会把整笔 100 加回 available，造成凭空 100 可花
    assert bal.available == 0
    assert bal.reorg_debt == 0
    assert bal.pending_in == 0

    # 回退后既不能转出也不能提现“凭空多出的钱”
    from wallet_ledger.errors import InsufficientFundsError
    with pytest.raises(InsufficientFundsError):
        s.withdraw(alice, "wA", 1, "off1")
    with pytest.raises(InsufficientFundsError):
        s.transfer(alice, "wA", "wB", 1, "t1")


def test_withdrawal_reorg_with_shortfall_restores_pre_withdrawal_state(env):
    """可用不足（非零）时的链上转出被回退：只退实扣、冲销缺口，恢复转出前状态。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    r = _deposit(s, idx, "d1", "wA", 100, 10)
    s.apply_confirmations(idx, [r.event.natural_key], 3, 13, 3)
    s.withdraw(alice, "wA", 70, "off1")  # 链外已花，链上可用 30
    w = _withdrawal(s, idx, "w1", "wA", 100, 12)  # 实扣 30，缺口 70
    assert (s.get_balance(alice, "wA").available,
            s.get_balance(alice, "wA").reorg_debt) == (0, 70)

    s.apply_reorg(idx, [w.event.natural_key], 13)
    bal = s.get_balance(alice, "wA")
    # 恢复到这笔链上转出发生之前：可用 30，缺口 0（旧实现会虚增到 100）
    assert bal.available == 30
    assert bal.reorg_debt == 0

    # 分口径流水自圆其说：扣款 100（毛额）、损失汇平账 70、
    # 回撤退回 30、损失汇冲销 70，彼此对应、可解释
    entries = s.list_entries(alice, "wA")
    sink = s.repo.list_entries(address="@loss-sink")
    assert [(e.txn_type, e.amount) for e in sink] == [
        (LedgerTxnType.REORG_CLAWBACK, 70),  # 转出时缺口平账
        (LedgerTxnType.REORG_SHORTFALL_REVERSAL, 70),  # 回撤时冲销
    ]
    clawbacks = [e for e in entries
                 if e.txn_type is LedgerTxnType.REORG_CLAWBACK]
    assert [(e.address, e.amount) for e in clawbacks] == [("wA", 30)]
    assert not [e for e in entries
                if e.txn_type is LedgerTxnType.REORG_SHORTFALL_REVERSAL]


def test_withdrawal_reorg_is_idempotent_and_later_confirmation_ignored(env):
    """同一链上转出重复回退结论不变；回退后再收到确认仍被忽略，不二次入账。"""
    s, sc, idx, alice = (env["service"], env["scanner"],
                         env["indexer"], env["alice"])
    w = _withdrawal(s, idx, "w1", "wA", 100, 12)  # 零余额，全额缺口
    for _ in range(3):
        s.apply_reorg(idx, [w.event.natural_key], 13)
    bal1 = s.get_balance(alice, "wA").to_dict()
    assert bal1["available"] == 0 and bal1["reorg_debt"] == 0

    # 回退后确认（哪怕确认数很大）一律忽略：不重复退款、不动账
    s.apply_confirmations(idx, [w.event.natural_key], 100, 200, 3, 64)
    assert s.get_balance(alice, "wA").to_dict() == bal1

    # 同批“回退 + 确认”再次到达，结论仍唯一可复现
    out = sc.scan_batch(
        idx, tip_height=14,
        events=[("w1", 0, 100)],
        reorg_keys=[("w1", 0)],
    )
    assert out["status"] == "ok"
    assert s.get_balance(alice, "wA").to_dict() == bal1

    # 回撤相关流水各自恰好一条（幂等闸门，不重复追加）
    ref = "w1:0"
    sink = [e for e in s.repo.list_entries(address="@loss-sink")
            if e.ref_key == ref]
    assert [(e.txn_type, e.amount) for e in sink] == [
        (LedgerTxnType.REORG_CLAWBACK, 100),
        (LedgerTxnType.REORG_SHORTFALL_REVERSAL, 100),
    ]
    rets = [e for e in s.list_entries(alice, "wA")
            if e.txn_type is LedgerTxnType.REORG_CLAWBACK
            and e.ref_key == ref]
    assert [(e.address, e.amount) for e in rets] == [("wA", 0)]


def test_withdrawal_reorg_and_confirmation_in_same_scan_deterministic(env):
    """同一批里回退与确认同时到达：先回退后确认（确认被忽略），结论可复现。"""
    s, sc, idx, alice = env["service"], env["scanner"], env["indexer"], env["alice"]
    w = _withdrawal(s, idx, "w1", "wA", 100, 12)  # 零余额超额转出
    key = w.event.natural_key
    out = sc.scan_batch(
        idx, tip_height=13,
        events=[(*key, 5)],
        reorg_keys=[key],
    )
    bal1 = s.get_balance(alice, "wA").to_dict()
    # 再跑完全相同批次，结论一致
    sc.scan_batch(
        idx, tip_height=13,
        events=[(*key, 5)],
        reorg_keys=[key],
    )
    bal2 = s.get_balance(alice, "wA").to_dict()
    assert out["status"] == "ok"
    assert bal1 == bal2 == {
        "address": "wA", "available": 0, "pending_in": 0,
        "pending_out": 0, "reorg_debt": 0,
    }

