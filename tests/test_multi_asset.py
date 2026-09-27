"""多资产：同一钱包下按资产分别记账/查询，资金结论各资产独立、守恒、可解释。

重点：
- 充值确认、转账、提现、链上回退都只落请求指定资产，资产间不抵扣不串账；
- 每个资产独立满足资金守恒，回撤不为负、缺口按资产平账；
- 幂等/冲突按资产区分；不同资产并发互不干扰；
- 不带资产的旧用法按默认资产处理，结论与单资产时代一致；
- 一批扫描混多资产时各自正确落账，限流仍按事件数计费。
"""

from __future__ import annotations

import threading
import time

import pytest

from wallet_ledger.domain import DEFAULT_ASSET
from wallet_ledger.errors import IdempotencyConflictError, InsufficientFundsError
from wallet_ledger.normalizer import normalize_event


def _deposit(service, indexer, tx, address, amount, height, asset=DEFAULT_ASSET,
             li=0, confirm=True, required=3):
    event = normalize_event(
        {"tx_hash": tx, "log_index": li, "type": "deposit",
         "address": address, "amount": str(amount),
         "block_height": height, "asset": asset}
    )
    rec = service.ingest_event(indexer, event)
    if confirm:
        service.apply_confirmations(
            indexer, [rec.event.natural_key], required,
            height + required, required,
        )
    return rec


def _sub(bal, asset):
    return bal.get(asset)


# ------------------------------------------------------------------ #
# 1. 分资产记账与查询
# ------------------------------------------------------------------ #


