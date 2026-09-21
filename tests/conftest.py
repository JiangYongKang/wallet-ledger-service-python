"""测试夹具：每个测试用例使用独立服务，并在结束时打印事件流水与判定依据。"""
from __future__ import annotations

import logging
import os

import pytest

# 每个用例前固定配置：3 确认 / 6 最终性
os.environ.setdefault("CONFIRMATIONS_REQUIRED", "3")
os.environ.setdefault("FINALITY_DEPTH", "6")
os.environ.setdefault("RATE_LIMIT_CAPACITY", "1000")

from fastapi.testclient import TestClient

import main as main_module
from app.api.deps import Services
from app.config import reset_settings_cache

reset_settings_cache()

logger = logging.getLogger("wallet-ledger.tests")
logging.basicConfig(level=logging.INFO, format="%(message)s")


@pytest.fixture
def svc():
    # 每个用例都用默认配置重建 Services，并把它装到路由模块直接绑定的全局单例上
    #（routes 内 `from ...deps import services`），既保证隔离，也避免跨用例污染。
    from app.api import routes_admin, routes_wallets
    services = Services()
    routes_wallets.services = services
    routes_admin.services = services
    main_module.services = services
    return services


@pytest.fixture
def client(svc):
    return TestClient(main_module.app)


@pytest.fixture(autouse=True)
def _print_flow(request, svc):
    """每个用例结束后打印账本事件流水，便于人工核对判定依据。"""
    yield
    lines = [f"\n===== FLOW: {request.node.name} ====="]
    for wid, w in sorted(svc.store.wallets.items()):
        b = svc.ledger.get_balance(wid).as_dict()
        evs = svc.store.list_events_by_address(w.address)
        lines.append(f"wallet {wid} addr={w.address} balance={b}")
        for e in evs:
            lines.append(
                f"  event {e.event_id} {e.kind.value} amount={e.amount} "
                f"h={e.block_height} status={e.status.value} "
                f"reverted_by={e.reverted_by}")
        for en in svc.store.entries_for_wallet(wid):
            lines.append(
                f"  entry {en.kind} amount={en.amount} "
                f"ref_ev={en.ref_event_id} ref_req={en.ref_request_id} "
                f"balance_after={en.balance_after}")
    if svc.store.dead_letters:
        lines.append("dead-letters:")
        for d in svc.store.dead_letters:
            lines.append(f"  {d.reason_code}: {d.reason} raw={d.raw}")
    logger.info("\n".join(lines))


H_USER1 = {"X-Principal-Id": "u1"}
H_USER2 = {"X-Principal-Id": "u2"}
H_ADMIN = {"X-Principal-Id": "ops", "X-Principal-Role": "admin"}


def make_wallet(client, headers, address, wallet_id=None):
    payload = {"address": address}
    if wallet_id:
        payload["wallet_id"] = wallet_id
    r = client.post("/api/v1/wallets", headers=headers, json=payload)
    assert r.status_code == 201, r.text
    return r.json()["wallet_id"]


def balance(client, headers, wid):
    r = client.get(f"/api/v1/wallets/{wid}/balance", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def deposit_event(eid, addr, amount, height, tx=None, kind="deposit"):
    return {
        "event_id": eid, "address": addr, "kind": kind,
        "amount": amount, "block_height": height, "tx_hash": tx or f"tx-{eid}",
    }


def seed_chain(client, height=1, admin=H_ADMIN):
    """空挖出块，把链推进到指定高度（供直接摄入事件时满足 block_height 校验）。"""
    client.post("/api/v1/admin/chain/advance", headers=admin,
                json={"blocks": height})


def push_block_with_events(client, events, admin=H_ADMIN):
    """把一组事件放进同一个新区块（高度自增），返回该块真实高度。"""
    h = client.post("/api/v1/admin/chain/blocks", headers=admin,
                    json={"events": events}).json()["chain_height"]
    for e in events:
        e["block_height"] = h
    r = client.post("/api/v1/admin/events/ingest", headers=admin, json={"events": events})
    assert r.status_code == 200, r.text
    return h


def push_and_ingest(client, events, advance_to=None, admin=H_ADMIN):
    """在当前链顶之上，为每个事件顺序追加区块（每事件一块），再摄入。

    事件里自带的 block_height 仅用于声明，会被重写为链上真实高度，
    从而正确模拟"事件出现在链顶后续区块"。
    返回 (最新事件真实高度, 最后一次摄入结果)。
    """
    result = None
    last_height = None
    first_height = None
    grouped: list[list] = [[e] for e in events]
    for group in grouped:
        h = client.post("/api/v1/admin/chain/blocks", headers=admin,
                        json={"events": group}).json()["chain_height"]
        if first_height is None:
            first_height = h
        for e in group:
            e["block_height"] = h
        r = client.post("/api/v1/admin/events/ingest", headers=admin, json={"events": group})
        assert r.status_code == 200, r.text
        result = r.json()
        last_height = h
    if advance_to is not None and advance_to > last_height:
        client.post("/api/v1/admin/chain/advance", headers=admin,
                    json={"blocks": advance_to - last_height})
        last_height = advance_to
    return first_height, last_height, result
