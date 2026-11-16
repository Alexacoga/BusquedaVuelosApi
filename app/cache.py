"""Caché en memoria con caducidad, para no gastar cuota de las APIs externas."""
from __future__ import annotations

import json
import time
from typing import Any


class TTLCache:
    def __init__(self, ttl_seconds: int, max_items: int = 2000):
        self.ttl = ttl_seconds
        self.max_items = max_items
        self._data: dict[str, tuple[float, Any]] = {}

    @staticmethod
    def key(*parts: Any) -> str:
        return json.dumps(parts, sort_keys=True, default=str)

    def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if not item:
            return None
        expires, value = item
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        if self.ttl <= 0:
            return
        if len(self._data) >= self.max_items:
            # descarta la entrada que caduca antes
            oldest = min(self._data, key=lambda k: self._data[k][0])
            self._data.pop(oldest, None)
        self._data[key] = (time.monotonic() + self.ttl, value)
