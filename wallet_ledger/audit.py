"""事件流水与判定依据日志（单测可捕获输出，打印资金结论依据）。"""

from __future__ import annotations

import logging

_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_configured = False


def get_logger(name: str = "wallet_ledger") -> logging.Logger:
    global _configured
    logger = logging.getLogger(name)
    if not _configured:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        _configured = True
    return logger
