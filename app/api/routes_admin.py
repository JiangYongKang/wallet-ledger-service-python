"""管理/运维类路由：模拟链投喂、高度推进、重组、事件摄入与批量扫描（仅 admin）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import get_principal, services
from app.auth import Principal, require_admin

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class PushBlockBody(BaseModel):
    events: list[dict] = Field(default_factory=list)


class AdvanceBody(BaseModel):
    blocks: int = Field(default=1, ge=1, le=10_000)


class ReorgBody(BaseModel):
    height: int = Field(ge=1)


class IngestBody(BaseModel):
    # 允许任意条目；非对象/缺字段由归一化层分类进死信，而不是在 schema 层直接 422
    events: list = Field(min_length=1)


class ScanBody(BaseModel):
    max_events: int | None = Field(default=None, ge=1)


def _admin(principal: Principal) -> None:
    require_admin(principal)


@router.post("/chain/blocks")
def push_block(body: PushBlockBody, principal: Principal = Depends(get_principal)) -> dict:
    _admin(principal)
    height = services.chain.push_block(body.events)
    services.store.set_chain_height(height)
    return {"status": "ok", "chain_height": height, "events": len(body.events)}


@router.post("/chain/advance")
def advance(body: AdvanceBody, principal: Principal = Depends(get_principal)) -> dict:
    _admin(principal)
    height = services.chain.advance(body.blocks)
    services.store.set_chain_height(height)
    # 推进高度后立即尝试确认，保证多个入口资金结论一致
    result = services.ledger.confirm_events(height)
    return {"status": "ok", "chain_height": height, **result}


@router.post("/chain/reorg")
def reorg(body: ReorgBody, principal: Principal = Depends(get_principal)) -> dict:
    _admin(principal)
    # 最终性必须按重组"前"的链高计算埋深（事件处于风险时的确认数）
    pre_height = services.chain.height()
    removed_ids = services.chain.reorg_to(body.height)
    result = services.ledger.reorg(removed_ids, current_height=pre_height)
    new_height = body.height - 1
    # 游标重置：后续扫描从头重读，按 event_id 幂等去重，不产生重复入账
    services.store.set_scan_cursor(None)
    services.store.set_chain_height(new_height)
    return {"status": "ok", "chain_height": new_height, **result}


@router.post("/events/ingest")
def ingest_events(body: IngestBody, principal: Principal = Depends(get_principal)) -> dict:
    _admin(principal)
    return services.ingestion.ingest_raw(body.events)


@router.post("/scan")
def scan(body: ScanBody, principal: Principal = Depends(get_principal)) -> dict:
    _admin(principal)
    return services.ingestion.scan_once(body.max_events)
