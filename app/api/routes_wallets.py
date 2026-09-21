"""钱包与资金类路由：注册、余额、内部转账、提现（含幂等与属主鉴权）。"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from app.api.deps import get_principal, services
from app.auth import Principal, authorize_wallet
from app.ledger import LedgerService

router = APIRouter(prefix="/api/v1", tags=["wallets"])


class CreateWalletBody(BaseModel):
    wallet_id: str | None = None
    address: str = Field(min_length=1)


class TransferBody(BaseModel):
    to_wallet_id: str = Field(min_length=1)
    amount: int = Field(gt=0)


class WithdrawBody(BaseModel):
    amount: int = Field(gt=0)


def ledger() -> LedgerService:
    return services.ledger


@router.post("/wallets", status_code=201)
def create_wallet(
    body: CreateWalletBody,
    principal: Principal = Depends(get_principal),
) -> dict:
    wallet_id = body.wallet_id or f"w_{uuid.uuid4().hex[:12]}"
    w = ledger().create_wallet(wallet_id, principal.principal_id, body.address)
    return {
        "wallet_id": w.wallet_id,
        "owner": w.owner_principal,
        "address": w.address,
        "deposit_instructions": {
            "address": w.address,
            "confirmations_required": services.settings.confirmations_required,
        },
    }


@router.get("/wallets/{wallet_id}/balance")
def get_balance(
    wallet_id: str,
    principal: Principal = Depends(get_principal),
) -> dict:
    # 不存在与越权返回完全相同的 404
    authorize_wallet(principal, services.store.owner_of(wallet_id))
    return ledger().get_balance(wallet_id).as_dict()


@router.post("/wallets/{wallet_id}/transfers", status_code=200)
def transfer(
    wallet_id: str,
    body: TransferBody,
    principal: Principal = Depends(get_principal),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> dict:
    authorize_wallet(principal, services.store.owner_of(wallet_id))
    return ledger().transfer(
        wallet_id, body.to_wallet_id, body.amount, idempotency_key
    )


@router.post("/wallets/{wallet_id}/withdrawals", status_code=200)
def withdraw(
    wallet_id: str,
    body: WithdrawBody,
    principal: Principal = Depends(get_principal),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> dict:
    authorize_wallet(principal, services.store.owner_of(wallet_id))
    return ledger().withdraw(wallet_id, body.amount, idempotency_key)
