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
        now = time.monotonic()
        if len(self._data) >= self.max_items:
            # primero limpia lo caducado; si sigue lleno, descarta la entrada que caduca antes
            for k in [k for k, (exp, _) in self._data.items() if exp < now]:
                del self._data[k]
            if len(self._data) >= self.max_items:
                oldest = min(self._data, key=lambda k: self._data[k][0])
                del self._data[oldest]
        self._data[key] = (now + self.ttl, value)
