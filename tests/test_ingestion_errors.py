"""模拟链输入归一化：缺字段、乱序、重复、非法金额，错误可区分且不静默丢弃。"""
from __future__ import annotations

from tests.conftest import H_ADMIN, make_wallet, seed_chain


def _ingest(client, events, seeded=True):
    if seeded:
        seed_chain(client, 1)
    return client.post("/api/v1/admin/events/ingest", headers=H_ADMIN,
                       json={"events": events})


def test_missing_fields_are_classified(client):
    make_wallet(client, {"X-Principal-Id": "u1"}, "0xA")
    r = _ingest(client, [
        {"address": "0xA", "kind": "deposit", "amount": 10,
         "block_height": 1, "tx_hash": "t1"},  # 缺 event_id
        {"event_id": "e2", "kind": "deposit", "amount": 10,
         "block_height": 1, "tx_hash": "t2"},  # 缺 address
        {"event_id": "e3", "address": "0xA", "amount": 10,
         "block_height": 1, "tx_hash": "t3"},  # 缺 kind
        {"event_id": "e4", "address": "0xA", "kind": "deposit",
         "block_height": 1, "tx_hash": "t4"},  # 缺 amount
    ])
    reasons = {x["reason"] for x in r.json()["rejected"]}
    assert reasons == {"missing_field"}


def test_invalid_amounts_are_classified(client):
    make_wallet(client, {"X-Principal-Id": "u1"}, "0xA")
    base = {"event_id": "x", "address": "0xA", "kind": "deposit",
            "block_height": 1, "tx_hash": "t"}
    # null 金额属于"缺失字段"，在 missing_field 用例覆盖；此处均为类型/取值非法
    cases = [-5, 0, "abc", "12.5", True]
    events = []
    for i, amt in enumerate(cases):
        e = dict(base, event_id=f"a{i}", tx_hash=f"t{i}", amount=amt)
        events.append(e)
    r = _ingest(client, events)
    assert len(r.json()["rejected"]) == len(cases)
    assert all(x["reason"] == "invalid_amount" for x in r.json()["rejected"])


def test_nan_inf_amount_rejected_at_normalizer(svc):
    # NaN/Inf 无法通过标准 JSON 传输，直接在归一化层验证其被拒绝
    import math
    from app.errors import ValidationError
    base = {"event_id": "n", "address": "0xA", "kind": "deposit",
            "block_height": 1, "tx_hash": "t"}
    for bad in (float("nan"), float("inf"), float("-inf")):
        raw = dict(base, amount=bad)
        try:
            svc.ingestion.normalize(raw)
            assert False, f"{bad} should be rejected"
        except ValidationError as ex:
            assert ex.reason == "invalid_amount"


def test_valid_string_amount_normalized(client):
    make_wallet(client, {"X-Principal-Id": "u1"}, "0xA")
    r = _ingest(client, [
        {"event_id": "s1", "address": "0xA", "kind": "deposit",
         "amount": "100", "block_height": 1, "tx_hash": "t1"},
        {"event_id": "s2", "address": "0xA", "kind": "deposit",
         "amount": 50.0, "block_height": 1, "tx_hash": "t2"},
    ])
    assert sorted(r.json()["accepted"]) == ["s1", "s2"]


def test_invalid_kind_height_tx_classified(client):
    make_wallet(client, {"X-Principal-Id": "u1"}, "0xA")
    r = _ingest(client, [
        {"event_id": "k", "address": "0xA", "kind": "airdrop",
         "amount": 10, "block_height": 1, "tx_hash": "t1"},
        {"event_id": "h", "address": "0xA", "kind": "deposit",
         "amount": 10, "block_height": -1, "tx_hash": "t2"},
        {"event_id": "x", "address": "0xA", "kind": "deposit",
         "amount": 10, "block_height": 1, "tx_hash": ""},  # 空=缺失
    ])
    reasons = {x["reason"] for x in r.json()["rejected"]}
    assert reasons == {"invalid_kind", "invalid_height", "missing_field"}
    # 每个具体事件的错误原因可区分
    by_id = {x["event_id"]: x["reason"] for x in r.json()["rejected"]}
    assert by_id["k"] == "invalid_kind"
    assert by_id["h"] == "invalid_height"
    assert by_id["x"] == "missing_field"


def test_future_block_and_unknown_address_rejected_not_silently_dropped(client):
    make_wallet(client, {"X-Principal-Id": "u1"}, "0xA")
    # 先把链推进到高度1：高度5事件 -> future_block；未绑定地址 -> unknown_address
    seed_chain(client, 1)
    r = _ingest(client, [
        {"event_id": "f", "address": "0xA", "kind": "deposit",
         "amount": 10, "block_height": 5, "tx_hash": "t1"},
        {"event_id": "u", "address": "0xZZZ", "kind": "deposit",
         "amount": 10, "block_height": 1, "tx_hash": "t2"},
    ], seeded=False)
    reasons = {x["reason"] for x in r.json()["rejected"]}
    assert reasons == {"future_block", "unknown_address"}
    # 全部进死信，不静默丢弃
    dl = client.get("/api/v1/admin/dead-letters", headers=H_ADMIN).json()
    codes = {d["reason_code"] for d in dl["dead_letters"]}
    assert {"future_block", "unknown_address"} <= codes


def test_partial_batch_good_events_still_accepted(client):
    make_wallet(client, {"X-Principal-Id": "u1"}, "0xA")
    r = _ingest(client, [
        {"event_id": "good", "address": "0xA", "kind": "deposit",
         "amount": 10, "block_height": 1, "tx_hash": "tg"},
        {"event_id": "bad", "address": "0xA", "kind": "deposit",
         "amount": -1, "block_height": 1, "tx_hash": "tb"},
    ])
    assert r.json()["accepted"] == ["good"]
    assert len(r.json()["rejected"]) == 1


def test_non_object_event_does_not_leak_internal_error(client):
    make_wallet(client, {"X-Principal-Id": "u1"}, "0xA")
    r = _ingest(client, ["not-an-object", 123])
    assert r.status_code == 200  # 批量入口不炸
    assert len(r.json()["rejected"]) == 2
    assert all(x["reason"] == "missing_field" for x in r.json()["rejected"])
