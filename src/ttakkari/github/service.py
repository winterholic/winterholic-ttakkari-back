"""GitHub 탐색 서비스 묶음(프로세스당 하나). 라우트는 `Depends(get_service)`로 받고, 테스트는 이 의존성을 갈아끼운다."""
from __future__ import annotations

from dataclasses import dataclass

from ttakkari.github.accounts import AccountRegistry
from ttakkari.github.client import GithubClient
from ttakkari.github.logs import LogFetcher


@dataclass
class GithubService:
    registry: AccountRegistry
    client: GithubClient
    logs: LogFetcher


_instance: GithubService | None = None


def get_service() -> GithubService:
    global _instance
    if _instance is None:
        registry = AccountRegistry()
        client = GithubClient(registry)
        _instance = GithubService(registry=registry, client=client, logs=LogFetcher(client))
    return _instance
