"""跨入口资金守恒与结论确定性：程序化构造交错的充值/转账/确认/回退序列。"""
from __future__ import annotations

from tests.conftest import (
    H_ADMIN, H_USER1, H_USER2, balance, deposit_event,
    make_wallet, push_and_ingest,
)


def _total(client, wallets):
    total = 0
    for wid, h in wallets:
        b = balance(client, h, wid)
        assert b["available"] >= 0 and b["holds"] >= 0
        total += b["available"] + b["holds"] + b["pending_in"] - b["clawback_due"]
    return total


def test_full_lifecycle_conservation_and_replay_determinism(client):
    w1 = make_wallet(client, H_USER1, "0xA")
    w2 = make_wallet(client, H_USER2, "0xB")
    wallets = [(w1, H_USER1), (w2, H_USER2)]

    # 充值 200 并确认
    push_and_ingest(client, [deposit_event("d1", "0xA", 200, 1)], advance_to=3)
    assert _total(client, wallets) == 200

    # 转账 50、提现冻结 30
    client.post(f"/api/v1/wallets/{w1}/transfers",
                headers={**H_USER1, "Idempotency-Key": "tr1"},
                json={"to_wallet_id": w2, "amount": 50})
    client.post(f"/api/v1/wallets/{w1}/withdrawals",
                headers={**H_USER1, "Idempotency-Key": "wd1"},
                json={"amount": 30})
    assert _total(client, wallets) == 200  # 转账+冻结不改变系统总额

    # 重组 d1：w1 可用 120 可覆盖 -> 扣 120，剩余 80 挂账
    r = client.post("/api/v1/admin/chain/reorg", headers=H_ADMIN, json={"height": 1})
    assert r.json()["reverted"] == ["d1"]
    b1 = balance(client, H_USER1, w1)
    assert b1["available"] == 0 and b1["holds"] == 30 and b1["clawback_due"] == 80
    # w2 有 50，w1 挂账 80：系统净额 = 0 + 30 + 50 - 80 == 0
    assert _total(client, wallets) == 0

    # 重新充值 200（新链），确认后：80 偿挂账，120 可用；hold 30 不变
    push_and_ingest(client, [deposit_event("d2", "0xA", 200, 1)], advance_to=3)
    b1 = balance(client, H_USER1, w1)
    assert b1["available"] == 120 and b1["clawback_due"] == 0 and b1["holds"] == 30
    # 系统净额恢复为 200（w1: 120+30, w2: 50）
    assert _total(client, wallets) == 200

    # 结论确定性：审计快照在重复读取间稳定
    a1 = client.get(f"/api/v1/wallets/{w1}/audit", headers=H_USER1).json()
    a2 = client.get(f"/api/v1/wallets/{w1}/audit", headers=H_USER1).json()
    assert a1["balance"] == a2["balance"]
    assert a1["entries"] == a2["entries"]


def test_transfer_conservation_chain(client):
    w1 = make_wallet(client, H_USER1, "0xA")
    w2 = make_wallet(client, H_USER2, "0xB")
    push_and_ingest(client, [deposit_event("d", "0xA", 100, 1)], advance_to=3)
    for i, amt in enumerate([10, 20, 30, 40]):
        r = client.post(f"/api/v1/wallets/{w1}/transfers",
                        headers={**H_USER1, "Idempotency-Key": f"k{i}"},
                        json={"to_wallet_id": w2, "amount": amt})
        assert r.status_code == 200
    b1 = balance(client, H_USER1, w1)
    b2 = balance(client, H_USER2, w2)
    assert b1["available"] == 0 and b2["available"] == 100
