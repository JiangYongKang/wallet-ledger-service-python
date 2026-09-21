"""钱包：创建、余额、流水。创建/访问仅限自身钱包范围。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import get_principal, get_service
from ..schemas import WalletCreateRequest

router = APIRouter(prefix="/wallets", tags=["wallets"])


@router.post("")
def create_wallet(req: WalletCreateRequest, principal=Depends(get_principal),
                  service=Depends(get_service)):
    bal = service.create_wallet(principal, req.address)
    return {"status": "ok", "balance": bal.to_dict()}


@router.get("/{address}/balance")
def get_balance(address: str, principal=Depends(get_principal),
                service=Depends(get_service)):
    bal = service.get_balance(principal, address)
    return bal.to_dict()


@router.get("/{address}/entries")
def list_entries(address: str, principal=Depends(get_principal),
                 service=Depends(get_service)):
    entries = service.list_entries(principal, address)
    return {
        "address": address,
        "entries": [
            {
                "seq": e.seq,
                "txn_type": e.txn_type.value,
                "amount": e.amount,
                "ref_key": e.ref_key,
                "memo": e.memo,
            }
            for e in entries
        ],
    }
