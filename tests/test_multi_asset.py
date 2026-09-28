"""多资产（多币种）支持：各资产独立记账、独立守恒、回退/幂等/并发隔离。

验证重点：
- 充值确认、链上转出/回退、内部转账、提现都只作用于请求指定的资产；
- 余额查询能一次看到每个资产各自的分口径余额；
- 回退只平本资产的账，不会让任一资产负余额或对不平；
- 同键不同资产 = 语义冲突；同键同资产重复 = 安全回放；
- 不带资产的旧请求落到默认资产 COIN，行为不变；
- 同一钱包不同资产并发互不干扰。
"""

from __future__ import annotations

import threading

import pytest

from wallet_ledger.domain import DEFAULT_ASSET
from wallet_ledger.errors import (
    IdempotencyConflictError,
    InsufficientFundsError,
    InvalidAssetError,
)
from wallet_ledger.normalizer import normalize_event

BTC = "BTC"
ETH = "ETH"


def _event(tx, address, amount, height, *, li=0, type_="deposit", asset=DEFAULT_ASSET):
    return normalize_event(
        {"tx_hash": tx, "log_index": li, "type": type_,
         "address": address, "amount": str(amount), "block_height": height,
         "asset": asset}
    )


def _deposit(s, idx, tx, address, amount, height, *, li=0, asset=DEFAULT_ASSET):
    return s.ingest_event(idx, _event(tx, address, amount, height,
                                      li=li, asset=asset))


def _confirm(s, idx, rec, conf=3, height=13, required=3, depth=64):
    s.apply_confirmations(idx, [rec.event.natural_key], conf, height,
                          required, depth)


