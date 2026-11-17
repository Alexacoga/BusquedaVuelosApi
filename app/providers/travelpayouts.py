"""Proveedor Travelpayouts / Aviasales Data API (gratuito).

Devuelve los precios más baratos encontrados por usuarios de Aviasales en los
últimos días (caché de 2-7 días), así que son ofertas orientativas: hay que
confirmar el precio en el enlace. Admite búsquedas "a cualquier destino"
omitiendo `destination`.
Docs: https://support.travelpayouts.com/hc/en-us/articles/203956163
"""
from __future__ import annotations

import asyncio
import itertools

from app.config import Settings
from app.models import DateWindow, Deal, SearchParams
from app.providers.base import Provider, ProviderError, parse_date

BASE = "https://api.travelpayouts.com"
MAX_CALLS = 12  # tope de combinaciones mes-ida x mes-vuelta por búsqueda


def _date_params(window: DateWindow | None) -> list[str | None]:
    if window is None:
        return [None]
    if window.is_single_day:
        return [window.start.isoformat()]
    return window.months()


class TravelpayoutsProvider(Provider):
    name = "travelpayouts"

    def __init__(self, client, cache, settings: Settings):
        super().__init__(client, cache)
        self.settings = settings
        self._city_names: dict[str, tuple[str, str | None]] | None = None
        self._city_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.travelpayouts_token)

    async def search(self, params: SearchParams) -> tuple[list[Deal], list[str]]:
        departs = _date_params(params.depart_window)
        returns = _date_params(params.return_window) if params.is_round_trip else [None]
        combos = list(itertools.product(departs, returns))
        if len(combos) > MAX_CALLS:
            raise ProviderError(
                f"{self.name}: el rango de fechas genera {len(combos)} consultas (máx. {MAX_CALLS}); acórtalo"
            )

        base = {
            "origin": params.origin,
            "currency": params.currency.lower(),
            "market": self.settings.travelpayouts_market,
            "one_way": "false" if params.is_round_trip else "true",
            "direct": "true" if params.direct_only else "false",
            "sorting": "price",
            "unique": "false",
            "limit": 1000,
        }
        # El token va en cabecera, no en la URL: así no aparece en logs ni en la clave de caché.
        headers = {"X-Access-Token": self.settings.travelpayouts_token}
        if params.destination:
            base["destination"] = params.destination

        calls = []
        for dep, ret in combos:
            q = dict(base)
            if dep:
                q["departure_at"] = dep
            if ret:
                q["return_at"] = ret
            calls.append(self._get_json(f"{BASE}/aviasales/v3/prices_for_dates", q, headers))

        payloads, names = await asyncio.gather(asyncio.gather(*calls), self._load_city_names())
        deals: list[Deal] = []
        for payload in payloads:
            if not isinstance(payload, dict) or payload.get("success") is False:
                err = payload.get("error") if isinstance(payload, dict) else None
                raise ProviderError(f"{self.name}: {err or 'respuesta no válida'}")
            currency = (payload.get("currency") or params.currency).upper()
            for item in payload.get("data") or []:
                deal = self._to_deal(item, currency, params, names)
                if deal:
                    deals.append(deal)
        return deals, []

    def _to_deal(self, item: dict, currency: str, params: SearchParams, names) -> Deal | None:
        dep = parse_date(item.get("departure_at"))
        if dep is None or item.get("price") is None:
            return None
        ret = parse_date(item.get("return_at")) if params.is_round_trip else None
        dest = item.get("destination") or params.destination or "?"
        name, country = names.get(dest, (None, None))
        link = item.get("link")
        if link:
            link = "https://www.aviasales.com" + link
            if self.settings.travelpayouts_marker:
                link += ("&" if "?" in link else "?") + f"marker={self.settings.travelpayouts_marker}"
        airline = item.get("airline")
        number = item.get("flight_number")
        return Deal(
            source=self.name,
            origin=item.get("origin") or params.origin,
            destination=dest,
            destination_name=name,
            destination_country=country,
            departure_date=dep,
            return_date=ret,
            nights=(ret - dep).days if ret else None,
            price=float(item["price"]),
            currency=currency,
            airline=airline,
            flight_number=f"{airline}{number}" if airline and number else (str(number) if number else None),
            stops=item.get("transfers"),
            return_stops=item.get("return_transfers") if ret else None,
            duration_minutes=item.get("duration_to") or item.get("duration"),
            link=link,
        )

    async def _load_city_names(self) -> dict[str, tuple[str, str | None]]:
        """Nombres de ciudad por código IATA (fichero público de Travelpayouts)."""
        if self._city_names is not None:
            return self._city_names
        async with self._city_lock:
            if self._city_names is None:
                try:
                    lang = self.settings.language
                    data = await self._get_json(f"{BASE}/data/{lang}/cities.json", {})
                    self._city_names = {
                        c["code"]: (c.get("name") or c.get("name_translations", {}).get("en"), c.get("country_code"))
                        for c in data if isinstance(c, dict) and c.get("code")
                    }
                except Exception:  # los nombres son opcionales; se reintenta en la próxima búsqueda
                    return {}
        return self._city_names
