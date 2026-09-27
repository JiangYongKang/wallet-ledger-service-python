"""钱包：创建、余额、流水。创建/访问仅限自身钱包范围。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ...normalizer import normalize_asset
from ..deps import get_principal, get_service
from ..schemas import WalletCreateRequest

router = APIRouter(prefix="/wallets", tags=["wallets"])


@router.post("")
def create_wallet(req: WalletCreateRequest, principal=Depends(get_principal),
                  service=Depends(get_service)):
    bal = service.create_wallet(principal, req.address)
    return {"status": "ok", "balance": bal.to_dict()}


@router.get("/{address}/balance")
def get_balance(address: str,
                asset: str | None = Query(default=None),
                principal=Depends(get_principal),
                service=Depends(get_service)):
    # 不带 asset：默认资产，响应形状与单资产时代完全一致。
    asset_id = normalize_asset(asset)
    bal = service.get_balance(principal, address, asset_id)
    return bal.to_dict(asset_id)


@router.get("/{address}/balances")
def get_balances(address: str, principal=Depends(get_principal),
                 service=Depends(get_service)):
    """一次看清该钱包下每个资产各自的分口径余额。"""
    bal = service.get_balance(principal, address)
    return {"address": address, "assets": bal.assets_to_dict()}


@router.get("/{address}/entries")
def list_entries(address: str,
                 asset: str | None = Query(default=None),
                 principal=Depends(get_principal),
                 service=Depends(get_service)):
    asset_id = normalize_asset(asset) if asset is not None else None
    entries = service.list_entries(principal, address, asset_id)
    return {
        "address": address,
        "entries": [
            {
                "seq": e.seq,
                "txn_type": e.txn_type.value,
                "asset": e.asset,
                "amount": e.amount,
                "ref_key": e.ref_key,
                "memo": e.memo,
            }
            for e in entries
        ],
    }
