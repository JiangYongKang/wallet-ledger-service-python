"""测试公共夹具：每个用例独立的内存状态 + 事件流水日志捕获。

单测运行时打印事件流水与判定依据（-s 或捕获到 caplog 均可见）。
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from wallet_ledger.api import deps
from wallet_ledger.domain import PrincipalRole
from wallet_ledger.repository import LedgerRepository
from wallet_ledger.scanner import ConfirmationScanner, ScanPolicy
from wallet_ledger.security import register_principal
from wallet_ledger.service import LedgerService


@pytest.fixture
def env():
    """服务层组件：独立 repo/service/scanner。"""
    repo = LedgerRepository()
    service = LedgerService(repo)
    scanner = ConfirmationScanner(
        service,
        ScanPolicy(required_confirmations=3, finalization_depth=10,
                   max_batch_size=50, max_inflight=4, burst=100),
    )
    alice = register_principal("alice", PrincipalRole.USER, {"wA", "wB"})
    bob = register_principal("bob", PrincipalRole.USER, {"wC"})
    indexer = register_principal("indexer", PrincipalRole.INDEXER, set())
    admin = register_principal("admin", PrincipalRole.ADMIN, set())
    service.create_wallet(alice, "wA")
    service.create_wallet(alice, "wB")
    service.create_wallet(bob, "wC")
    yield {
        "repo": repo,
        "service": service,
        "scanner": scanner,
        "alice": alice,
        "bob": bob,
        "indexer": indexer,
        "admin": admin,
    }
    from wallet_ledger.security import REGISTRY
    REGISTRY.clear()


@pytest.fixture
def client():
    """HTTP 层：每用例重置全局单例。"""
    deps.reset_state()
    from wallet_ledger.app import app
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()
    deps.reset_state()


@pytest.fixture(autouse=True)
def _event_log(caplog):
    """让所有用例都捕获并在失败时可见事件流水日志。"""
    caplog.set_level(logging.INFO, logger="wallet_ledger")
    yield
