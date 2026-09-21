"""HTTP 全链路：投喂 -> 扫描确认 -> 转账/提现 -> 重组；限流与重放可区分。"""

from __future__ import annotations

from wallet_ledger.api import deps
from wallet_ledger.scanner import ConfirmationScanner, ScanPolicy

IDX = {"X-Principal": "principal:indexer"}
ALICE = {"X-Principal": "principal:alice"}


def _deposit_event(client, tx, address, amount, height, li=0):
    return client.post("/chain/events", headers=IDX, json={
        "tx_hash": tx, "log_index": li, "type": "deposit",
        "address": address, "amount": str(amount), "block_height": height,
    })


def test_full_deposit_confirm_transfer_withdraw_flow(client):
    client.post("/wallets", json={"address": "wA"}, headers=ALICE)
    client.post("/wallets", json={"address": "wB"}, headers=ALICE)

    r = _deposit_event(client, "t1", "wA", 100, 10)
    assert r.status_code == 200 and r.json()["event_status"] == "pending"

    # 未确认：available 为 0，pending_in 为 100
    bal = client.get("/wallets/wA/balance", headers=ALICE).json()
    assert bal["available"] == 0 and bal["pending_in"] == 100

    # 扫描确认
    r = client.post("/chain/scan", headers=IDX,
                    json={"tip_height": 13, "events": [
                        {"tx_hash": "t1", "log_index": 0, "confirmations": 3}]})
    assert r.status_code == 200
    bal = client.get("/wallets/wA/balance", headers=ALICE).json()
    assert bal["available"] == 100 and bal["pending_in"] == 0

    # 转账 + 重复重放
    body = {"from_address": "wA", "to_address": "wB", "amount": 40,
            "idempotency_key": "k1"}
    assert client.post("/ledger/transfer", json=body, headers=ALICE).status_code == 200
    r2 = client.post("/ledger/transfer", json=body, headers=ALICE)
    assert r2.status_code == 208 and r2.json().get("replay") is True
    assert client.get("/wallets/wA/balance", headers=ALICE).json()["available"] == 60

    # 同键不同体 -> 409
    conflict = dict(body, amount=41)
    r3 = client.post("/ledger/transfer", json=conflict, headers=ALICE)
    assert r3.status_code == 409 and r3.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_http_rate_limited_returns_429_with_retry_after(client):
    # 用一个极小在途配额的扫描器替换单例
    scanner = ConfirmationScanner(
        deps.get_service(),
        ScanPolicy(max_inflight=1, burst=1000, rate_per_sec=1000,
                   max_batch_size=1000),
    )
    client.app.dependency_overrides[deps.get_scanner] = lambda: scanner
    try:
        # 先占满唯一在途名额，再请求即 429，带 retry_after。
        scanner._limiter.acquire()
        r = client.post("/chain/scan", headers=IDX,
                        json={"tip_height": 1, "events": []})
        assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED"
        assert "retry_after" in r.json()["details"]
    finally:
        scanner._limiter.release()
        client.app.dependency_overrides.clear()  # 不泄漏到后续用例


def test_reorg_keeps_balance_nonnegative_over_http(client):
    client.post("/wallets", json={"address": "wA"}, headers=ALICE)
    _deposit_event(client, "t1", "wA", 100, 10)
    client.post("/chain/scan", headers=IDX,
                json={"tip_height": 13, "events": [
                    {"tx_hash": "t1", "log_index": 0, "confirmations": 3}]})
    # 花掉全部
    client.post("/ledger/withdraw", headers=ALICE,
                json={"address": "wA", "amount": 100, "idempotency_key": "w1"})
    # 重组回撤：余额钳 0，缺口平账
    r = client.post("/chain/scan", headers=IDX,
                    json={"tip_height": 14, "events": [],
                          "reorg_keys": [["t1", 0]]})
    assert r.status_code == 200
    bal = client.get("/wallets/wA/balance", headers=ALICE).json()
    assert bal["available"] == 0 and bal["reorg_debt"] == 100


def test_entries_are_owner_scoped(client):
    client.post("/wallets", json={"address": "wA"}, headers=ALICE)
    _deposit_event(client, "t1", "wA", 10, 1)
    r = client.get("/wallets/wA/entries", headers=ALICE)
    assert r.status_code == 200 and len(r.json()["entries"]) == 1
    # bob 越权：统一 404
    r = client.get("/wallets/wA/entries",
                   headers={"X-Principal": "principal:bob"})
    assert r.status_code == 404
