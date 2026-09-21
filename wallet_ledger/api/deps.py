"""FastAPI 共享依赖：身份、仓储、服务、扫描器单例（进程内）。"""

from __future__ import annotations

from fastapi import Header

from ..domain import Principal, PrincipalRole
from ..repository import LedgerRepository
from ..scanner import ConfirmationScanner, ScanPolicy
from ..security import REGISTRY, authenticate, register_principal
from ..service import LedgerService

_repo = LedgerRepository()
_service = LedgerService(_repo)
_scanner = ConfirmationScanner(_service, ScanPolicy())


def get_repo() -> LedgerRepository:
    return _repo


def get_service() -> LedgerService:
    return _service


def get_scanner() -> ConfirmationScanner:
    return _scanner


def get_principal(x_principal: str | None = Header(default=None)) -> Principal:
    return authenticate(x_principal)


def reset_state() -> None:
    """测试辅助：重置内存状态并重建单例。"""
    global _repo, _service, _scanner
    _repo = LedgerRepository()
    _service = LedgerService(_repo)
    _scanner = ConfirmationScanner(_service, ScanPolicy())
    REGISTRY.clear()
    register_principal("alice", PrincipalRole.USER, {"wA", "wB"})
    register_principal("bob", PrincipalRole.USER, {"wC"})
    register_principal("indexer", PrincipalRole.INDEXER, set())
    register_principal("admin", PrincipalRole.ADMIN, set())


# 预置演示主体，也可由测试在 reset 后自行注册。
register_principal("alice", PrincipalRole.USER, {"wA", "wB"})
register_principal("bob", PrincipalRole.USER, {"wC"})
register_principal("indexer", PrincipalRole.INDEXER, set())
register_principal("admin", PrincipalRole.ADMIN, set())
