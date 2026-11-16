from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime

import httpx

from app.cache import TTLCache
from app.models import Deal, SearchParams


class ProviderError(Exception):
    pass


class Provider(ABC):
    name: str = "base"

    def __init__(self, client: httpx.AsyncClient, cache: TTLCache):
        self.client = client
        self.cache = cache

    @property
    @abstractmethod
    def enabled(self) -> bool: ...

    @abstractmethod
    async def search(self, params: SearchParams) -> tuple[list[Deal], list[str]]:
        """Devuelve (ofertas en bruto, avisos); el filtrado fino lo hace el servicio."""

    async def _get_json(self, url: str, params: dict) -> dict | list:
        key = self.cache.key(self.name, url, params)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        try:
            resp = await self.client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: error de red ({exc.__class__.__name__})") from exc
        if resp.status_code != 200:
            raise ProviderError(f"{self.name}: HTTP {resp.status_code} - {resp.text[:200]}")
        data = resp.json()
        self.cache.set(key, data)
        return data


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
