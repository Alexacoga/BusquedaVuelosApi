"""Orquesta los proveedores, filtra, deduplica y ordena las ofertas."""
from __future__ import annotations

import asyncio

from app.models import Deal, SearchParams, SearchResponse
from app.providers.base import Provider, ProviderError, ProviderQueryError


class NoProvidersError(Exception):
    pass


def matches(deal: Deal, p: SearchParams) -> bool:
    dw, rw = p.depart_window, p.return_window
    if dw and not dw.contains(deal.departure_date):
        return False
    if p.is_round_trip:
        if deal.return_date is None:
            return False
        if rw and not rw.contains(deal.return_date):
            return False
        if deal.return_date < deal.departure_date:
            return False
        if p.min_nights is not None and (deal.nights or 0) < p.min_nights:
            return False
        if p.max_nights is not None and (deal.nights or 0) > p.max_nights:
            return False
    if p.min_price is not None and deal.price < p.min_price:
        return False
    if p.max_price is not None and deal.price > p.max_price:
        return False
    if p.direct_only and (deal.stops or 0) > 0:
        return False
    return True


def dedupe(deals: list[Deal]) -> list[Deal]:
    best: dict[tuple, Deal] = {}
    for d in deals:
        key = (d.destination, d.departure_date, d.return_date, d.airline)
        if key not in best or d.price < best[key].price:
            best[key] = d
    return list(best.values())


def cheapest_per_destination(deals: list[Deal]) -> list[Deal]:
    best: dict[str, Deal] = {}
    for d in deals:
        if d.destination not in best or d.price < best[d.destination].price:
            best[d.destination] = d
    return list(best.values())


class FlightDealsService:
    def __init__(self, providers: list[Provider]):
        self.providers = providers

    def available(self) -> list[str]:
        return [p.name for p in self.providers if p.enabled]

    async def search(self, params: SearchParams) -> SearchResponse:
        chosen = [p for p in self.providers if p.enabled and (not params.sources or p.name in params.sources)]
        if not chosen:
            raise NoProvidersError(
                "No hay proveedores disponibles. Configura TRAVELPAYOUTS_TOKEN y/o SERPAPI_API_KEY "
                f"(activos: {self.available() or 'ninguno'})."
            )

        results = await asyncio.gather(*(p.search(params) for p in chosen), return_exceptions=True)
        warnings: list[str] = []
        deals: list[Deal] = []
        used: list[str] = []
        for provider, res in zip(chosen, results):
            if isinstance(res, ProviderQueryError):
                # El proveedor funciona pero no acepta esta consulta: sin resultados, con aviso.
                used.append(provider.name)
                warnings.append(str(res))
                continue
            if isinstance(res, ProviderError):
                warnings.append(str(res))
                continue
            if isinstance(res, Exception):
                warnings.append(f"{provider.name}: error inesperado ({res.__class__.__name__}: {res})")
                continue
            found, extra = res
            used.append(provider.name)
            warnings.extend(extra)
            deals.extend(found)

        filtered = [d for d in dedupe(deals) if matches(d, params)]
        if params.group_by_destination:
            filtered = cheapest_per_destination(filtered)
        filtered.sort(key=lambda d: (d.price, d.departure_date))
        filtered = filtered[: params.limit]

        return SearchResponse(
            query=params.model_dump(mode="json", exclude_none=True),
            count=len(filtered),
            sources_used=used,
            warnings=warnings,
            results=filtered,
        )
