"""Proveedor Travelpayouts / Aviasales Data API (gratuito).

Devuelve los precios más baratos encontrados por usuarios de Aviasales en los
últimos días (caché de 2-7 días), así que son ofertas orientativas: hay que
confirmar el precio en el enlace. Admite búsquedas "a cualquier destino"
omitiendo `destination` y "desde cualquier origen" omitiendo `origin`
(en ambos casos devuelve como mucho la opción más barata de cada día).
Docs: https://support.travelpayouts.com/hc/en-us/articles/203956163
"""
from __future__ import annotations

import asyncio
import itertools
import math
from datetime import timedelta

from app.config import Settings
from app.models import DateWindow, Deal, DealKind, Leg, SearchParams
from app.providers.base import Provider, ProviderError, parse_date, parse_datetime

BASE = "https://api.travelpayouts.com"
PRICES_URL = f"{BASE}/aviasales/v3/prices_for_dates"
MAX_CALLS = 12        # tope de combinaciones mes-ida x mes-vuelta por búsqueda
MAX_CONCURRENCY = 6   # llamadas simultáneas máximas a Travelpayouts


def _date_params(window: DateWindow | None) -> list[str | None]:
    if window is None:
        return [None]
    if window.is_single_day:
        return [window.start.isoformat()]
    return window.months()


