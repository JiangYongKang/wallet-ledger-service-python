"""链重组 / 事件回退：余额不为负、账目平衡、回撤与确认同时到达结论确定。"""

from __future__ import annotations

from wallet_ledger.normalizer import normalize_event


def _deposit(service, indexer, tx, address, amount, height, li=0):
    event = normalize_event(
        {"tx_hash": tx, "log_index": li, "type": "deposit",
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
