"""调用方身份与钱包归属授权。

策略：
- 缺失/无法识别的凭据 -> UNAUTHENTICATED (401)；
- 已认证但角色不符 -> FORBIDDEN (403)；
- 访问/操作不在自身钱包集合内的地址 -> RESOURCE_NOT_FOUND (404)，
  与“地址不存在”返回完全一致，不泄露目标资源是否存在。
"""

from __future__ import annotations

from .domain import Principal, PrincipalRole
from .errors import ForbiddenError, ResourceNotFoundError, UnauthenticatedError

# 演示用内存主体表（生产应由鉴权层签发 token）。
REGISTRY: dict[str, Principal] = {}


def register_principal(
    principal_id: str,
    role: PrincipalRole,
    wallets: frozenset[str] | set[str] = frozenset(),
) -> Principal:
    principal = Principal(
        principal_id=principal_id, role=role, wallets=frozenset(wallets)
    )
    REGISTRY[principal_id] = principal
    return principal


def authenticate(token: str | None) -> Principal:
    """token 形如 ``principal:<id>``（indexer 为 ``principal:indexer``）。

    无效或缺失 -> UnauthenticatedError。
    """
    if not token:
        raise UnauthenticatedError("missing credentials")
    prefix = "principal:"
    if not token.startswith(prefix):
        raise UnauthenticatedError("malformed credentials")
    principal_id = token[len(prefix) :]
    principal = REGISTRY.get(principal_id)
    if principal is None:
        raise UnauthenticatedError("unknown principal")
    return principal


def require_role(principal: Principal, role: PrincipalRole) -> None:
    if principal.role != role:
        # 角色不允许不涉及资源存在性，明确 403。
        raise ForbiddenError(
            f"role {principal.role.value!r} requires {role.value!r}",
            details={"required_role": role.value},
        )


def authorize_wallet(principal: Principal, address: str) -> None:
    """不在自身钱包范围 -> ResourceNotFoundError（不泄露存在性）。"""
    if address not in principal.wallets:
        # 与“地址不存在”使用完全相同的错误码/状态/文案，避免侧信道。
        raise ResourceNotFoundError("wallet not found", details={"address": address})
