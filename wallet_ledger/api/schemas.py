"""HTTP 请求/响应模型（pydantic）。

为保证错误可区分，归一化层自行校验，不依赖 pydantic 自动 422 暴露字段细节；
链事件输入以宽松模型接收，再由 normalizer 归一化。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class WalletCreateRequest(BaseModel):
    address: str = Field(min_length=1)


class TransferRequest(BaseModel):
    from_address: str
    to_address: str
    amount: int  # 最小货币单位正整数
    idempotency_key: str = Field(min_length=1)


class WithdrawRequest(BaseModel):
    address: str
    amount: int
    idempotency_key: str = Field(min_length=1)


class ChainEventIn(BaseModel):
    # 宽松接收：缺字段/非法金额交给 normalizer 归一化并分类报错
    model_config = {"extra": "allow"}
    tx_hash: Any | None = None
    log_index: Any | None = None
    type: Any | None = None
    address: Any | None = None
    amount: Any | None = None
    block_height: Any | None = None


class ScanItem(BaseModel):
    tx_hash: str
    log_index: int
    confirmations: int


class ScanRequest(BaseModel):
    tip_height: int
    events: list[ScanItem]
    reorg_keys: list[tuple[str, int]] | None = None
