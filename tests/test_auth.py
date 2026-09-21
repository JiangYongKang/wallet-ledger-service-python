"""身份与钱包级权限：越权拒绝且不泄露目标资源是否存在。"""
from __future__ import annotations

from tests.conftest import (
    H_ADMIN, H_USER1, H_USER2, deposit_event, make_wallet,
    push_and_ingest,
)


def test_missing_identity_unauthorized(client):
    r = client.get("/api/v1/wallets/whatever/balance")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_other_owner_and_missing_wallet_return_identical_404(client):
    wid = make_wallet(client, H_USER1, "0xA")
    r_denied = client.get(f"/api/v1/wallets/{wid}/balance", headers=H_USER2)
    r_missing = client.get("/api/v1/wallets/does-not-exist/balance",
                           headers=H_USER2)
    assert r_denied.status_code == r_missing.status_code == 404
    # 状态码与响应体均不可区分，且不回显目标 wallet_id
    assert r_denied.json() == r_missing.json()
    assert wid not in r_denied.text


def test_cannot_transfer_from_other_wallet(client):
    w1 = make_wallet(client, H_USER1, "0xA")
    w2 = make_wallet(client, H_USER2, "0xB")
    push_and_ingest(client, [deposit_event("d1", "0xA", 100, 1)], advance_to=3)
    # u2 试图从 u1 的钱包转出
    r = client.post(f"/api/v1/wallets/{w1}/transfers",
                    headers={**H_USER2, "Idempotency-Key": "atk"},
                    json={"to_wallet_id": w2, "amount": 100})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
    # 资金未动
    r = client.get(f"/api/v1/wallets/{w1}/balance", headers=H_USER1)
    assert r.json()["available"] == 100
    assert client.get(f"/api/v1/wallets/{w2}/balance",
                      headers=H_USER2).json()["available"] == 0


def test_cannot_withdraw_from_other_wallet(client):
    w1 = make_wallet(client, H_USER1, "0xA")
    push_and_ingest(client, [deposit_event("d1", "0xA", 100, 1)], advance_to=3)
    r = client.post(f"/api/v1/wallets/{w1}/withdrawals",
                    headers={**H_USER2, "Idempotency-Key": "atk-w"},
                    json={"amount": 100})
    assert r.status_code == 404
    b = client.get(f"/api/v1/wallets/{w1}/balance", headers=H_USER1).json()
    assert b["available"] == 100 and b["holds"] == 0


def test_audit_endpoint_also_enforced(client):
    wid = make_wallet(client, H_USER1, "0xA")
    assert client.get(f"/api/v1/wallets/{wid}/audit",
                      headers=H_USER2).status_code == 404


def test_non_admin_cannot_access_admin_routes(client):
    for method, path, body in [
        ("post", "/api/v1/admin/scan", {}),
        ("post", "/api/v1/admin/chain/advance", {"blocks": 1}),
        ("post", "/api/v1/admin/chain/reorg", {"height": 1}),
        ("post", "/api/v1/admin/events/ingest",
         {"events": [{"event_id": "x"}]}),
        ("get", "/api/v1/admin/dead-letters", None),
    ]:
        r = getattr(client, method)(path, headers=H_USER1,
                                    json=body) if body is not None else \
            getattr(client, method)(path, headers=H_USER1)
        assert r.status_code == 403, (path, r.status_code)


def test_invalid_role_unauthorized(client):
    r = client.get("/api/v1/wallets/x/balance",
                   headers={"X-Principal-Id": "u1", "X-Principal-Role": "root"})
    assert r.status_code == 401
