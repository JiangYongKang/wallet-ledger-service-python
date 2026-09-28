"""链重组 / 事件回退：余额不为负、账目平衡、回撤与确认同时到达结论确定。"""

from __future__ import annotations

import pytest

from wallet_ledger.errors import InsufficientFundsError
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


# ---- 链上转出（提现）事件被链重组回撤：恢复到转出发生之前 ----


def _withdrawal(service, indexer, tx, address, amount, height, li=0, asset=None):
    payload = {"tx_hash": tx, "log_index": li, "type": "withdrawal",
               "address": address, "amount": str(amount), "block_height": height}
    if asset is not None:
        payload["asset"] = asset
    event = normalize_event(payload)
    return service.ingest_event(indexer, event)


def _clawbacks(service, address=None, asset=None):
    return [e for e in service.repo.list_entries(address=address, asset=asset)
            if e.txn_type.value == "reorg_clawback"]


def test_withdrawal_reorg_with_sufficient_funds_restores_state(env):
    """可用充足时的链上转出被回撤：整笔退回可用，无缺口残留。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    rec = _deposit(s, idx, "t1", "wA", 100, 10)
    s.apply_confirmations(idx, [rec.event.natural_key], 3, 13, 3)
    w = _withdrawal(s, idx, "w1", "wA", 40, 12)
    assert s.get_balance(alice, "wA").available == 60

    s.apply_reorg(idx, [w.event.natural_key], 13)

    bal = s.get_balance(alice, "wA")
    # 恢复到转出之前：available=100，没有任何 reorg_debt
    assert (bal.available, bal.pending_in, bal.reorg_debt) == (100, 0, 0)


def test_withdrawal_reorg_with_shortfall_does_not_inflate_available(env):
    """链上转出金额超过当时可用（含部分不足）：回撤只退实际扣掉的部分，
    缺口平账同步冲销，可用余额不得被虚增。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    rec = _deposit(s, idx, "t1", "wA", 100, 10)
    s.apply_confirmations(idx, [rec.event.natural_key], 3, 13, 3)
    s.withdraw(alice, "wA", 70, "off-chain")  # 链外已花，链上可用剩 30
    w = _withdrawal(s, idx, "w1", "wA", 100, 12)  # 实际扣 30，缺口 70
    bal = s.get_balance(alice, "wA")
    assert (bal.available, bal.reorg_debt) == (0, 70)

    s.apply_reorg(idx, [w.event.natural_key], 13)

    bal = s.get_balance(alice, "wA")
    # 只退回真正扣掉的 30；多记的 70 缺口被冲销，绝不能把 100 全加回来
    assert (bal.available, bal.pending_in, bal.reorg_debt) == (30, 0, 0)

    # 损失汇账户上该资产的平账流水净额为 0（+70 缺口 / -70 冲销）
    sink = [e for e in s.repo.list_entries()
            if e.address == "@loss-sink" and e.asset == bal.asset]
    booked = sum(e.amount for e in sink if "booked" in e.memo)
    reversed_ = sum(e.amount for e in sink if "reversed" in e.memo)
    assert booked == reversed_ == 70


def test_withdrawal_reorg_with_zero_balance_restores_zero(env):
    """该资产根本没有余额时发生的链上转出被回撤：依旧一分钱都没有。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    w = _withdrawal(s, idx, "w1", "wA", 100, 12)  # 零余额：实际扣 0，缺口 100
    assert (s.get_balance(alice, "wA").available,
            s.get_balance(alice, "wA").reorg_debt) == (0, 100)

    s.apply_reorg(idx, [w.event.natural_key], 13)

    bal = s.get_balance(alice, "wA")
    # 回退后不能凭空多出可花的钱
    assert (bal.available, bal.pending_in, bal.reorg_debt) == (0, 0, 0)
    # 账面对平：损失汇缺口已冲销，用户地址没有凭空的退款流水
    address_clawbacks = _clawbacks(s, address="wA")
    assert address_clawbacks == []


def test_withdrawal_reorg_then_user_cannot_spend_phantom_funds(env):
    """零余额链上转出被回撤后，不能继续转出或提现凭空多出的钱。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    w = _withdrawal(s, idx, "w1", "wA", 100, 12)
    s.apply_reorg(idx, [w.event.natural_key], 13)

    with pytest.raises(InsufficientFundsError):
        s.withdraw(alice, "wA", 1, "try-phantom")
    with pytest.raises(InsufficientFundsError):
        s.transfer(alice, "wA", "wB", 1, "try-phantom-tx")
    assert s.get_balance(alice, "wA").available == 0


