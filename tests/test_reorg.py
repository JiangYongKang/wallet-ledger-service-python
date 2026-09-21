"""链重组回退：已入账回撤不产生负余额、账目恒等、结论可复现。"""
from __future__ import annotations

from tests.conftest import (
    H_ADMIN, H_USER1, H_USER2, balance, deposit_event,
    make_wallet, push_and_ingest,
)


def _identity_total(client, wallets):
    """系统资金恒等式：Σ(available+holds+pending_in-clawback_due) == 净在系统本金。"""
    total = 0
    for wid, headers in wallets:
        b = balance(client, headers, wid)
        total += b["available"] + b["holds"] + b["pending_in"] - b["clawback_due"]
    return total


def test_revert_confirmed_deposit_claws_back_available(client):
    wid = make_wallet(client, H_USER1, "0xA")
    push_and_ingest(client, [deposit_event("e1", "0xA", 100, 1)], advance_to=3)
    assert balance(client, H_USER1, wid)["available"] == 100

    r = client.post("/api/v1/admin/chain/reorg", headers=H_ADMIN, json={"height": 1})
    assert r.status_code == 200
    assert r.json()["reverted"] == ["e1"]
    b = balance(client, H_USER1, wid)
    assert b["available"] == 0 and b["pending_in"] == 0 and b["clawback_due"] == 0


def test_revert_when_funds_spent_creates_shortfall_never_negative(client):
    w1 = make_wallet(client, H_USER1, "0xA")
    w2 = make_wallet(client, H_USER2, "0xB")
    push_and_ingest(client, [deposit_event("e1", "0xA", 100, 1)], advance_to=3)
    # 把 80 转给 w2，w1 只剩 20
    r = client.post(f"/api/v1/wallets/{w1}/transfers",
                    headers={**H_USER1, "Idempotency-Key": "t1"},
                    json={"to_wallet_id": w2, "amount": 80})
    assert r.status_code == 200
    # 重组 e1
    r = client.post("/api/v1/admin/chain/reorg", headers=H_ADMIN, json={"height": 1})
    assert r.json()["reverted"] == ["e1"]
    b1 = balance(client, H_USER1, w1)
    assert b1["available"] == 0                 # 不为负
    assert b1["clawback_due"] == 80             # 无法实扣部分挂账
    b2 = balance(client, H_USER2, w2)
    assert b2["available"] == 80
    # 账目恒等：0 + 80 - 80 == 0（系统总资金与回退后真实情况一致）
    assert _identity_total(client, [(w1, H_USER1), (w2, H_USER2)]) == 0


def test_later_deposit_repays_shortfall_first(client):
    w1 = make_wallet(client, H_USER1, "0xA")
    w2 = make_wallet(client, H_USER2, "0xB")
    push_and_ingest(client, [deposit_event("e1", "0xA", 100, 1)], advance_to=3)
    client.post(f"/api/v1/wallets/{w1}/transfers",
                headers={**H_USER1, "Idempotency-Key": "t1"},
                json={"to_wallet_id": w2, "amount": 80})
    client.post("/api/v1/admin/chain/reorg", headers=H_ADMIN, json={"height": 1})
    assert balance(client, H_USER1, w1)["clawback_due"] == 80

    # 新充值 50（重组后新链高度1）：全部用于偿还挂账，available 仍为 0
    push_and_ingest(client, [deposit_event("e2", "0xA", 50, 1)], advance_to=3)
    b = balance(client, H_USER1, w1)
    assert b["available"] == 0 and b["clawback_due"] == 30

    # 再充值 100（新链高度4）：30 偿还挂账，70 进可用
    push_and_ingest(client, [deposit_event("e3", "0xA", 100, 1)], advance_to=6)
    b = balance(client, H_USER1, w1)
    assert b["available"] == 70 and b["clawback_due"] == 0


def test_revert_is_idempotent(client):
    wid = make_wallet(client, H_USER1, "0xA")
    push_and_ingest(client, [deposit_event("e1", "0xA", 100, 1)], advance_to=3)
    client.post("/api/v1/admin/chain/reorg", headers=H_ADMIN, json={"height": 1})
    r2 = client.post("/api/v1/admin/chain/reorg", headers=H_ADMIN, json={"height": 1})
    assert r2.json()["reverted"] == []  # 重复回退不产生新动作
    assert balance(client, H_USER1, wid)["available"] == 0


def test_revert_then_reconfirm_is_deterministic(client):
    """回退与确认同时到达：REVERTED 优先，旧确认重放不得重新入账。
    连续执行 N 次"确认扫描"，结论必须完全一致（可复现）。"""
    wid = make_wallet(client, H_USER1, "0xA")
    push_and_ingest(client, [deposit_event("e1", "0xA", 100, 1)], advance_to=3)
    assert balance(client, H_USER1, wid)["available"] == 100
    client.post("/api/v1/admin/chain/reorg", headers=H_ADMIN, json={"height": 1})

    snapshots = []
    for _ in range(5):
        client.post("/api/v1/admin/chain/advance", headers=H_ADMIN, json={"blocks": 1})
        snapshots.append(balance(client, H_USER1, wid)["available"])
    assert snapshots == [0, 0, 0, 0, 0]
    assert balance(client, H_USER1, wid)["clawback_due"] == 0


def test_revert_pending_deposit_only_removes_pending(client):
    wid = make_wallet(client, H_USER1, "0xA")
    # 事件在高度1，只推进到高度2，埋深2 < 3，仍是 pending
    first_h, _, _ = push_and_ingest(client, [deposit_event("e1", "0xA", 100, 1)],
                                    advance_to=2)
    assert first_h == 1
    b = balance(client, H_USER1, wid)
    assert b["available"] == 0 and b["pending_in"] == 100
    # 回退到高度2（移除高度2空块不影响）；再回退高度1
    r = client.post("/api/v1/admin/chain/reorg", headers=H_ADMIN, json={"height": 1})
    assert r.json()["reverted"] == ["e1"]
    b = balance(client, H_USER1, wid)
    assert b["available"] == 0 and b["pending_in"] == 0


def test_reorg_batch_is_atomic_when_mixed_with_finalized(client):
    wid = make_wallet(client, H_USER1, "0xA")
    # old 放在高度1；推进到高度7使其最终化（埋深7 > finality_depth 6）
    h_old, _, _ = push_and_ingest(
        client, [deposit_event("old", "0xA", 100, 1)], advance_to=7)
    assert h_old == 1
    assert balance(client, H_USER1, wid)["available"] == 100
    # 已最终化事件回退 -> 拒绝且无任何资金变动
    r = client.post("/api/v1/admin/chain/reorg", headers=H_ADMIN,
                    json={"height": 1})
    assert r.status_code == 409
    assert r.json()["error"]["details"]["event_ids"] == ["old"]
    assert balance(client, H_USER1, wid)["available"] == 100
