"""用户资金操作：转账、提现（均需 idempotency_key，重复提交安全回放）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from ..deps import get_principal, get_service
from ..schemas import TransferRequest, WithdrawRequest

router = APIRouter(prefix="/ledger", tags=["ledger"])


@router.post("/transfer")
def transfer(req: TransferRequest, response: Response,
             principal=Depends(get_principal), service=Depends(get_service)):
    body = service.transfer(
        principal, req.from_address, req.to_address,
        req.amount, req.idempotency_key,
    )
    # 重复但等价的请求回放原结果，通过 replay 与 208 状态可与首次区分。
    if body.get("replay"):
        response.status_code = 208
    return body


@router.post("/withdraw")
def withdraw(req: WithdrawRequest, response: Response,
             principal=Depends(get_principal), service=Depends(get_service)):
    body = service.withdraw(principal, req.address, req.amount, req.idempotency_key)
    if body.get("replay"):
        response.status_code = 208
    return body
