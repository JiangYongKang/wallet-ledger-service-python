"""模拟链输入：缺字段/乱序/重复/非法金额分类归一化，不透底层异常。"""

from __future__ import annotations

import pytest

from wallet_ledger.errors import (
    InvalidAmountError,
    InvalidEventError,
    MissingFieldError,
    OutOfOrderError,
    RateLimitedError,
)
from wallet_ledger.normalizer import normalize_event, parse_amount

_BASE = {"tx_hash": "t1", "log_index": 0, "type": "deposit",
         "address": "wA", "amount": "10", "block_height": 5}


def test_valid_event_normalizes():
    ev = normalize_event(dict(_BASE))
    assert ev.amount == 10 and ev.block_height == 5
    assert ev.natural_key == ("t1", 0)


@pytest.mark.parametrize("drop", ["tx_hash", "log_index", "type", "address",
                                  "amount", "block_height"])
def test_missing_field_distinct(drop):
    payload = dict(_BASE)
    payload[drop] = None
    with pytest.raises(MissingFieldError) as ei:
        normalize_event(payload)
    assert ei.value.details["field"] == drop


def test_empty_string_is_missing():
    payload = dict(_BASE, address="   ")
    with pytest.raises(MissingFieldError):
        normalize_event(payload)


@pytest.mark.parametrize("bad", [0, -1, "0", "-5", "1.5", "abc", True, 1.2])
def test_invalid_amount_distinct(bad):
    with pytest.raises(InvalidAmountError):
        parse_amount(bad)
    payload = dict(_BASE, amount=bad)
    with pytest.raises(InvalidAmountError):
        normalize_event(payload)


def test_unknown_type_distinct():
    with pytest.raises(InvalidEventError) as ei:
        normalize_event(dict(_BASE, type="mint"))
    assert ei.value.details["field"] == "type"


def test_http_error_codes_distinguishable(client):
    headers = {"X-Principal": "principal:indexer"}
    r = client.post("/chain/events", json=dict(_BASE, amount="bad"), headers=headers)
    assert r.status_code == 400 and r.json()["code"] == "INVALID_AMOUNT"

    r = client.post("/chain/events", json={}, headers=headers)
    assert r.status_code == 400 and r.json()["code"] == "MISSING_FIELD"

    r = client.post("/chain/events", json=dict(_BASE, type="mint"), headers=headers)
    assert r.status_code == 400 and r.json()["code"] == "INVALID_EVENT"


def test_scan_out_of_order_tip_rejected(env):
    sc, idx = env["scanner"], env["indexer"]
    sc.scan_batch(idx, tip_height=10, events=[])
    with pytest.raises(OutOfOrderError):
        sc.scan_batch(idx, tip_height=9, events=[])


def test_scan_oversized_batch_rejected(env):
    sc, idx = env["scanner"], env["indexer"]
    events = [(f"t{i}", 0, 1) for i in range(1000)]
    with pytest.raises(RateLimitedError) as ei:
        sc.scan_batch(idx, tip_height=11, events=events)
    assert ei.value.details["reason"] == "batch_size"
