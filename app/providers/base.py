from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import date, datetime

import httpx

from app.cache import TTLCache
from app.models import Deal, SearchParams

RETRIES = 1                      # un reintento ante fallos transitorios
RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_DELAY_SECONDS = 1.0


class ProviderError(Exception):
    pass


class ProviderQueryError(ProviderError):
    """El proveedor respondió, pero rechaza la consulta (p. ej. código IATA que no conoce).

    No es un fallo del servicio: se devuelve como aviso con resultados vacíos.
    """


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

    async def _get_json(self, url: str, params: dict, headers: dict | None = None) -> dict | list:
        """GET con caché. Las cabeceras (credenciales) no forman parte de la clave de caché."""
        key = self.cache.key(self.name, url, params)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        resp = None
        for attempt in range(1 + RETRIES):
            try:
                resp = await self.client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                if attempt < RETRIES:
                    continue
                raise ProviderError(f"{self.name}: error de red ({exc.__class__.__name__})") from exc
            if resp.status_code in RETRY_STATUS and attempt < RETRIES:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                continue
            break
        if resp.status_code == 400:
            raise ProviderQueryError(f"{self.name}: consulta rechazada - {_error_text(resp)}")
        if resp.status_code != 200:
            raise ProviderError(f"{self.name}: HTTP {resp.status_code} - {_error_text(resp)}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(f"{self.name}: respuesta no es JSON") from exc
        self.cache.set(key, data)
        return data


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("error"):
            return str(body["error"])[:200]
    except ValueError:
        pass
    return resp.text[:200]


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
