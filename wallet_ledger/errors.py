"""账本服务错误类型。

每类错误带有可区分的 ``code`` 与 HTTP 状态码，便于调用方与测试区分
“重复提交 / 语义冲突 / 越权 / 非法输入 / 限流”等不同原因。
"""

from __future__ import annotations


class LedgerError(Exception):
    """所有账本领域错误的基类。"""

    code: str = "LEDGER_ERROR"
    http_status: int = 400

    def __init__(self, message: str = "", *, details: dict | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details: dict = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


# ---- 输入归一化相关（400，错误码互不相同）----


class ValidationError(LedgerError):
    code = "VALIDATION_ERROR"
    http_status = 400


class MissingFieldError(ValidationError):
    """模拟链输入缺少必填字段。"""

    code = "MISSING_FIELD"


class InvalidAmountError(ValidationError):
    """金额缺失、无法解析、非正数或超出精度。"""

    code = "INVALID_AMOUNT"


class InvalidEventError(ValidationError):
    """事件结构非法（如无法构造事件自然键）。"""

    code = "INVALID_EVENT"


class OutOfOrderError(LedgerError):
    """事件/扫描高度乱序（不静默丢弃，显式拒绝）。"""

    code = "OUT_OF_ORDER"
    http_status = 422


# ---- 幂等与冲突（409，重复 vs 语义冲突可区分）----


class ConflictError(LedgerError):
    http_status = 409


class DuplicateRequestError(ConflictError):
    """同一幂等键 + 同一请求体重复提交：安全拒绝，不重复扣款/入账。"""

    code = "DUPLICATE_REQUEST"


class IdempotencyConflictError(ConflictError):
    """同一幂等键对应不同请求体：语义冲突。"""

    code = "IDEMPOTENCY_CONFLICT"


class DuplicateEventError(ConflictError):
    """链上事件自然键 (tx_hash, log_index) 重复。"""

    code = "DUPLICATE_EVENT"


class InsufficientFundsError(ConflictError):
    """可用余额不足。"""

    code = "INSUFFICIENT_FUNDS"


# ---- 身份 / 权限 / 资源 ----


class UnauthenticatedError(LedgerError):
    code = "UNAUTHENTICATED"
    http_status = 401


class ForbiddenError(LedgerError):
    """已认证但角色不允许（不涉及资源存在性，可明确拒绝）。"""

    code = "FORBIDDEN"
    http_status = 403


class ResourceNotFoundError(LedgerError):
    """资源不存在，或资源属于其他主体（不暴露存在性，统一 404）。"""

    code = "RESOURCE_NOT_FOUND"
    http_status = 404


# ---- 背压 / 限流 ----


class RateLimitedError(LedgerError):
    code = "RATE_LIMITED"
    http_status = 429
