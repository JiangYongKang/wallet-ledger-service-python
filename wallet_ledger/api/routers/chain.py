"""模拟链入口：事件投喂、批量确认扫描/重组（仅 INDEXER 角色）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ...normalizer import normalize_event
from ..deps import get_principal, get_scanner, get_service
from ..schemas import ChainEventIn, ScanRequest

router = APIRouter(prefix="/chain", tags=["chain"])


@router.post("/events")
def ingest_event(req: ChainEventIn, principal=Depends(get_principal),
                 service=Depends(get_service)):
    # 宽松入模后归一化：缺字段/非法金额/非法类型在此分类报错，不透底层异常。
    event = normalize_event(req.model_dump())
    record = service.ingest_event(principal, event)
    return {
        "status": "ok",
        "key": list(record.event.natural_key),
        "event_status": record.status.value,
    }


@router.post("/scan")
def scan(req: ScanRequest,
         wait: float = Query(default=0.0, ge=0.0, le=5.0),
         principal=Depends(get_principal), scanner=Depends(get_scanner)):
    events = [(item.tx_hash, item.log_index, item.confirmations)
              for item in req.events]
    return scanner.scan_batch(
        principal,
        tip_height=req.tip_height,
        events=events,
        reorg_keys=[tuple(k) for k in (req.reorg_keys or [])],
        acquire_timeout=wait,
    )
