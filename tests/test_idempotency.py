"""写操作幂等：重复提交不重复扣款/入账，语义冲突被明确区分拒绝。"""
from __future__ import annotations

from tests.conftest import (
    H_ADMIN, H_USER1, H_USER2, balance, deposit_event,
    make_wallet, push_and_ingest,
)


def _fund(client, wid, address, amount=100, key_seed="x"):
    push_and_ingest(client, [deposit_event(f"d-{key_seed}", address, amount, 1)],
                    advance_to=3)


def test_duplicate_transfer_does_not_double_debit(client):
    w1 = make_wallet(client, H_USER1, "0xA")
    w2 = make_wallet(client, H_USER2, "0xB")
    _fund(client, w1, "0xA", 100, "dup")
    headers = {**H_USER1, "Idempotency-Key": "k-t1"}
    payload = {"to_wallet_id": w2, "amount": 30}
    r1 = client.post(f"/api/v1/wallets/{w1}/transfers", headers=headers, json=payload)
    assert r1.status_code == 200 and r1.json().get("idempotent_replay") is False
    # 完全相同的重复请求 -> 返回首次响应，不再次扣款
    r2 = client.post(f"/api/v1/wallets/{w1}/transfers", headers=headers, json=payload)
    assert r2.status_code == 200
    assert r2.json().get("idempotent_replay") is True
    assert balance(client, H_USER1, w1)["available"] == 70
    assert balance(client, H_USER2, w2)["available"] == 30


def test_same_key_different_payload_is_conflict(client):
    w1 = make_wallet(client, H_USER1, "0xA")
    w2 = make_wallet(client, H_USER2, "0xB")
    _fund(client, w1, "0xA", 100, "cf")
    client.post(f"/api/v1/wallets/{w1}/transfers",
                headers={**H_USER1, "Idempotency-Key": "k-c"},
                json={"to_wallet_id": w2, "amount": 30})
    # 同一 Idempotency-Key，不同金额 -> 409 idempotency_conflict，不扣款
    r = client.post(f"/api/v1/wallets/{w1}/transfers",
                    headers={**H_USER1, "Idempotency-Key": "k-c"},
                    json={"to_wallet_id": w2, "amount": 99})
    assert r.status_code == 409
    body = r.json()["error"]
    assert body["code"] == "idempotency_conflict"
    assert balance(client, H_USER1, w1)["available"] == 70
    assert balance(client, H_USER2, w2)["available"] == 30


def test_duplicate_withdrawal_does_not_double_hold(client):
    w1 = make_wallet(client, H_USER1, "0xA")
    _fund(client, w1, "0xA", 100, "w")
    headers = {**H_USER1, "Idempotency-Key": "k-w"}
    for i in range(3):
        r = client.post(f"/api/v1/wallets/{w1}/withdrawals",
                        headers=headers, json={"amount": 40})
        assert r.status_code == 200
    b = balance(client, H_USER1, w1)
    assert b["available"] == 60 and b["holds"] == 40  # 只冻结一次


def test_insufficient_funds_is_retryable_with_same_key(client):
    w1 = make_wallet(client, H_USER1, "0xA")
    w2 = make_wallet(client, H_USER2, "0xB")
    # 初始余额 0，超额转账失败但 request_id 不被永久锁死
    r = client.post(f"/api/v1/wallets/{w1}/transfers",
                    headers={**H_USER1, "Idempotency-Key": "k-r"},
                    json={"to_wallet_id": w2, "amount": 50})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "insufficient_funds"
    # 充值后同 key 重试成功
    _fund(client, w1, "0xA", 100, "retry")
    r = client.post(f"/api/v1/wallets/{w1}/transfers",
                    headers={**H_USER1, "Idempotency-Key": "k-r"},
                    json={"to_wallet_id": w2, "amount": 50})
    assert r.status_code == 200
    assert balance(client, H_USER2, w2)["available"] == 50


def test_duplicate_chain_event_ingested_once(client):
    wid = make_wallet(client, H_USER1, "0xA")
    ev = deposit_event("dup-ev", "0xA", 100, 1)
    push_and_ingest(client, [ev], advance_to=3)
    assert balance(client, H_USER1, wid)["available"] == 100
    # 通过扫描入口直接重复摄入相同原始事件（游标重读/上游重发的等价路径）
    r = client.post("/api/v1/admin/events/ingest", headers=H_ADMIN,
                    json={"events": [ev]})
    assert r.status_code == 200
    assert r.json()["duplicates"] == ["dup-ev"]
    assert balance(client, H_USER1, wid)["available"] == 100  # 不重复入账


def test_scan_replay_after_cursor_reset_is_idempotent(client):
    # 重组后游标重置，扫描从头重读：靠 event_id 去重，不产生重复入账
    wid = make_wallet(client, H_USER1, "0xA")
    ev = deposit_event("scan-ev", "0xA", 100, 1)
    h = client.post("/api/v1/admin/chain/blocks", headers=H_ADMIN,
                    json={"events": [ev]}).json()["chain_height"]
    ev["block_height"] = h
    client.post("/api/v1/admin/scan", headers=H_ADMIN, json={})
    client.post("/api/v1/admin/chain/advance", headers=H_ADMIN, json={"blocks": 2})
    client.post("/api/v1/admin/scan", headers=H_ADMIN, json={})
    assert balance(client, H_USER1, wid)["available"] == 100
    # 再来一批新事件，扫描处理；旧事件不重复
    ev2 = deposit_event("scan-ev-2", "0xA", 20, 1)
    client.post("/api/v1/admin/chain/blocks", headers=H_ADMIN, json={"events": [ev2]})
    client.post("/api/v1/admin/scan", headers=H_ADMIN, json={})
    assert balance(client, H_USER1, wid)["pending_in"] == 20


def test_same_event_id_different_payload_rejected_as_conflict(client):
    wid = make_wallet(client, H_USER1, "0xA")
    push_and_ingest(client, [deposit_event("ev", "0xA", 100, 1)], advance_to=3)
    # 同 event_id 金额不同 -> conflict，进死信
    _, _, res = push_and_ingest(
        client, [deposit_event("ev", "0xA", 999, 1)], advance_to=5)
    assert any(x["event_id"] == "ev" and x["reason"] == "conflict_state"
               for x in res["rejected"])
    dl = client.get("/api/v1/admin/dead-letters", headers=H_ADMIN).json()
    assert any(d["reason_code"] == "conflict_state" for d in dl["dead_letters"])
    assert balance(client, H_USER1, wid)["available"] == 100


def test_missing_idempotency_header_rejected(client):
    w1 = make_wallet(client, H_USER1, "0xA")
    r = client.post(f"/api/v1/wallets/{w1}/withdrawals",
                    headers=H_USER1, json={"amount": 10})
    assert r.status_code == 422  # 缺少 Idempotency-Key
