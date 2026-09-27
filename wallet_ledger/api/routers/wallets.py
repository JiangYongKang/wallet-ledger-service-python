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
def get_balance(address: str, asset: str | None = None,
                principal=Depends(get_principal), service=Depends(get_service)):
    # 指定 asset：返回该资产的分口径余额；不指定：钱包级视图（顶层为默认资产，
    # assets 给出全部资产），旧客户端继续读顶层字段即可。
    if asset is not None:
        bal = service.get_balance(principal, address, asset)
        return {**bal.to_dict(), "asset": bal.asset}
    return service.get_wallet_balance(principal, address).to_dict()


@router.get("/{address}/entries")
def list_entries(address: str, asset: str | None = None,
                 principal=Depends(get_principal), service=Depends(get_service)):
    entries = service.list_entries(principal, address, asset=asset)
    return {
        "address": address,
        "entries": [
            {
                "seq": e.seq,
                "txn_type": e.txn_type.value,
                "amount": e.amount,
                "asset": e.asset,
                "ref_key": e.ref_key,
                "memo": e.memo,
            }
            for e in entries
        ],
    }
