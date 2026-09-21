"""确认数与最终性：未达确认数与可用余额分开展示；达到后入账。"""

from __future__ import annotations

from wallet_ledger.normalizer import normalize_event


def _ingest(service, indexer, tx, address, amount, height):
    event = normalize_event(
        {"tx_hash": tx, "log_index": 0, "type": "deposit",
         "address": address, "amount": str(amount), "block_height": height}
    )
    return service.ingest_event(indexer, event)


def test_pending_separated_from_available(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    rec = _ingest(s, idx, "t1", "wA", 100, 10)

    bal = s.get_balance(alice, "wA")
    assert bal.available == 0
    assert bal.pending_in == 100  # 未达最终性，单独展示

    # 2/3 确认：仍不可用
    s.apply_confirmations(idx, [rec.event.natural_key], 2, 12, 3)
    assert s.get_balance(alice, "wA").available == 0

    # 3/3 确认：进入可用
    s.apply_confirmations(idx, [rec.event.natural_key], 3, 13, 3)
    bal = s.get_balance(alice, "wA")
    assert bal.available == 100 and bal.pending_in == 0


def test_repeated_confirmation_is_idempotent(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    rec = _ingest(s, idx, "t1", "wA", 100, 10)
    for _ in range(5):
        s.apply_confirmations(idx, [rec.event.natural_key], 3, 13, 3)
    assert s.get_balance(alice, "wA").available == 100
    # 流水里确认只入账一次
    confirms = [e for e in s.list_entries(alice, "wA")
                if e.txn_type.value == "deposit_confirm"]
    assert len(confirms) == 1


def test_finalized_event_cannot_reorg(env):
    s, idx, alice = env["service"], env["indexer"], env["alice"]
    rec = _ingest(s, idx, "t1", "wA", 100, 10)
    s.apply_confirmations(idx, [rec.event.natural_key], 10, 20, 3,
                          finalization_depth=10)
    assert s.repo.get_event(rec.event.natural_key).finalized is True
    affected = s.apply_reorg(idx, [rec.event.natural_key], 20)
    assert affected == []  # 最终性后不可回撤
    assert s.get_balance(alice, "wA").available == 100


def test_scanner_confirms_and_advances_tip(env):
    s, sc, idx, alice = env["service"], env["scanner"], env["indexer"], env["alice"]
    _ingest(s, idx, "t1", "wA", 40, 5)
    out = sc.scan_batch(idx, tip_height=8,
                        events=[("t1", 0, 3)])
    assert out["status"] == "ok" and out["tip_height"] == 8
    assert s.get_balance(alice, "wA").available == 40
