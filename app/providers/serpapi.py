"""Proveedor SerpApi - Google Travel Explore (datos de Google Flights).

Plan gratuito: 250 búsquedas/mes, por eso cada consulta se cachea y solo se
hace una llamada por mes del rango de ida. Explore solo cubre los próximos
6 meses. Docs: https://serpapi.com/google-travel-explore-api
"""
from __future__ import annotations

import asyncio
from datetime import date

from app.config import Settings
from app.models import Deal, SearchParams
from app.providers.base import Provider, ProviderError, parse_date

URL = "https://serpapi.com/search.json"
NO_RESULTS_HINTS = ("hasn't returned any results", "no results")


def _months_ahead(month_str: str, today: date) -> int:
    y, m = map(int, month_str.split("-"))
    return (y - today.year) * 12 + (m - today.month)


def _travel_duration(params: SearchParams) -> int:
    """1 = fin de semana, 2 = una semana, 3 = dos semanas."""
    nights = params.max_nights or params.min_nights
    dw, rw = params.depart_window, params.return_window
    if nights is None and dw and rw:
        mid_dep = dw.start + (dw.end - dw.start) / 2
        mid_ret = rw.start + (rw.end - rw.start) / 2
        nights = (mid_ret - mid_dep).days
    if nights is None:
        return 2
    return 1 if nights <= 3 else 2 if nights <= 9 else 3


class SerpApiProvider(Provider):
    name = "google_flights"

    def __init__(self, client, cache, settings: Settings):
        super().__init__(client, cache)
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.serpapi_api_key)

    def build_queries(self, params: SearchParams) -> tuple[list[dict], list[str]]:
        warnings: list[str] = []
        base = {
            "engine": "google_travel_explore",
            "departure_id": params.origin,
            "type": 1 if params.is_round_trip else 2,
            "currency": params.currency,
            "hl": self.settings.language,
            "gl": self.settings.country,
            "travel_mode": 1,  # solo vuelos
            "api_key": self.settings.serpapi_api_key,
        }
        if params.destination:
            base["arrival_id"] = params.destination
        if params.direct_only:
            base["stops"] = 1
        if params.max_price is not None:
            base["max_price"] = int(params.max_price)

        dw, rw = params.depart_window, params.return_window
        exact = dw and dw.is_single_day and (not params.is_round_trip or (rw and rw.is_single_day))
        if exact:
            q = dict(base, outbound_date=dw.start.isoformat())
            if params.is_round_trip:
                q["return_date"] = rw.start.isoformat()
            return [q], warnings

        if params.is_round_trip:
            base["travel_duration"] = _travel_duration(params)
        if dw is None:
            return [dict(base, month=0)], warnings  # próximos 6 meses

        today = date.today()
        queries = []
        for m in dw.months():
            ahead = _months_ahead(m, today)
            if ahead > 6:
                warnings.append(f"{self.name}: {m} queda fuera de los 6 meses que cubre Google Explore")
                continue
            queries.append(dict(base, month=int(m[-2:])))
        return queries, warnings

    async def search(self, params: SearchParams) -> tuple[list[Deal], list[str]]:
        queries, warnings = self.build_queries(params)
        payloads = await asyncio.gather(*(self._get_json(URL, q) for q in queries))
        deals: list[Deal] = []
        for payload in payloads:
            err = payload.get("error") if isinstance(payload, dict) else None
            if err:
                if any(h in err.lower() for h in NO_RESULTS_HINTS):
                    continue
                raise ProviderError(f"{self.name}: {err}")
            for item in payload.get("destinations") or []:
                deal = self._to_deal(item, params)
                if deal:
                    deals.append(deal)
        return deals, warnings

    def _to_deal(self, item: dict, params: SearchParams) -> Deal | None:
        dep = parse_date(item.get("start_date"))
        price = item.get("flight_price")
        if dep is None or price is None:
            return None
        ret = parse_date(item.get("end_date")) if params.is_round_trip else None
        airport = item.get("destination_airport") or {}
        airline_code = item.get("airline_code")
        return Deal(
            source=self.name,
            origin=params.origin,
            destination=airport.get("code") or params.destination or "?",
            destination_name=item.get("name"),
            destination_country=item.get("country"),
            departure_date=dep,
            return_date=ret,
            nights=(ret - dep).days if ret else None,
            price=float(price),
            currency=params.currency,
            airline=item.get("airline") or (airline_code if airline_code != "multi" else None),
            stops=item.get("number_of_stops"),
            duration_minutes=item.get("flight_duration"),
            link=item.get("link"),
        )
