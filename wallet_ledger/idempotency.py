"""幂等支持：请求指纹 + 按主体隔离的幂等键命名空间。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def request_fingerprint(payload: Mapping[str, Any]) -> str:
    """对请求体做规范化 SHA-256；键排序、无空白分隔，保证同一体同一指纹。"""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def namespaced_key(principal_id: str, key: str) -> str:
    """同一 key 字符串在不同主体下互不影响。"""
    return f"{principal_id}::{key}"
