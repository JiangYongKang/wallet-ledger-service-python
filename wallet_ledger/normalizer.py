"""模拟链输入归一化：缺字段/乱序/重复/非法金额不静默丢弃，按可区分错误拒绝。

金额接受十进制正整数字符串或整数（最小货币单位）；
禁止小数/浮点/非数字/零/负数，避免浮点误差与非法金额混入。
"""

from __future__ import annotations

import re
from typing import Any

from .domain import DEFAULT_ASSET, ChainEvent, ChainEventType, RawChainEvent
from .errors import (
    InvalidAmountError,
    InvalidAssetError,
    InvalidEventError,
    MissingFieldError,
)

_REQUIRED_FIELDS = (
    "tx_hash",
    "log_index",
    "type",
    "address",
    "amount",
    "block_height",
)


def normalize_event(raw: RawChainEvent | dict[str, Any]) -> ChainEvent:
    """把原始事件转成 ChainEvent；非法输入抛出可区分的 ValidationError 子类。"""
    data = vars(raw) if isinstance(raw, RawChainEvent) else dict(raw)

    # 1) 缺字段（None 或空串都视为缺失），逐字段报出第一个缺失项以便定位。
    for field_name in _REQUIRED_FIELDS:
        value = data.get(field_name)
        if value is None or (isinstance(value, str) and value.strip() == ""):
            raise MissingFieldError(
                f"missing required field: {field_name}",
                details={"field": field_name},
            )

    tx_hash = data["tx_hash"]
    if not isinstance(tx_hash, str) or not tx_hash.strip():
        raise InvalidEventError("tx_hash must be non-empty string",
                                details={"field": "tx_hash"})

    log_index = _parse_nonneg_int(data["log_index"], "log_index")
    block_height = _parse_nonneg_int(data["block_height"], "block_height")

    raw_type = data["type"]
    try:
        event_type = ChainEventType(str(raw_type))
    except ValueError:
        raise InvalidEventError(
            f"unknown event type: {raw_type!r}",
            details={"field": "type", "value": str(raw_type)},
        ) from None

    address = data["address"]
    if not isinstance(address, str) or not address.strip():
        raise InvalidEventError("address must be non-empty string",
                                details={"field": "address"})

    amount = parse_amount(data["amount"])
    asset = normalize_asset(data.get("asset"))

    return ChainEvent(
        tx_hash=tx_hash.strip(),
        log_index=log_index,
        type=event_type,
        address=address.strip(),
        amount=amount,
        block_height=block_height,
        asset=asset,
    )


# 资产符号：字母数字开头，可含 . _ -，最长 32；统一大写，避免大小写串账。
_ASSET_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,31}$")


def normalize_asset(value: Any) -> str:
    """资产符号归一化：缺省（None）-> DEFAULT_ASSET；非法 -> InvalidAssetError。

    字符串去空白并统一大写，保证 ``btc``/``BTC`` 落到同一资产桶。
    """
    if value is None:
        return DEFAULT_ASSET
    if not isinstance(value, str):
        raise InvalidAssetError("asset must be a string",
                                details={"value": str(value)})
    symbol = value.strip().upper()
    if not symbol or not _ASSET_RE.match(symbol):
        raise InvalidAssetError(
            "asset must be 1-32 chars of [A-Z0-9._-], starting alphanumeric",
            details={"value": value},
        )
    return symbol


def parse_amount(value: Any) -> int:
    """正整数最小单位金额。float/小数/非数字/<=0 一律 InvalidAmountError。"""
    if isinstance(value, bool):  # bool 是 int 子类，明确拒绝
        raise InvalidAmountError("amount must be a positive integer",
                                 details={"value": str(value)})
    if isinstance(value, int):
        amount = value
    elif isinstance(value, str):
        text = value.strip()
        if not text or not text.isdigit():
            # 含小数点、负号、非数字都到这里，错误码统一但原因可区分。
            raise InvalidAmountError(
                "amount must be a positive integer string (minor unit)",
                details={"value": value},
            )
        amount = int(text)
    else:
        raise InvalidAmountError("unsupported amount type",
                                 details={"value": str(value)})
    if amount <= 0:
        raise InvalidAmountError("amount must be > 0", details={"value": str(value)})
    return amount


def _parse_nonneg_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        # 字符串数字也接受（模拟链 JSON 可能给字符串）
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        raise InvalidEventError(
            f"{field_name} must be non-negative integer",
            details={"field": field_name, "value": str(value)},
        )
    return value
