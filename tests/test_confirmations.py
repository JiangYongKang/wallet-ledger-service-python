"""确认与最终性：未达确认数只展示 pending_in，达到后计入 available。"""
from __future__ import annotations

from tests.conftest import (
    H_ADMIN, H_USER1, balance, deposit_event, make_wallet,
    push_and_ingest, push_block_with_events,
)


def test_pending_separated_from_available(client):
    wid = make_wallet(client, H_USER1, "0xA")
    push_and_ingest(client, [deposit_event("e1", "0xA", 100, 1)], advance_to=2)
    b = balance(client, H_USER1, wid)
    assert b["available"] == 0
    assert b["pending_in"] == 100  # 2/3 确认，未计入可用


def test_confirmation_moves_funds_to_available(client):
    wid = make_wallet(client, H_USER1, "0xA")
    # e1 在高度1，e2 在高度2
    push_and_ingest(client, [
        deposit_event("e1", "0xA", 100, 1),
        deposit_event("e2", "0xA", 50, 2),
    ], advance_to=3)
    b = balance(client, H_USER1, wid)
    # h=3 时 e1 埋深3 -> 确认；e2 埋深2 -> 仍 pending
    assert b["available"] == 100
    assert b["pending_in"] == 50

    client.post("/api/v1/admin/chain/advance", headers=H_ADMIN, json={"blocks": 1})
    b = balance(client, H_USER1, wid)
    assert b["available"] == 150
    assert b["pending_in"] == 0


def test_finality_depth_blocks_reorg(client):
    wid = make_wallet(client, H_USER1, "0xA")
    push_and_ingest(client, [deposit_event("e1", "0xA", 100, 1)], advance_to=7)
    # h=7 时 e1 埋深7 > finality_depth 6 -> finalized
    b = balance(client, H_USER1, wid)
    assert b["available"] == 100

    r = client.post("/api/v1/admin/chain/reorg", headers=H_ADMIN, json={"height": 1})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "finalized_reorg"
    assert r.json()["error"]["details"]["event_ids"] == ["e1"]
    # 资金不受影响
    assert balance(client, H_USER1, wid)["available"] == 100


def test_finality_boundary_depth_equals_depth_is_reorgable(client):
    # 埋深 == finality_depth（6）仍可回退；> 6 才最终化
    wid = make_wallet(client, H_USER1, "0xA")
    push_and_ingest(client, [deposit_event("e1", "0xA", 100, 1)], advance_to=6)
    assert balance(client, H_USER1, wid)["available"] == 100
    r = client.post("/api/v1/admin/chain/reorg", headers=H_ADMIN, json={"height": 1})
    assert r.status_code == 200, r.text
    assert r.json()["reverted"] == ["e1"]
    assert balance(client, H_USER1, wid)["available"] == 0
