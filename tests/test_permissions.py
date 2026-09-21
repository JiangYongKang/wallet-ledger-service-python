"""身份与权限：角色约束、越权统一 404 且不泄露资源是否存在。"""

from __future__ import annotations

import pytest

from wallet_ledger.errors import (
    ForbiddenError,
    ResourceNotFoundError,
    UnauthenticatedError,
)
from wallet_ledger.security import authenticate, require_role

# ---- 服务层 ----


def test_unauthenticated_missing_token():
    with pytest.raises(UnauthenticatedError):
        authenticate(None)
    with pytest.raises(UnauthenticatedError):
        authenticate("garbage-token")
    with pytest.raises(UnauthenticatedError):
        authenticate("principal:nobody")


def test_user_cannot_call_indexer_role(env):
    s, alice = env["service"], env["alice"]
    from wallet_ledger.normalizer import normalize_event
    ev = normalize_event({"tx_hash": "t", "log_index": 0, "type": "deposit",
                          "address": "wA", "amount": "1", "block_height": 1})
    with pytest.raises(ForbiddenError):
        s.ingest_event(alice, ev)
    require_role  # noqa: B018  保持导入可见


def test_cross_tenant_access_indistinguishable_from_missing(env):
    s, bob = env["service"], env["bob"]
    # bob 访问 alice 的 wA（存在）与一个根本不存在的地址：错误完全一致
    for address in ("wA", "wZ-not-exist"):
        with pytest.raises(ResourceNotFoundError) as ei:
            s.get_balance(bob, address)
        assert ei.value.code == "RESOURCE_NOT_FOUND"
        assert ei.value.http_status == 404


def test_cross_tenant_transfer_target_404(env):
    s, alice = env["service"], env["alice"]
    # 目标 wC 属于 bob：alice 不可见，统一 404，不泄露 wC 存在
    with pytest.raises(ResourceNotFoundError):
        s.transfer(alice, "wA", "wC", 1, "k")


# ---- HTTP 层：响应同构，无侧信道 ----


def test_http_unauthenticated_and_forbidden(client):
    r = client.get("/wallets/wA/balance")
    assert r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED"

    # 普通用户尝试链入口
    r = client.post(
        "/chain/events",
        json={"tx_hash": "t", "log_index": 0, "type": "deposit",
              "address": "wA", "amount": "1", "block_height": 1},
        headers={"X-Principal": "principal:alice"},
    )
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"


def test_http_cross_tenant_response_identical(client):
    client.post("/wallets", json={"address": "wA"},
                headers={"X-Principal": "principal:alice"})
    headers_alice = {"X-Principal": "principal:alice"}
    r_existing = client.get("/wallets/wC/balance", headers=headers_alice)  # 属于 bob
    r_missing = client.get("/wallets/wZzz/balance", headers=headers_alice)  # 不存在
    assert r_existing.status_code == r_missing.status_code == 404
    # 状态码与 code 完全一致；body 不含“存在但无权限”之类区分信息
    assert r_existing.json()["code"] == r_missing.json()["code"] == "RESOURCE_NOT_FOUND"
