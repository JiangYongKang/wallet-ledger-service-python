"""调用方身份与钱包级权限约束。

身份通过请求头显式传递（本地服务的简化方案）：
- X-Principal-Id：调用方身份标识，必填。
- X-Principal-Role：user（默认）或 admin。

权限规则：
- 钱包资源只能由属主访问；属主不存在或钱包不存在时返回*完全相同*的 404，
  避免通过状态码差异推断目标资源是否存在。
- /admin 路由仅 admin 角色可访问，越权返回 403（管理端点的存在性不敏感）。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.errors import NotFound, PermissionDenied, Unauthorized

WALLET_NOT_FOUND_MESSAGE = "wallet not found"


@dataclass(frozen=True)
class Principal:
    principal_id: str
    role: str  # "user" | "admin"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def authenticate(header_value_id: str | None, header_value_role: str | None) -> Principal:
    """从请求头解析调用方身份。缺失/非法身份统一 401，不区分具体原因。"""
    if not header_value_id or not header_value_id.strip():
        raise Unauthorized("missing principal identity")
    role = (header_value_role or "user").strip().lower()
    if role not in ("user", "admin"):
        raise Unauthorized("invalid principal role")
    return Principal(principal_id=header_value_id.strip(), role=role)


def require_admin(principal: Principal) -> None:
    if not principal.is_admin:
        raise PermissionDenied("admin role required")


def authorize_wallet(principal: Principal, owner_principal: str | None) -> None:
    """校验属主。owner_principal 为 None（钱包不存在）时同样返回 404，
    与越权访问的响应不可区分。"""
    if owner_principal is None or owner_principal != principal.principal_id:
        # 注意：错误信息固定，不携带 wallet_id / 真实原因。
        raise NotFound(WALLET_NOT_FOUND_MESSAGE)
