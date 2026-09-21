"""共享服务实例（单进程内存态）与鉴权依赖。"""
from __future__ import annotations

from fastapi import Header

from app.auth import Principal, authenticate
from app.chain import MockChain
from app.config import Settings, get_settings
from app.ingestion import IngestionService
from app.ledger import LedgerService
from app.store import Store


class Services:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.store = Store()
        self.chain = MockChain()
        self.ledger = LedgerService(self.store, self.settings)
        self.ingestion = IngestionService(
            self.store, self.chain, self.ledger, self.settings
        )

    def reset(self) -> None:
        self.store.reset()
        self.chain.reset()


services = Services()


def get_principal(
    x_principal_id: str | None = Header(default=None),
    x_principal_role: str | None = Header(default=None),
) -> Principal:
    return authenticate(x_principal_id, x_principal_role)