def test_withdrawal_reorg_is_idempotent_and_reconfirm_ignored(env):
    """同一事件重复回撤结论不变；回撤之后再到的确认一律忽略。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    w = _withdrawal(s, idx, "w1", "wA", 100, 12)  # 零余额：缺口 100
    for _ in range(3):
        s.apply_reorg(idx, [w.event.natural_key], 13)

    bal = s.get_balance(alice, "wA").to_dict()
    assert bal == {"address": "wA", "available": 0, "pending_in": 0,
                   "pending_out": 0, "reorg_debt": 0}

    # 回撤之后确认到达：必须被忽略，不能把钱"确认"回可用余额
    s.apply_confirmations(idx, [w.event.natural_key], 64, 76, 3, 10)
    # 再来一次重复回撤，结论仍然完全相同
    s.apply_reorg(idx, [w.event.natural_key], 14)
    assert s.get_balance(alice, "wA").to_dict() == bal

    # 损失汇账户上该资产：一笔缺口登记 + 一笔等额冲销，净额为 0；
    # 冲销恰好一次（重复回撤不产生重复冲销流水）
    sink = [e for e in _clawbacks(s) if e.address == "@loss-sink"]
    booked = [e for e in sink if "booked" in e.memo]
    reversed_ = [e for e in sink if "reversed" in e.memo]
    assert len(booked) == 1 and booked[0].amount == 100
    assert len(reversed_) == 1 and reversed_[0].amount == 100


def test_withdrawal_reorg_and_confirm_in_same_scan_reorg_first(env):
    """同一批里回撤与确认同时到达（链上转出事件）：先回撤，确认被忽略。"""
    s, sc, idx, alice = env["service"], env["scanner"], env["indexer"], env["alice"]
    _withdrawal(s, idx, "w1", "wA", 100, 12)  # 零余额：缺口 100
    out = sc.scan_batch(
        idx, tip_height=13,
        events=[("w1", 0, 3)],
        reorg_keys=[("w1", 0)],
    )
    bal1 = s.get_balance(alice, "wA").to_dict()
    assert out["status"] == "ok"
    assert bal1 == {"address": "wA", "available": 0, "pending_in": 0,
                   "pending_out": 0, "reorg_debt": 0}
    # 同批再跑一遍：结论唯一可复现
    sc.scan_batch(
        idx, tip_height=13,
        events=[("w1", 0, 3)],
        reorg_keys=[("w1", 0)],
    )
    assert s.get_balance(alice, "wA").to_dict() == bal1


def test_withdrawal_reorg_entries_tell_a_balanced_story(env):
    """分口径余额与流水自圆其说：流水能重放出回退后的 available/reorg_debt。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    rec = _deposit(s, idx, "t1", "wA", 100, 10)
    s.apply_confirmations(idx, [rec.event.natural_key], 3, 13, 3)
    w = _withdrawal(s, idx, "w1", "wA", 100, 12)  # 扣 100，缺口 0
    w2 = _withdrawal(s, idx, "w2", "wA", 50, 13)  # 扣 0，缺口 50
    assert (s.get_balance(alice, "wA").available,
            s.get_balance(alice, "wA").reorg_debt) == (0, 50)

    # 只回撤第二笔（零实扣的那笔）：状态不变，但缺口被冲销
    s.apply_reorg(idx, [w2.event.natural_key], 14)
    bal = s.get_balance(alice, "wA")
    assert (bal.available, bal.reorg_debt) == (0, 0)

    # 再回撤第一笔：退回实扣的 100
    s.apply_reorg(idx, [w.event.natural_key], 14)
    bal = s.get_balance(alice, "wA")
    assert (bal.available, bal.reorg_debt) == (100, 0)

    # 流水自圆其说：被回撤的链上转出应“当作从未发生”——其全量 withdraw_out
    # 流水与对应的损失汇记缺口流水整组剔除；未回撤事件按守恒口径重放：
    # 确认入账为正、链上转出全量为负、未冲销缺口为正（损失汇吸收），
    # 应精确得到 available − reorg_debt。
    reorged_refs = {
        f"{r.event.tx_hash}:{r.event.log_index}"
        for r in s.repo.all_event_records() if r.reorged
    }
    net_effect = 0
    for e in s.repo.list_entries():
        if e.asset != bal.asset or e.ref_key in reorged_refs:
            continue  # 已回撤事件的全部流水（含损失汇平账）整组冲销
        if e.txn_type.value == "deposit_confirm":
            net_effect += e.amount
        elif e.txn_type.value == "withdraw_out":
            net_effect -= e.amount
        elif (e.txn_type.value == "reorg_clawback"
              and e.address == "@loss-sink" and "booked" in e.memo):
            net_effect += e.amount
    assert net_effect == bal.available - bal.reorg_debt == 100
    # 两笔回撤中只有 w2 当初有 50 的缺口需要从损失汇冲销（w1 缺口为 0）
    reversals = [e for e in s.repo.list_entries()
                 if (e.txn_type.value == "reorg_clawback"
                     and e.address == "@loss-sink" and "reversed" in e.memo
                     and e.asset == bal.asset)]
    assert len(reversals) == 1 and reversals[0].amount == 50
    # reorg_debt 口径：记缺口总额 − 冲缺口总额
    booked = sum(e.amount for e in s.repo.list_entries()
                 if e.address == "@loss-sink" and "booked" in e.memo
                 and e.asset == bal.asset)
    reversed_ = sum(e.amount for e in reversals)
    assert booked == reversed_ == 50
    assert booked - reversed_ == bal.reorg_debt == 0
