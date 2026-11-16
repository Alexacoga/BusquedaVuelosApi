"""Configuración leída de variables de entorno (o de un fichero .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Carga un .env sencillo (CLAVE=valor) sin dependencias externas."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    travelpayouts_token: str | None
    travelpayouts_market: str
    travelpayouts_marker: str | None
    serpapi_api_key: str | None
    default_currency: str
    language: str
    country: str
    cache_ttl_seconds: int
    http_timeout_seconds: float


@lru_cache
def get_settings() -> Settings:
    _load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    env = os.environ.get
    return Settings(
        travelpayouts_token=env("TRAVELPAYOUTS_TOKEN") or None,
        travelpayouts_market=env("TRAVELPAYOUTS_MARKET", "es"),
        travelpayouts_marker=env("TRAVELPAYOUTS_MARKER") or None,
        serpapi_api_key=env("SERPAPI_API_KEY") or None,
        default_currency=env("DEFAULT_CURRENCY", "EUR").upper(),
        language=env("LANGUAGE", "es"),
        country=env("COUNTRY", "es"),
        cache_ttl_seconds=int(env("CACHE_TTL_SECONDS", "21600")),  # 6 h
        http_timeout_seconds=float(env("HTTP_TIMEOUT_SECONDS", "30")),
    )