class TravelpayoutsProvider(Provider):
    name = "travelpayouts"
    supports_one_way_legs = True

    def __init__(self, client, cache, settings: Settings):
        super().__init__(client, cache)
        self.settings = settings
        self._city_names: dict[str, tuple[str, str | None]] | None = None
        self._coords: dict[str, tuple[float, float]] = {}
        self._city_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(MAX_CONCURRENCY)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.travelpayouts_token)

    # --- Consultas ----------------------------------------------------------
    def _base_query(self, currency: str, one_way: bool, direct: bool) -> dict:
        return {
            "currency": currency.lower(),
            # sin departure_at, Travelpayouts devuelve vuelos de los próximos meses (red de rutas)
            "market": self.settings.travelpayouts_market,
            "one_way": "true" if one_way else "false",
            "direct": "true" if direct else "false",
            "sorting": "price",
            "unique": "false",
            "limit": 1000,
        }

    async def _prices(self, q: dict) -> dict:
        # El token va en cabecera, no en la URL: así no aparece en logs ni en la clave de caché.
        headers = {"X-Access-Token": self.settings.travelpayouts_token}
        async with self._sem:
            payload = await self._get_json(PRICES_URL, q, headers)
        if not isinstance(payload, dict) or payload.get("success") is False:
            err = payload.get("error") if isinstance(payload, dict) else None
            raise ProviderError(f"{self.name}: {err or 'respuesta no válida'}")
        return payload

    async def search(self, params: SearchParams) -> tuple[list[Deal], list[str]]:
        departs = _date_params(params.depart_search_window)
        returns = _date_params(params.return_search_window) if params.is_round_trip else [None]
        combos = list(itertools.product(departs, returns))
        if len(combos) > MAX_CALLS:
            raise ProviderError(
                f"{self.name}: el rango de fechas genera {len(combos)} consultas (máx. {MAX_CALLS}); acórtalo"
            )

        base = self._base_query(params.currency, not params.is_round_trip, params.direct_only)
        base["origin"] = params.origin
        if params.destination:
            base["destination"] = params.destination

        calls = []
        for dep, ret in combos:
            q = dict(base)
            if dep:
                q["departure_at"] = dep
            if ret:
                q["return_at"] = ret
            calls.append(self._prices(q))

        payloads, names = await asyncio.gather(asyncio.gather(*calls), self._load_city_names())
        deals: list[Deal] = []
        for payload in payloads:
            currency = (payload.get("currency") or params.currency).upper()
            for item in payload.get("data") or []:
                deal = self._to_deal(item, currency, params, names)
                if deal:
                    deals.append(deal)
        return deals, []

    async def one_way_legs(self, origin: str | None, destination: str | None, window: DateWindow | None,
                           currency: str, direct_only: bool = False) -> list[Leg]:
        """Billetes de solo ida. `origin` o `destination` pueden ser None (= cualquiera)."""
        if not origin and not destination:
            raise ValueError("hace falta origen o destino")
        base = self._base_query(currency, True, direct_only)
        if origin:
            base["origin"] = origin
        if destination:
            base["destination"] = destination
        calls = []
        for dep in _date_params(window):
            q = dict(base)
            if dep:
                q["departure_at"] = dep
            calls.append(self._prices(q))
        payloads, names = await asyncio.gather(asyncio.gather(*calls), self._load_city_names())
        legs: list[Leg] = []
        for payload in payloads:
            cur = (payload.get("currency") or currency).upper()
            for item in payload.get("data") or []:
                leg = self._to_leg(item, cur, names)
                if leg and (window is None or window.contains(leg.departure_date)):
                    legs.append(leg)
        return legs

    # --- Conversión ---------------------------------------------------------
    def _link(self, path: str | None) -> str | None:
        if not path:
            return None
        link = "https://www.aviasales.com" + path
        if self.settings.travelpayouts_marker:
            link += ("&" if "?" in link else "?") + f"marker={self.settings.travelpayouts_marker}"
        return link

    @staticmethod
    def _flight_number(item: dict) -> str | None:
        airline, number = item.get("airline"), item.get("flight_number")
        return f"{airline}{number}" if airline and number else (str(number) if number else None)

    def _to_leg(self, item: dict, currency: str, names) -> Leg | None:
        dep_at = parse_datetime(item.get("departure_at"))
        if dep_at is None or item.get("price") is None or not item.get("origin") or not item.get("destination"):
            return None
        duration = item.get("duration_to") or item.get("duration")
        dest_name, dest_country = names.get(item["destination"], (None, None))
        return Leg(
            source=self.name,
            origin=item["origin"],
            origin_airport=item.get("origin_airport"),
            origin_country=names.get(item["origin"], (None, None))[1],
            destination=item["destination"],
            destination_airport=item.get("destination_airport"),
            destination_name=dest_name,
            destination_country=dest_country,
            departure_at=dep_at,
            arrival_at=dep_at + timedelta(minutes=duration) if duration else None,
            departure_date=dep_at.date(),
            price=float(item["price"]),
            currency=currency,
            airline=item.get("airline"),
            flight_number=self._flight_number(item),
            stops=item.get("transfers"),
            duration_minutes=duration,
            link=self._link(item.get("link")),
        )

    def _to_deal(self, item: dict, currency: str, params: SearchParams, names) -> Deal | None:
        dep = parse_date(item.get("departure_at"))
        if dep is None or item.get("price") is None:
            return None
        ret = parse_date(item.get("return_at")) if params.is_round_trip else None
        dest = item.get("destination") or params.destination or "?"
        name, country = names.get(dest, (None, None))
        return Deal(
            source=self.name,
            kind=DealKind.round_trip if ret else DealKind.one_way,
            origin=item.get("origin") or params.origin,
            destination=dest,
            destination_name=name,
            destination_country=country,
            departure_date=dep,
            return_date=ret,
            nights=(ret - dep).days if ret else None,
            price=float(item["price"]),
            currency=currency,
            airline=item.get("airline"),
            flight_number=self._flight_number(item),
            stops=item.get("transfers"),
            return_stops=item.get("return_transfers") if ret else None,
            duration_minutes=item.get("duration_to") or item.get("duration"),
            link=self._link(item.get("link")),
        )

    async def _load_city_names(self) -> dict[str, tuple[str, str | None]]:
        """Nombres y país de cada ciudad por código IATA (fichero público de Travelpayouts)."""
        if self._city_names is not None:
            return self._city_names
        async with self._city_lock:
            if self._city_names is None:
                try:
                    lang = self.settings.language
                    data = await self._get_json(f"{BASE}/data/{lang}/cities.json", {})
                    names, coords = {}, {}
                    for c in data:
                        if not isinstance(c, dict) or not c.get("code"):
                            continue
                        names[c["code"]] = (c.get("name") or c.get("name_translations", {}).get("en"),
                                            c.get("country_code"))
                        pos = c.get("coordinates") or {}
                        if pos.get("lat") is not None and pos.get("lon") is not None:
                            coords[c["code"]] = (float(pos["lat"]), float(pos["lon"]))
                    self._coords = coords
                    self._city_names = names
                except Exception:  # los nombres son opcionales; se reintenta en la próxima búsqueda
                    return {}
        return self._city_names

    async def distance_km(self, a: str, b: str) -> float | None:
        """Distancia en línea recta entre dos ciudades (None si no hay coordenadas)."""
        await self._load_city_names()
        pa, pb = self._coords.get(a), self._coords.get(b)
        if not pa or not pb:
            return None
        lat1, lon1, lat2, lon2 = map(math.radians, (*pa, *pb))
        h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
        return 2 * 6371 * math.asin(math.sqrt(h))