def test_deposit_and_confirm_book_to_specified_asset_only(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    r_btc = _deposit(s, idx, "tb", "wA", 100, 10, asset=BTC)
    r_eth = _deposit(s, idx, "te", "wA", 50, 10, asset=ETH)
    _confirm(s, idx, r_btc)
    _confirm(s, idx, r_eth)

    btc = s.get_balance(alice, "wA", BTC)
    eth = s.get_balance(alice, "wA", ETH)
    assert btc.available == 100 and btc.pending_in == 0
    assert eth.available == 50 and eth.pending_in == 0

    # 默认资产没有发生任何资金活动
    coin = s.get_balance(alice, "wA")
    assert (coin.available, coin.pending_in, coin.reorg_debt) == (0, 0, 0)


def test_pending_deposits_are_separated_per_asset(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    _deposit(s, idx, "tb", "wA", 100, 10, asset=BTC)
    _deposit(s, idx, "te", "wA", 70, 10, asset=ETH)

    view = s.get_wallet_balance(alice, "wA")
    assert view.assets[BTC].pending_in == 100
    assert view.assets[ETH].pending_in == 70
    assert DEFAULT_ASSET in view.assets  # 建钱包即存在的默认资产桶
    assert view.assets[DEFAULT_ASSET].pending_in == 0


def test_transfer_moves_only_specified_asset(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    rb = _deposit(s, idx, "tb", "wA", 100, 10, asset=BTC)
    re_ = _deposit(s, idx, "te", "wA", 40, 10, asset=ETH)
    _confirm(s, idx, rb)
    _confirm(s, idx, re_)

    s.transfer(alice, "wA", "wB", 60, "t-btc", asset=BTC)

    assert s.get_balance(alice, "wA", BTC).available == 40
    assert s.get_balance(alice, "wB", BTC).available == 60
    # ETH 未参与转账
    assert s.get_balance(alice, "wA", ETH).available == 40
    assert s.get_balance(alice, "wB", ETH).available == 0


def test_insufficient_funds_does_not_cross_deduct_other_asset(env):
    """某资产余额不足不能拿另一资产抵扣：显式 INSUFFICIENT_FUNDS 且无副作用。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    rb = _deposit(s, idx, "tb", "wA", 100, 10, asset=BTC)
    _confirm(s, idx, rb)

    with pytest.raises(InsufficientFundsError) as exc:
        s.transfer(alice, "wA", "wB", 1, "k", asset=ETH)
    assert exc.value.details["asset"] == ETH

    # 被拒操作没有创建任何脏流水、没有动用 BTC
    assert s.get_balance(alice, "wA", BTC).available == 100
    assert s.get_balance(alice, "wA", ETH).available == 0
    assert s.get_balance(alice, "wB", ETH).available == 0


# ---- 回退与守恒：每个资产独立成立 ----


# ---- 回退与守恒：每个资产独立成立 ----


def _per_asset_net_input(s, addresses, asset):
    """从流水重放计算某资产在给定钱包集合上的净外部注入（充值确认 - 链上提现），
    用于和“Σ可用 − Σreorg_debt”对账。"""
    confirmed_deposits = withdrawn = 0
    for e in s.repo.list_entries():
        if e.asset != asset or e.address not in addresses:
            continue
        if e.txn_type.value == "deposit_confirm":
            confirmed_deposits += e.amount
        if e.txn_type.value == "withdraw_out":
            # 链上转出（含缺口平账那笔）以实际 amount 计外部流出
            confirmed_deposits -= 0
            withdrawn += e.amount
    return confirmed_deposits - withdrawn


def _assert_asset_conservation(s, addresses, asset):
    total_available = sum(
        s.get_balance(_pr(s, a), a, asset).available for a in addresses
    ) if False else None
    return total_available  # 占位（实际在各用例内直接断言）


def _pr(s, address):  # 授权主体：测试中 alice 拥有 wA/wB，bob 拥有 wC
    return {"wA": None, "wB": None, "wC": None}[address]


def test_reorg_confirmed_deposit_isolated_per_asset(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    rb = _deposit(s, idx, "tb", "wA", 100, 10, asset=BTC)
    re_ = _deposit(s, idx, "te", "wA", 100, 10, asset=ETH)
    _confirm(s, idx, rb)
    _confirm(s, idx, re_)
    s.withdraw(alice, "wA", 100, "spend-btc", asset=BTC)  # 花光 BTC

    s.apply_reorg(idx, [rb.event.natural_key], 13)  # 只回撤 BTC

    btc, eth = s.get_balance(alice, "wA", BTC), s.get_balance(alice, "wA", ETH)
    assert btc.available == 0 and btc.reorg_debt == 100
    assert eth.available == 100 and eth.reorg_debt == 0
    # loss-sink 平账流水也只打在 BTC 上
    sink = [e for e in s.repo.list_entries()
            if e.address == "@loss-sink" and e.asset == BTC]
    assert sum(e.amount for e in sink) == 100
    assert not [e for e in s.repo.list_entries()
                if e.address == "@loss-sink" and e.asset == ETH]


def test_reorg_unconfirmed_only_releases_same_asset_pending(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    rb = _deposit(s, idx, "tb", "wA", 100, 10, asset=BTC)
    _deposit(s, idx, "te", "wA", 30, 10, asset=ETH)

    s.apply_reorg(idx, [rb.event.natural_key], 10)

    btc, eth = s.get_balance(alice, "wA", BTC), s.get_balance(alice, "wA", ETH)
    assert btc.pending_in == 0 and eth.pending_in == 30


def test_per_asset_conservation_under_mixed_ops(env):
    """混合资产操作后，每个资产分别满足 Σavailable − Σreorg_debt = 净外部注入。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    # BTC: 确认 200，内部转出 70，回撤 100（其中 30 已被转出 -> 缺口）
    b1 = _deposit(s, idx, "b1", "wA", 100, 10, li=0, asset=BTC)
    b2 = _deposit(s, idx, "b2", "wA", 100, 11, li=0, asset=BTC)
    # ETH: 确认 80
    e1 = _deposit(s, idx, "e1", "wA", 80, 10, li=0, asset=ETH)
    for r in (b1, b2, e1):
        _confirm(s, idx, r)
    s.transfer(alice, "wA", "wB", 70, "btc-move", asset=BTC)
    s.apply_reorg(idx, [b1.event.natural_key], 14)

    addrs = ["wA", "wB", "wC"]
    for asset, net_expected in ((BTC, 100), (ETH, 80)):
        avail = sum(s.repo.get_balance(a, asset).available
                    for a in addrs if s.repo.get_balance(a, asset))
        debt = sum((s.repo.get_balance(a, asset).reorg_debt if s.repo.get_balance(a, asset) else 0)
                   for a in addrs)
        assert avail - debt == net_expected, asset


def test_onchain_withdrawal_event_debits_specified_asset_and_reorg_returns(env):
    """链上转出事件只扣指定资产；其被重组回撤时钱只回该资产。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    rb = _deposit(s, idx, "tb", "wA", 100, 10, asset=BTC)
    re_ = _deposit(s, idx, "te", "wA", 100, 10, asset=ETH)
    _confirm(s, idx, rb)
    _confirm(s, idx, re_)
    w = s.ingest_event(idx, _event("wx", "wA", 40, 12, type_="withdrawal", asset=BTC))

    assert s.get_balance(alice, "wA", BTC).available == 60
    assert s.get_balance(alice, "wA", ETH).available == 100

    s.apply_reorg(idx, [w.event.natural_key], 13)
    assert s.get_balance(alice, "wA", BTC).available == 100
    assert s.get_balance(alice, "wA", ETH).available == 100


def test_onchain_withdrawal_shortfall_booked_on_same_asset(env):
    """链上转出超过该资产可用：钳 0 + 本资产 reorg_debt，不碰其他资产。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    rb = _deposit(s, idx, "tb", "wA", 100, 10, asset=BTC)
    _confirm(s, idx, rb)
    s.withdraw(alice, "wA", 70, "off", asset=BTC)  # 链外已花，链上可用 30
    s.ingest_event(idx, _event("wx", "wA", 100, 12, type_="withdrawal", asset=BTC))

    btc = s.get_balance(alice, "wA", BTC)
    assert btc.available == 0 and btc.reorg_debt == 70
    coin = s.get_balance(alice, "wA")
    assert (coin.available, coin.reorg_debt) == (0, 0)


def test_onchain_withdrawal_shortfall_reorg_restores_per_asset(env):
    """不足额（含零余额）链上转出被回撤：只回本资产实扣部分、冲本资产缺口，
    不碰其他资产，也不凭空抬高可用。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    rb = _deposit(s, idx, "tb", "wA", 100, 10, asset=BTC)
    re_ = _deposit(s, idx, "te", "wA", 100, 10, asset=ETH)
    _confirm(s, idx, rb)
    _confirm(s, idx, re_)
    s.withdraw(alice, "wA", 70, "off", asset=BTC)  # BTC 可用剩 30
    # BTC：转出 100，实扣 30、缺口 70；COIN：零余额转出 60，实扣 0、缺口 60
    w_btc = s.ingest_event(
        idx, _event("wbtc", "wA", 100, 12, type_="withdrawal", asset=BTC))
    w_coin = s.ingest_event(
        idx, _event("wcoin", "wA", 60, 12, type_="withdrawal", asset=DEFAULT_ASSET))

    assert (s.get_balance(alice, "wA", BTC).available,
            s.get_balance(alice, "wA", BTC).reorg_debt) == (0, 70)
    assert (s.get_balance(alice, "wA").available,
            s.get_balance(alice, "wA").reorg_debt) == (0, 60)
    assert s.get_balance(alice, "wA", ETH).available == 100

    # 只回撤 BTC 的转出：退回实扣 30、冲掉 BTC 缺口 70；ETH/COIN 完全不动
    s.apply_reorg(idx, [w_btc.event.natural_key], 13)
    btc, eth, coin = (s.get_balance(alice, "wA", BTC),
                      s.get_balance(alice, "wA", ETH),
                      s.get_balance(alice, "wA"))
    assert (btc.available, btc.pending_in, btc.reorg_debt) == (30, 0, 0)
    assert (eth.available, eth.reorg_debt) == (100, 0)
    assert (coin.available, coin.reorg_debt) == (0, 60)
    # 损失汇冲销流水只打在 BTC 上
    assert [e for e in s.repo.list_entries()
            if e.address == "@loss-sink" and "reversed" in e.memo
            and e.asset == BTC]
    assert not [e for e in s.repo.list_entries()
                if e.address == "@loss-sink" and "reversed" in e.memo
                and e.asset in (ETH, DEFAULT_ASSET)]

    # 再回撤零余额的 COIN 转出：仍然一分钱都没有，缺口冲销；BTC/ETH 不变
    s.apply_reorg(idx, [w_coin.event.natural_key], 13)
    coin = s.get_balance(alice, "wA")
    assert (coin.available, coin.reorg_debt) == (0, 0)
    btc, eth = s.get_balance(alice, "wA", BTC), s.get_balance(alice, "wA", ETH)
    assert btc.available == 30 and eth.available == 100

    # 逐资产守恒：available − reorg_debt == 净外部注入（按未回撤事件计算）
    # BTC：确认 100、链外提现 70、链上转出已回撤（视为未发生）=> 30
    # ETH：确认 100 => 100；COIN：无任何未回撤事件 => 0
    assert btc.available - btc.reorg_debt == 30
    assert eth.available - eth.reorg_debt == 100
    assert coin.available - coin.reorg_debt == 0


def test_onchain_withdrawal_reorg_same_batch_confirm_isolated_per_asset(env):
    """同批回撤+确认到达链上转出：回撤优先、结论确定，且只作用本资产。"""
    s, sc, idx, alice = (env["service"], env["scanner"],
                         env["indexer"], env["alice"])
    # COIN 零余额链上转出 80（缺口 80）；BTC 充值待确认 50
    s.ingest_event(idx, _event("wc", "wA", 80, 12, type_="withdrawal"))
    _deposit(s, idx, "tb", "wA", 50, 11, asset=BTC)

    out = sc.scan_batch(
        idx, tip_height=13,
        events=[("wc", 0, 3), ("tb", 0, 3)],
        reorg_keys=[("wc", 0)],
    )
    assert out["status"] == "ok"
    coin = s.get_balance(alice, "wA")
    btc = s.get_balance(alice, "wA", BTC)
    # COIN 转出已回撤：零余额、零缺口，确认被忽略；BTC 同批正常确认
    assert (coin.available, coin.pending_in, coin.reorg_debt) == (0, 0, 0)
    assert (btc.available, btc.pending_in) == (50, 0)

    # 整批重放：结论唯一可复现
    sc.scan_batch(
        idx, tip_height=13,
        events=[("wc", 0, 3), ("tb", 0, 3)],
        reorg_keys=[("wc", 0)],
    )
    coin2 = s.get_balance(alice, "wA")
    btc2 = s.get_balance(alice, "wA", BTC)
    assert (coin2.available, coin2.reorg_debt) == (0, 0)
    assert (btc2.available, btc2.pending_in) == (50, 0)
    # 回撤后的 COIN 地址上不能凭空提现
    with pytest.raises(InsufficientFundsError):
        s.withdraw(alice, "wA", 1, "phantom-coin")


# ---- 幂等：重复 vs 语义冲突在资产维度上仍可区分 ----


def test_same_idem_key_different_asset_is_conflict(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    for asset in (BTC, ETH):
        r = _deposit(s, idx, f"t-{asset}", "wA", 100, 10, asset=asset)
        _confirm(s, idx, r)

    s.transfer(alice, "wA", "wB", 10, "shared", asset=BTC)
    with pytest.raises(IdempotencyConflictError):
        s.transfer(alice, "wA", "wB", 10, "shared", asset=ETH)

    # 冲突不产生 ETH 落账，BTC 只扣一次
    assert s.get_balance(alice, "wA", BTC).available == 90
    assert s.get_balance(alice, "wB", BTC).available == 10
    assert s.get_balance(alice, "wA", ETH).available == 100
    assert s.get_balance(alice, "wB", ETH).available == 0


def test_same_idem_key_same_asset_replays_without_double_effect(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    r = _deposit(s, idx, "tb", "wA", 100, 10, asset=BTC)
    _confirm(s, idx, r)

    s.withdraw(alice, "wA", 20, "wd", asset=BTC)
    b2 = s.withdraw(alice, "wA", 20, "wd", asset="btc")  # 大小写归一为同一资产
    assert b2["replay"] is True
    assert s.get_balance(alice, "wA", BTC).available == 80


def test_repeated_confirmations_are_idempotent_per_asset(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    r = _deposit(s, idx, "tb", "wA", 100, 10, asset=BTC)
    _confirm(s, idx, r)
    _confirm(s, idx, r)  # 重复扫描
    bal = s.get_balance(alice, "wA", BTC)
    assert bal.available == 100 and bal.pending_in == 0
    confirms = [e for e in s.list_entries(alice, "wA", BTC)
                if e.txn_type.value == "deposit_confirm"]
    assert len(confirms) == 1


def test_duplicate_natural_key_even_with_other_asset_rejected(env):
    """自然键仍是 (tx_hash, log_index)：重复投喂（即便换资产）也不二次入账。"""
    from wallet_ledger.errors import DuplicateEventError
    s, idx = env["service"], env["indexer"]
    s.ingest_event(idx, _event("t1", "wA", 100, 10, asset=BTC))
    with pytest.raises(DuplicateEventError):
        s.ingest_event(idx, _event("t1", "wA", 100, 10, asset=ETH))
    assert s.get_balance(env["alice"], "wA", BTC).pending_in == 100
    assert s.get_balance(env["alice"], "wA", ETH).pending_in == 0


# ---- 兼容：不带资产的旧请求落到默认资产 ----


def test_legacy_requests_without_asset_hit_default_asset(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    # 旧形态事件（无 asset 字段）
    rec = s.ingest_event(idx, normalize_event(
        {"tx_hash": "t1", "log_index": 0, "type": "deposit",
         "address": "wA", "amount": "100", "block_height": 10}))
    assert rec.event.asset == DEFAULT_ASSET
    _confirm(s, idx, rec)
    # 旧形态用户操作（位置参数，无 asset）
    s.transfer(alice, "wA", "wB", 30, "k1")
    s.withdraw(alice, "wA", 20, "w1")
    assert s.get_balance(alice, "wA").available == 50
    assert s.get_balance(alice, "wB").available == 30
    # 显式指定默认资产看到的是同一本账
    assert s.get_balance(alice, "wA", DEFAULT_ASSET).available == 50


def test_invalid_asset_symbol_rejected(env):
    from wallet_ledger.normalizer import normalize_asset
    for bad in ("", "   ", 123, "bad coin", "x" * 33):
        with pytest.raises(InvalidAssetError):
            normalize_asset(bad)
    assert normalize_asset("btc") == "BTC"


# ---- 批量扫描：一批混多种资产 ----


def test_scan_batch_mixed_assets_applies_each_to_own_ledger(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    _deposit(s, idx, "tb", "wA", 100, 10, li=0, asset=BTC)
    _deposit(s, idx, "te", "wA", 50, 10, li=1, asset=ETH)
    scanner = env["scanner"]
    out = scanner.scan_batch(
        idx, tip_height=13,
        events=[
            ("te", 1, 3),  # ETH 确认
            ("tb", 0, 3),  # BTC 确认（顺序故意打乱，结果确定）
        ],
    )
    assert out["status"] == "ok"
    assert s.get_balance(alice, "wA", BTC).available == 100
    assert s.get_balance(alice, "wA", ETH).available == 50


def test_scan_batch_mixed_reorgs_isolated(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    _deposit(s, idx, "tb", "wA", 100, 10, li=0, asset=BTC)
    _deposit(s, idx, "te", "wA", 30, 10, li=1, asset=ETH)
    env["scanner"].scan_batch(
        idx, tip_height=13,
        events=[("tb", 0, 3), ("te", 1, 3)],
    )
    # 下一批：BTC 回撤 + ETH 再确认，先回撤后确认、互不影响
    env["scanner"].scan_batch(
        idx, tip_height=14,
        events=[("te", 1, 9)],
        reorg_keys=[("tb", 0)],
    )
    assert s.get_balance(alice, "wA", BTC).available == 0
    assert s.get_balance(alice, "wA", ETH).available == 30


# ---- 并发：同一钱包不同资产互不干扰 ----


def test_concurrent_ops_on_different_assets_do_not_interfere(env):
    s, alice, idx = env["service"], env["alice"], env["indexer"]
    rb = _deposit(s, idx, "tb", "wA", 1000, 1, asset=BTC)
    re_ = _deposit(s, idx, "te", "wA", 1000, 1, asset=ETH)
    _confirm(s, idx, rb)
    _confirm(s, idx, re_)

    n_per_asset = 50
    results: list[tuple[str, bool]] = []
    barrier = threading.Barrier(n_per_asset * 2)

    def worker(i, asset):
        barrier.wait()
        try:
            s.withdraw(alice, "wA", 20, f"wd-{asset}-{i}", asset=asset)
            results.append((asset, True))
        except Exception:
            results.append((asset, False))

    threads = []
    for i in range(n_per_asset):
        threads.append(threading.Thread(target=worker, args=(i, BTC)))
        threads.append(threading.Thread(target=worker, args=(i, ETH)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for asset in (BTC, ETH):
        oks = [ok for a, ok in results if a == asset]
        assert len(oks) == n_per_asset and all(oks)
        bal = s.get_balance(alice, "wA", asset)
        assert bal.available == 0 and bal.available >= 0
        outs = [e for e in s.list_entries(alice, "wA", asset)
                if e.txn_type.value == "withdraw_out"]
        assert len(outs) == n_per_asset
        assert len({e.ref_key for e in outs}) == n_per_asset


def test_concurrent_confirmations_across_assets_no_lost_update(env):
    """不同资产的确认并发推进：各自恰好入账一次。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    assets = [f"A{i}" for i in range(8)]
    recs = {}
    for a in assets:
        recs[a] = _deposit(s, idx, f"tx-{a}", "wA", 100, 1, asset=a)
    barrier = threading.Barrier(len(assets))

    def worker(asset):
        barrier.wait()
        _confirm(s, idx, recs[asset])

    threads = [threading.Thread(target=worker, args=(a,)) for a in assets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for a in assets:
        bal = s.get_balance(alice, "wA", a)
        assert bal.available == 100 and bal.pending_in == 0


# ---- HTTP 层 ----


IDX = {"X-Principal": "principal:indexer"}
ALICE = {"X-Principal": "principal:alice"}


def test_http_balance_shows_all_assets_and_default_fields(client):
    client.post("/wallets", json={"address": "wA"}, headers=ALICE)
    client.post("/chain/events", headers=IDX, json={
        "tx_hash": "tb", "log_index": 0, "type": "deposit",
        "address": "wA", "amount": "100", "block_height": 10, "asset": "BTC"})
    client.post("/chain/events", headers=IDX, json={
        "tx_hash": "te", "log_index": 0, "type": "deposit",
        "address": "wA", "amount": "50", "block_height": 10, "asset": "ETH"})

    body = client.get("/wallets/wA/balance", headers=ALICE).json()
    # 顶层仍是默认资产口径（旧客户端兼容）
    assert body["available"] == 0
    # assets 一次看全
    assert body["assets"]["BTC"]["pending_in"] == 100
    assert body["assets"]["ETH"]["pending_in"] == 50
    assert body["assets"][DEFAULT_ASSET]["pending_in"] == 0

    # 指定资产查询
    single = client.get("/wallets/wA/balance?asset=btc", headers=ALICE).json()
    assert single["asset"] == "BTC" and single["pending_in"] == 100


def test_http_legacy_request_without_asset_unchanged(client):
    client.post("/wallets", json={"address": "wA"}, headers=ALICE)
    client.post("/chain/events", headers=IDX, json={
        "tx_hash": "t1", "log_index": 0, "type": "deposit",
        "address": "wA", "amount": "100", "block_height": 10})
    client.post("/chain/scan", headers=IDX,
                json={"tip_height": 13, "events": [
                    {"tx_hash": "t1", "log_index": 0, "confirmations": 3}]})
    r = client.post("/ledger/withdraw", headers=ALICE,
                    json={"address": "wA", "amount": 40, "idempotency_key": "w1"})
    assert r.status_code == 200 and r.json()["available"] == 60
    body = client.get("/wallets/wA/balance", headers=ALICE).json()
    assert body["available"] == 60 and body["assets"][DEFAULT_ASSET]["available"] == 60


def test_http_transfer_asset_and_conflict(client):
    client.post("/wallets", json={"address": "wA"}, headers=ALICE)
    client.post("/wallets", json={"address": "wB"}, headers=ALICE)
    client.post("/chain/events", headers=IDX, json={
        "tx_hash": "tb", "log_index": 0, "type": "deposit",
        "address": "wA", "amount": "100", "block_height": 10, "asset": "BTC"})
    client.post("/chain/scan", headers=IDX,
                json={"tip_height": 13, "events": [
                    {"tx_hash": "tb", "log_index": 0, "confirmations": 3}]})
    body = {"from_address": "wA", "to_address": "wB", "amount": 60,
            "idempotency_key": "k1", "asset": "BTC"}
    assert client.post("/ledger/transfer", json=body, headers=ALICE).status_code == 200
    conflict = dict(body, asset="ETH")
    r = client.post("/ledger/transfer", json=conflict, headers=ALICE)
    assert r.status_code == 409 and r.json()["code"] == "IDEMPOTENCY_CONFLICT"

    wa = client.get("/wallets/wA/balance?asset=BTC", headers=ALICE).json()
    wb = client.get("/wallets/wB/balance?asset=BTC", headers=ALICE).json()
    assert wa["available"] == 40 and wb["available"] == 60


def test_http_invalid_asset_returns_400_distinguishable(client):
    client.post("/wallets", json={"address": "wA"}, headers=ALICE)
    r = client.get("/wallets/wA/balance?asset=bad%20coin", headers=ALICE)
    assert r.status_code == 400 and r.json()["code"] == "INVALID_ASSET"


def test_http_entries_can_be_filtered_by_asset(client):
    client.post("/wallets", json={"address": "wA"}, headers=ALICE)
    client.post("/chain/events", headers=IDX, json={
        "tx_hash": "tb", "log_index": 0, "type": "deposit",
        "address": "wA", "amount": "10", "block_height": 1, "asset": "BTC"})
    client.post("/chain/events", headers=IDX, json={
        "tx_hash": "t1", "log_index": 0, "type": "deposit",
        "address": "wA", "amount": "7", "block_height": 1})
    all_entries = client.get("/wallets/wA/entries", headers=ALICE).json()["entries"]
    btc_entries = client.get("/wallets/wA/entries?asset=BTC",
                             headers=ALICE).json()["entries"]
    assert len(all_entries) == 2
    assert len(btc_entries) == 1 and btc_entries[0]["asset"] == "BTC"
