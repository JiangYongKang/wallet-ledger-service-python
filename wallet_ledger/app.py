"""FastAPI 应用装配：统一异常映射、路由注册。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api.routers import chain, ledger, wallets
from .errors import LedgerError


def create_app() -> FastAPI:
    app = FastAPI(title="wallet-ledger-service", version="0.1.0")

    @app.exception_handler(LedgerError)
    async def _ledger_error_handler(request: Request, exc: LedgerError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    app.include_router(wallets.router)
    app.include_router(ledger.router)
    app.include_router(chain.router)
    return app


app = create_app()
