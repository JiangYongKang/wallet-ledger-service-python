"""领域错误类型：可区分、不向调用方透出底层异常。"""
from __future__ import annotations


class LedgerError(Exception):
    """所有账本领域错误的基类。"""

    code: str = "ledger_error"
    http_status: int = 400
    message: str = "ledger error"

    def __init__(
        self,
        message: str | None = None,
        *,
        reason: str | None = None,
        details: dict | None = None,
    ):
        super().__init__(message or self.message)
        if message:
            self.message = message
        self.reason = reason
        self.details = details or {}


class ValidationError(LedgerError):
    code = "invalid_request"
    http_status = 422


class Unauthorized(LedgerError):
    code = "unauthorized"
    http_status = 401


class IdempotencyReplay(LedgerError):
    code = "idempotent_replay"
    http_status = 200


class IdempotencyConflict(LedgerError):
    code = "idempotency_conflict"
    http_status = 409


class InsufficientFunds(LedgerError):
    code = "insufficient_funds"
    http_status = 409


class PermissionDenied(LedgerError):
    code = "permission_denied"
    http_status = 403


class NotFound(LedgerError):
    code = "not_found"
    http_status = 404


class RateLimited(LedgerError):
    code = "rate_limited"
    http_status = 429


class FinalizedReorg(LedgerError):
    code = "finalized_reorg"
    http_status = 409


class ConflictState(LedgerError):
    code = "conflict_state"
    http_status = 409