def test_distinct_assets_booked_independently(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    _deposit(s, idx, "t-u", "wA", 100, 10, asset="USDT")
    _deposit(s, idx, "t-b", "wA", 5, 10, asset="BTC")

    bal = s.get_balance(alice, "wA")
    assert _sub(bal, "USDT").available == 100
    assert _sub(bal, "BTC").available == 5
    assert bal.available == 0  # 默认资产视图不受影响
    view = bal.assets_to_dict()
    assert set(view) == {"USDT", "BTC"}
    # 未发生过的资产返回全 0，但不凭空落账
    assert "ETH" not in view
    assert _sub(bal, "ETH").available == 0


def test_asset_identifier_is_case_insensitive(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    _deposit(s, idx, "t1", "wA", 10, 1, asset="usdt")
    _deposit(s, idx, "t2", "wA", 20, 2, asset="UsDt")
    # 不同写法归一到同一资产，不串账
    bal = s.get_balance(alice, "wA")
    assert _sub(bal, "USDT").available == 30
    assert set(bal.assets_to_dict()) == {"USDT"}


def test_pending_is_per_asset(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    rec_u = _deposit(s, idx, "t-u", "wA", 100, 10, asset="USDT", confirm=False)
    _deposit(s, idx, "t-b", "wA", 5, 10, asset="BTC")
    bal = s.get_balance(alice, "wA")
    assert _sub(bal, "USDT").pending_in == 100
    assert _sub(bal, "USDT").available == 0
    assert _sub(bal, "BTC").available == 5
    assert _sub(bal, "BTC").pending_in == 0
    # 只确认 USDT
    s.apply_confirmations(idx, [rec_u.event.natural_key], 3, 13, 3)
    bal = s.get_balance(alice, "wA")
    assert _sub(bal, "USDT").available == 100
    assert _sub(bal, "USDT").pending_in == 0
    assert _sub(bal, "BTC").available == 5  # 不受影响


def test_transfer_moves_only_named_asset(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    _deposit(s, idx, "t-u", "wA", 100, 10, asset="USDT")
    _deposit(s, idx, "t-b", "wA", 5, 10, asset="BTC")

    s.transfer(alice, "wA", "wB", 40, "k1", asset="USDT")
    a, b = s.get_balance(alice, "wA"), s.get_balance(alice, "wB")
    assert _sub(a, "USDT").available == 60
    assert _sub(b, "USDT").available == 40
    # 源/目的钱包的 BTC 与默认资产都不受影响
    assert _sub(a, "BTC").available == 5
    assert _sub(b, "BTC").available == 0
    assert _sub(b, DEFAULT_ASSET).available == 0


def test_withdraw_debits_only_named_asset(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    _deposit(s, idx, "t-u", "wA", 100, 10, asset="USDT")
    _deposit(s, idx, "t-b", "wA", 5, 10, asset="BTC")
    s.withdraw(alice, "wA", 30, "w1", asset="USDT")
    bal = s.get_balance(alice, "wA")
    assert _sub(bal, "USDT").available == 70
    assert _sub(bal, "BTC").available == 5
    # BTC 余额不能用于 USDT 提现；反之亦然
    with pytest.raises(InsufficientFundsError) as ei:
        s.withdraw(alice, "wA", 6, "w2", asset="BTC")
    assert ei.value.details["asset"] == "BTC"
    with pytest.raises(InsufficientFundsError):
        s.withdraw(alice, "wA", 71, "w3", asset="USDT")
    assert _sub(s.get_balance(alice, "wA"), "USDT").available == 70
    assert _sub(s.get_balance(alice, "wA"), "BTC").available == 5


def test_entries_are_asset_scoped(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    _deposit(s, idx, "t-u", "wA", 100, 10, asset="USDT")
    _deposit(s, idx, "t-b", "wA", 5, 10, asset="BTC")
    all_entries = s.list_entries(alice, "wA")
    assert {e.asset for e in all_entries} == {"USDT", "BTC"}
    usdt_entries = s.list_entries(alice, "wA", "USDT")
    assert usdt_entries and all(e.asset == "USDT" for e in usdt_entries)


# ------------------------------------------------------------------ #
# 2. 各资产独立的资金守恒 / 回撤
# ------------------------------------------------------------------ #


def test_conservation_holds_per_asset_under_transfers(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    _deposit(s, idx, "u1", "wA", 100, 10, asset="USDT")
    _deposit(s, idx, "u2", "wB", 50, 11, asset="USDT")
    _deposit(s, idx, "b1", "wA", 8, 12, asset="BTC")
    s.transfer(alice, "wA", "wB", 70, "t1", asset="USDT")
    # wB 没有 BTC：跨资产/跨钱包均不能用他人或他币种资金，显式拒绝不落账
    with pytest.raises(InsufficientFundsError):
        s.transfer(alice, "wB", "wA", 1, "t2", asset="BTC")
    totals = {}
    for asset in ("USDT", "BTC", DEFAULT_ASSET):
        totals[asset] = sum(
            s.get_balance(alice, w).get(asset).available for w in ("wA", "wB")
        )
    assert totals["USDT"] == 150  # 内部转账不改变总量
    assert totals["BTC"] == 8
    assert totals[DEFAULT_ASSET] == 0


def test_reorg_clawback_is_isolated_per_asset(env):
    """USDT 已花出后被回撤，只影响 USDT，BTC 与默认资产不动。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    ru = _deposit(s, idx, "u1", "wA", 100, 10, asset="USDT")
    _deposit(s, idx, "b1", "wA", 5, 10, asset="BTC")
    s.withdraw(alice, "wA", 100, "w1", asset="USDT")  # USDT 全部花出
    s.apply_reorg(idx, [ru.event.natural_key], 13)

    bal = s.get_balance(alice, "wA")
    assert _sub(bal, "USDT").available == 0
    assert _sub(bal, "USDT").reorg_debt == 100  # 缺口按 USDT 平账
    assert _sub(bal, "BTC").available == 5 and _sub(bal, "BTC").reorg_debt == 0
    assert bal.reorg_debt == 0  # 默认资产无债
    # 平账流水带资产标记
    sink = [e for e in s.repo.list_entries("@loss-sink")]
    assert len(sink) == 1 and sink[0].asset == "USDT" and sink[0].amount == 100


def test_reorg_unconfirmed_is_per_asset(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    ru = _deposit(s, idx, "u1", "wA", 100, 10, asset="USDT", confirm=False)
    _deposit(s, idx, "b1", "wA", 5, 10, asset="BTC")
    s.apply_reorg(idx, [ru.event.natural_key], 10)
    bal = s.get_balance(alice, "wA")
    assert _sub(bal, "USDT").pending_in == 0
    assert _sub(bal, "BTC").available == 5


def test_reorg_precedence_holds_with_mixed_asset_batch(env):
    """同一批混多资产且确认+回撤并存：回撤优先，逐资产结论确定。"""
    s, sc, idx, alice = (
        env["service"], env["scanner"], env["indexer"], env["alice"])
    _deposit(s, idx, "u1", "wA", 100, 10, asset="USDT", confirm=False)
    _deposit(s, idx, "b1", "wA", 5, 10, asset="BTC", confirm=False)
    sc.scan_batch(
        idx, tip_height=13,
        events=[("u1", 0, 3), ("b1", 0, 3)],
        reorg_keys=[("u1", 0)],
    )
    bal = s.get_balance(alice, "wA")
    assert _sub(bal, "USDT").available == 0 and _sub(bal, "USDT").pending_in == 0
    assert _sub(bal, "BTC").available == 5  # 只确认 BTC，互不影响


# ------------------------------------------------------------------ #
# 3. 幂等 / 冲突
# ------------------------------------------------------------------ #


def test_same_key_different_asset_is_conflict(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    _deposit(s, idx, "u1", "wA", 100, 10, asset="USDT")
    _deposit(s, idx, "b1", "wA", 5, 10, asset="BTC")
    s.withdraw(alice, "wA", 10, "k", asset="USDT")
    with pytest.raises(IdempotencyConflictError):
        s.withdraw(alice, "wA", 10, "k", asset="BTC")
    # 冲突不产生任何扣款
    bal = s.get_balance(alice, "wA")
    assert _sub(bal, "USDT").available == 90
    assert _sub(bal, "BTC").available == 5


def test_replay_replays_same_asset(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    _deposit(s, idx, "u1", "wA", 100, 10, asset="USDT")
    first = s.transfer(alice, "wA", "wB", 10, "k", asset="USDT")
    replay = s.transfer(alice, "wA", "wB", 10, "k", asset="USDT")
    assert first.get("replay") is None and replay.get("replay") is True
    assert replay["asset"] == "USDT"
    assert _sub(s.get_balance(alice, "wA"), "USDT").available == 90
    assert _sub(s.get_balance(alice, "wB"), "USDT").available == 10


def test_mixed_asset_scan_is_idempotent_on_retry(env):
    s, sc, idx, alice = (
        env["service"], env["scanner"], env["indexer"], env["alice"])
    _deposit(s, idx, "u1", "wA", 100, 10, asset="USDT", confirm=False)
    _deposit(s, idx, "b1", "wA", 7, 10, asset="BTC", confirm=False)
    body = {"tip_height": 13,
            "events": [("u1", 0, 3), ("b1", 0, 3)]}
    sc.scan_batch(idx, **body)
    sc.scan_batch(idx, **body)  # 重试不重复入账
    bal = s.get_balance(alice, "wA")
    assert _sub(bal, "USDT").available == 100
    assert _sub(bal, "BTC").available == 7
    confirms = [e for e in s.list_entries(alice, "wA")
                if e.txn_type.value == "deposit_confirm"]
    assert len(confirms) == 2


# ------------------------------------------------------------------ #
# 4. 默认资产兼容
# ------------------------------------------------------------------ #


def test_legacy_requests_without_asset_hit_default(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    # 完全不携带 asset（旧请求体）
    rec = _deposit(s, idx, "t1", "wA", 100, 10, asset=DEFAULT_ASSET)
    assert rec.event.asset == DEFAULT_ASSET
    s.withdraw(alice, "wA", 40, "w1")  # 旧签名
    bal = s.get_balance(alice, "wA")
    assert bal.available == 60 and bal.pending_in == 0
    # 默认资产与显式非默认资产互不影响
    _deposit(s, idx, "t2", "wA", 3, 11, asset="BTC")
    assert s.get_balance(alice, "wA").available == 60
    assert _sub(s.get_balance(alice, "wA"), "BTC").available == 3


# ------------------------------------------------------------------ #
# 5. 混资产批量扫描与限流
# ------------------------------------------------------------------ #


def test_mixed_asset_batch_scan_does_not_amplify_rate_cost(env):
    """令牌按事件数计；同一批多资产不会因币种变多而多占资源。"""
    s, sc, idx, alice = (
        env["service"], env["scanner"], env["indexer"], env["alice"])
    for i, asset in enumerate(["USDT", "BTC", "ETH", "USDC"]):
        _deposit(s, idx, f"t{i}", "wA", 1, 1, asset=asset, confirm=False)
    # 直接观测令牌扣减口径：N 个事件只收 N 个令牌，与资产种类数无关。
    sc._limiter._tokens = 100.0
    sc._limiter._updated = time.monotonic()
    sc.scan_batch(idx, tip_height=4,
                  events=[(f"t{i}", 0, 3) for i in range(4)])
    assert sc._limiter._tokens == pytest.approx(96.0, abs=0.01)
    bal = s.get_balance(alice, "wA")
    assert all(_sub(bal, a).available == 1
               for a in ["USDT", "BTC", "ETH", "USDC"])


# ------------------------------------------------------------------ #
# 6. 不同资产并发隔离
# ------------------------------------------------------------------ #


def test_concurrent_different_asset_withdrawals_are_isolated(env):
    """同一钱包上 USDT/BTC 并发提现：各自恰好花完自己的钱，互不串账。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    _deposit(s, idx, "u1", "wA", 1000, 1, asset="USDT")
    _deposit(s, idx, "b1", "wA", 200, 1, asset="BTC")

    n_each = 25
    results: list[tuple[str, bool]] = []
    barrier = threading.Barrier(n_each * 2)
    lock = threading.Lock()

    def worker(asset, i):
        barrier.wait()
        try:
            s.withdraw(alice, "wA", 40, f"wd-{asset}-{i}", asset=asset)
            ok = True
        except Exception:
            ok = False
        with lock:
            results.append((asset, ok))

    threads = []
    for i in range(n_each):
        threads.append(threading.Thread(target=worker, args=("USDT", i)))
        threads.append(threading.Thread(target=worker, args=("BTC", i)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    bal = s.get_balance(alice, "wA")
    # USDT: 25 * 40 = 1000 恰好成功；BTC: 25 * 40 = 1000，只能成 5 笔
    assert sum(ok for a, ok in results if a == "USDT" and ok) == 25
    assert sum(ok for a, ok in results if a == "BTC" and ok) == 5
    assert _sub(bal, "USDT").available == 0
    assert _sub(bal, "BTC").available == 0
    assert _sub(bal, "USDT").reorg_debt == 0
    assert _sub(bal, "BTC").reorg_debt == 0
    # 成功流水每资产唯一 ref_key，无重复扣款
    for asset, expect in (("USDT", 25), ("BTC", 5)):
        outs = [e for e in s.list_entries(alice, "wA", asset)
                if e.txn_type.value == "withdraw_out"]
        assert len(outs) == expect
        assert len({e.ref_key for e in outs}) == expect
        assert all(e.asset == asset for e in outs)


def test_concurrent_different_asset_operations_keep_per_asset_conservation(env):
    """并发混合：USDT 转账、BTC 提现、默认资产充值确认同时发生，各守恒。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    _deposit(s, idx, "u1", "wA", 100, 1, asset="USDT")
    _deposit(s, idx, "b1", "wA", 100, 1, asset="BTC")
    _deposit(s, idx, "d1", "wA", 100, 1, asset=DEFAULT_ASSET)

    errors: list[Exception] = []
    barrier = threading.Barrier(3)

    def move_usdt():
        barrier.wait()
        try:
            for i in range(20):
                s.transfer(alice, "wA", "wB", 1, f"u{i}", asset="USDT")
        except Exception as exc:
            errors.append(exc)

    def spend_btc():
        barrier.wait()
        try:
            for i in range(20):
                s.withdraw(alice, "wA", 1, f"b{i}", asset="BTC")
        except Exception as exc:
            errors.append(exc)

    def spend_default():
        barrier.wait()
        try:
            for i in range(20):
                s.withdraw(alice, "wA", 1, f"d{i}")  # 旧用法：默认资产
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=move_usdt),
               threading.Thread(target=spend_btc),
               threading.Thread(target=spend_default)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    wa, wb = s.get_balance(alice, "wA"), s.get_balance(alice, "wB")
    # USDT：wA 出 80、wB 入 20，总量 100
    assert _sub(wa, "USDT").available == 80
    assert _sub(wb, "USDT").available == 20
    # BTC：只在 wA 上花掉 20
    assert _sub(wa, "BTC").available == 80
    assert _sub(wb, "BTC").available == 0
    # 默认资产：只在 wA 上花掉 20，与命名资产互不可见
    assert wa.available == 80
    assert wb.available == 0


def test_concurrent_same_asset_identity_key_single_effect(env):
    """同一资产 + 同一幂等键并发：恰好一次；不波及其他资产。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    _deposit(s, idx, "u1", "wA", 100, 1, asset="USDT")
    _deposit(s, idx, "b1", "wA", 100, 1, asset="BTC")

    n = 20
    statuses: list[str] = []
    barrier = threading.Barrier(n)
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            body = s.withdraw(alice, "wA", 10, "same", asset="USDT")
            with lock:
                statuses.append("replay" if body.get("replay") else "ok")
        except Exception as exc:
            with lock:
                statuses.append(type(exc).__name__)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert statuses.count("ok") == 1
    assert statuses.count("replay") == n - 1
    bal = s.get_balance(alice, "wA")
    assert _sub(bal, "USDT").available == 90  # 只扣一次
    assert _sub(bal, "BTC").available == 100  # 其他资产不变


# ------------------------------------------------------------------ #
# 7. HTTP 层
# ------------------------------------------------------------------ #


IDX_HEADERS = {"X-Principal": "principal:indexer"}
ALICE_HEADERS = {"X-Principal": "principal:alice"}


def _http_deposit(client, tx, address, amount, height, asset=None, li=0):
    payload = {"tx_hash": tx, "log_index": li, "type": "deposit",
               "address": address, "amount": str(amount),
               "block_height": height}
    if asset is not None:
        payload["asset"] = asset
    return client.post("/chain/events", headers=IDX_HEADERS, json=payload)


def test_http_multi_asset_balances_and_legacy_shape(client):
    client.post("/wallets", json={"address": "wA"}, headers=ALICE_HEADERS)
    r = _http_deposit(client, "t1", "wA", 100, 10, asset="USDT")
    assert r.status_code == 200 and r.json()["asset"] == "USDT"
    r = _http_deposit(client, "t2", "wA", 5, 10, asset="btc")
    assert r.json()["asset"] == "BTC"
    client.post("/chain/scan", headers=IDX_HEADERS,
                json={"tip_height": 13, "events": [
                    {"tx_hash": "t1", "log_index": 0, "confirmations": 3},
                    {"tx_hash": "t2", "log_index": 0, "confirmations": 3}]})

    # 全资产视图
    r = client.get("/wallets/wA/balances", headers=ALICE_HEADERS)
    assets = r.json()["assets"]
    assert assets["USDT"]["available"] == 100
    assert assets["BTC"]["available"] == 5

    # 旧形状（默认资产，无 asset 键）
    r = client.get("/wallets/wA/balance", headers=ALICE_HEADERS)
    assert set(r.json()) == {"address", "available", "pending_in",
                             "pending_out", "reorg_debt"}
    assert r.json()["available"] == 0

    # 指定资产
    r = client.get("/wallets/wA/balance", headers=ALICE_HEADERS,
                   params={"asset": "usdt"})
    assert r.status_code == 200 and r.json()["available"] == 100
    assert r.json()["asset"] == "USDT"


def test_http_legacy_requests_still_work(client):
    """不带 asset 的旧请求按默认资产，结论与单资产时代一致。"""
    client.post("/wallets", json={"address": "wA"}, headers=ALICE_HEADERS)
    assert _http_deposit(client, "t1", "wA", 100, 10).status_code == 200
    client.post("/chain/scan", headers=IDX_HEADERS,
                json={"tip_height": 13, "events": [
                    {"tx_hash": "t1", "log_index": 0, "confirmations": 3}]})
    body = {"address": "wA", "amount": 40, "idempotency_key": "k1"}
    assert client.post("/ledger/withdraw", json=body,
                       headers=ALICE_HEADERS).status_code == 200
    bal = client.get("/wallets/wA/balance", headers=ALICE_HEADERS).json()
    assert bal["available"] == 60
    # 流水的 asset 字段回显为默认资产
    entries = client.get("/wallets/wA/entries",
                         headers=ALICE_HEADERS).json()["entries"]
    assert entries and all(e["asset"] == DEFAULT_ASSET for e in entries)


def test_http_invalid_asset_distinguishable(client):
    client.post("/wallets", json={"address": "wA"}, headers=ALICE_HEADERS)
    r = _http_deposit(client, "t1", "wA", 1, 1, asset="bad coin")
    assert r.status_code == 400 and r.json()["code"] == "INVALID_ASSET"
    # 非法资产事件未入模
    r = client.post("/chain/scan", headers=IDX_HEADERS,
                    json={"tip_height": 13, "events": [
                        {"tx_hash": "t1", "log_index": 0, "confirmations": 3}]})
    assert r.status_code == 404  # 未知事件，无串账
    # 用户操作上的非法资产
    r = client.post("/ledger/withdraw", headers=ALICE_HEADERS,
                    json={"address": "wA", "amount": 1,
                          "idempotency_key": "k", "asset": 123})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_ASSET"


def test_http_mixed_asset_reorg_keeps_each_nonnegative(client):
    client.post("/wallets", json={"address": "wA"}, headers=ALICE_HEADERS)
    _http_deposit(client, "u1", "wA", 100, 10, asset="USDT")
    _http_deposit(client, "b1", "wA", 5, 10, asset="BTC")
    client.post("/chain/scan", headers=IDX_HEADERS,
                json={"tip_height": 13, "events": [
                    {"tx_hash": "u1", "log_index": 0, "confirmations": 3},
                    {"tx_hash": "b1", "log_index": 0, "confirmations": 3}]})
    # USDT 全花掉，BTC 留着；一批里只回撤 USDT
    client.post("/ledger/withdraw", headers=ALICE_HEADERS,
                json={"address": "wA", "amount": 100,
                      "idempotency_key": "w", "asset": "USDT"})
    r = client.post("/chain/scan", headers=IDX_HEADERS,
                    json={"tip_height": 14, "events": [],
                          "reorg_keys": [["u1", 0]]})
    assert r.status_code == 200
    assets = client.get("/wallets/wA/balances",
                        headers=ALICE_HEADERS).json()["assets"]
    assert assets["USDT"] == {"asset": "USDT", "available": 0,
                              "pending_in": 0, "pending_out": 0,
                              "reorg_debt": 100}
    assert assets["BTC"] == {"asset": "BTC", "available": 5,
                             "pending_in": 0, "pending_out": 0,
                             "reorg_debt": 0}


def test_mixed_asset_oversized_batch_still_rejected(env):
    """批量上限按事件数生效：即使全部是不同资产，超限同样拒绝并可重试。"""
    from wallet_ledger.errors import RateLimitedError

    s, sc, idx = env["service"], env["scanner"], env["indexer"]
    # 51 个事件、51 个不同资产，超过 env 夹具 max_batch_size=50
    events = []
    for i in range(51):
        asset = f"A{i:02d}"
        _deposit(s, idx, f"t{i}", "wA", 1, 1, asset=asset, confirm=False)
        events.append((f"t{i}", 0, 3))
    with pytest.raises(RateLimitedError) as ei:
        sc.scan_batch(idx, tip_height=4, events=events)
    assert ei.value.details["reason"] == "batch_size"
    assert "retry_after" in ei.value.details
    # 没有任何事件被确认（显式拒绝而非部分处理）
    bal = s.get_balance(env["alice"], "wA")
    assert all(bal.get(f"A{i:02d}").available == 0 for i in range(51))


def test_onchain_withdrawal_shortfall_booked_per_asset(env):
    """链上提现事件入口与用户入口结论对齐：该资产不足也不为负，缺口可解释。

    链上事件表达“链上已经扣款”的事实，必须落账；可用不足时钳 0 并把缺口
    平账到该资产的 reorg_debt，而不是动用其他资产或产生负余额。
    """
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    _deposit(s, idx, "b1", "wA", 5, 1, asset="BTC")
    # 账本上 USDT 可用为 0，却观察到一笔 100 的 USDT 链上转出
    ev = normalize_event(
        {"tx_hash": "w1", "log_index": 0, "type": "withdrawal",
         "address": "wA", "amount": "100", "block_height": 2, "asset": "USDT"}
    )
    s.ingest_event(idx, ev)
    bal = s.get_balance(alice, "wA")
    assert _sub(bal, "USDT").available == 0
    assert _sub(bal, "USDT").reorg_debt == 100
    assert _sub(bal, "BTC").available == 5  # 其他资产不被拿来抵扣
    # 守恒：USDT 净外部注入 -100，available - debt = 0 - 100 = -100
    assert (_sub(bal, "USDT").available - _sub(bal, "USDT").reorg_debt) == -100
    sink = [e for e in s.repo.list_entries("@loss-sink") if e.asset == "USDT"]
    assert len(sink) == 1 and sink[0].amount == 100


def test_onchain_withdrawal_reorg_returns_funds_to_same_asset(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    _deposit(s, idx, "u1", "wA", 100, 1, asset="USDT")
    ev = normalize_event(
        {"tx_hash": "w1", "log_index": 0, "type": "withdrawal",
         "address": "wA", "amount": 30, "block_height": 2, "asset": "USDT"}
    )
    rec = s.ingest_event(idx, ev)
    assert _sub(s.get_balance(alice, "wA"), "USDT").available == 70
    s.apply_reorg(idx, [rec.event.natural_key], 2)
    bal = s.get_balance(alice, "wA")
    assert _sub(bal, "USDT").available == 100
    assert _sub(bal, "USDT").reorg_debt == 0


def test_concurrent_confirm_reorg_across_assets_is_isolated(env):
    """不同资产的确认与回撤并发交错：逐资产结论确定、不串账、不为负。"""
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    _deposit(s, idx, "u1", "wA", 300, 10, asset="USDT", confirm=False)
    _deposit(s, idx, "b1", "wA", 9, 10, asset="BTC", confirm=False)
    errors: list[Exception] = []

    def call(fn):
        try:
            fn()
        except Exception as exc:
            errors.append(exc)

    jobs = [
        lambda: s.apply_confirmations(idx, [("u1", 0)], 5, 15, 3),
        lambda: s.apply_reorg(idx, [("u1", 0)], 15),
        lambda: s.apply_confirmations(idx, [("b1", 0)], 5, 15, 3),
        lambda: s.apply_reorg(idx, [("b1", 0)], 15),
        lambda: s.apply_confirmations(idx, [("u1", 0)], 5, 15, 3),
    ]
    threads = [threading.Thread(target=call, args=(j,)) for j in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    bal = s.get_balance(alice, "wA")
    for asset in ("USDT", "BTC"):
        sub = _sub(bal, asset)
        assert sub.available >= 0 and sub.pending_in >= 0 and sub.reorg_debt >= 0
        rec = s.repo.get_event(("u1" if asset == "USDT" else "b1", 0))
        # 回撤与确认竞争：至多一条确认入账
        confirms = [e for e in s.list_entries(alice, "wA", asset)
                    if e.txn_type.value == "deposit_confirm"]
        assert len(confirms) <= 1
        if rec.reorged:
            assert sub.available == 0 and sub.pending_in == 0
        else:
            assert sub.available == (300 if asset == "USDT" else 9)
