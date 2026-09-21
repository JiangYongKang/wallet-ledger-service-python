"""本地钱包账本服务入口（FastAPI）。"""
from __future__ import annotations

from fastapi import Depends, FastAPI

from app.api.deps import get_principal, services
from app.api.middleware import register_error_handlers
from app.api.routes_admin import router as admin_router
from app.api.routes_wallets import router as wallets_router
from app.auth import Principal, authorize_wallet, require_admin

app = FastAPI(title="wallet-ledger-service", version="0.1.0")
register_error_handlers(app)
app.include_router(wallets_router)
app.include_router(admin_router)


@app.get("/")
def read_root() -> dict:
    s = services.settings
    return {
        "service": "wallet-ledger-service",
        "status": "ok",
        "confirmations_required": s.confirmations_required,
        "finality_depth": s.finality_depth,
        "currency": s.currency,
    }


@app.get("/api/v1/wallets/{wallet_id}/audit")
def wallet_audit(
    wallet_id: str,
    principal: Principal = Depends(get_principal),
) -> dict:
    """返回钱包维度的事件流水与追加式账本条目（资金结论可解释）。"""
    authorize_wallet(principal, services.store.owner_of(wallet_id))
    wallet = services.store.get_wallet(wallet_id)
    events = [
        {
            "event_id": e.event_id,
            "kind": e.kind.value,
            "amount": e.amount,
            "block_height": e.block_height,
            "status": e.status.value,
            "reverted_by": e.reverted_by,
        }
        for e in services.store.list_events_by_address(wallet.address)
    ]
    entries = [
        {
            "entry_id": e.entry_id,
            "kind": e.kind,
            "amount": e.amount,
            "ref_event_id": e.ref_event_id,
            "ref_request_id": e.ref_request_id,
            "balance_after": e.balance_after,
        }
        for e in services.store.entries_for_wallet(wallet_id)
    ]
    balance = services.ledger.get_balance(wallet_id).as_dict()
    return {"wallet_id": wallet_id, "events": events, "entries": entries,
            "balance": balance}


@app.get("/api/v1/admin/dead-letters")
def dead_letters(principal: Principal = Depends(get_principal)) -> dict:
    require_admin(principal)
    return {"dead_letters": [
        {"raw": d.raw, "reason_code": d.reason_code, "reason": d.reason, "at": d.at}
        for d in services.store.dead_letters
    ]}
