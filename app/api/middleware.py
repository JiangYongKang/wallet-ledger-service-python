"""统一错误处理：领域错误 -> 可区分的 code/reason，不泄露底层异常与资源存在性。"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.errors import IdempotencyReplay, LedgerError

logger = logging.getLogger("wallet-ledger")


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(LedgerError)
    async def _handle_ledger_error(_: Request, exc: LedgerError) -> JSONResponse:
        body: dict = {
            "error": {
                "code": exc.code,
                "message": exc.message,
                "reason": exc.reason,
            }
        }
        if exc.details:
            body["error"]["details"] = exc.details
        status = 200 if isinstance(exc, IdempotencyReplay) else exc.http_status
        if isinstance(exc, IdempotencyReplay):
            # 幂等重放以 200 返回首次响应体，并带可区分标记
            return JSONResponse(status_code=200, content=exc.details.get(
                "response", body["error"]))
        return JSONResponse(status_code=status, content=body)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": {
                "code": "invalid_request",
                "message": "request validation failed",
                "reason": "schema",
                "details": {"fields": exc.errors()},
            }},
        )

    @app.exception_handler(Exception)
    async def _handle_unknown(_: Request, exc: Exception) -> JSONResponse:
        # 不透出底层异常细节，仅记录服务端日志
        logger.exception("unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error",
                               "message": "internal server error"}},
        )
