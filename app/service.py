"""Orquesta los proveedores, combina billetes, filtra, deduplica y ordena las ofertas."""
from __future__ import annotations

import asyncio
import math

from app.models import (CalendarCell, CalendarResponse, CalendarStats, Deal, DealKind, Leg, SearchParams,
                        SearchResponse, SortBy)
from app.providers.base import Provider, ProviderError, ProviderQueryError

STANDARD_KINDS = (DealKind.one_way, DealKind.round_trip)


class NoProvidersError(Exception):
    pass


# --- Filtros ----------------------------------------------------------------

def _in_ranges(p: SearchParams, dep, ret, nights, price, stops, return_stops) -> bool:
    """Comprobación barata (sin construir objetos) compartida por ofertas y combinaciones."""
    dw, rw = p.depart_search_window, p.return_search_window
    if dw and not dw.contains(dep):
        return False
    if p.is_round_trip:
        if ret is None or ret < dep:
            return False
        if rw and not rw.contains(ret):
            return False
        if p.min_nights is not None and (nights or 0) < p.min_nights:
            return False
        if p.max_nights is not None and (nights or 0) > p.max_nights:
            return False
    if p.min_price is not None and price < p.min_price:
        return False
    if p.max_price is not None and price > p.max_price:
        return False
    if p.direct_only and ((stops or 0) > 0 or (return_stops or 0) > 0):
        return False
    if p.max_shift_total is not None:
        total = abs(_shift(p.depart_window, dep) or 0) + abs(_shift(p.return_window, ret) or 0)
        if total > p.max_shift_total:
            return False
    return True


def _shift(window, d):
    return window.shift(d) if window else None


def matches(deal: Deal, p: SearchParams) -> bool:
    return _in_ranges(p, deal.departure_date, deal.return_date, deal.nights, deal.price,
                      deal.stops, deal.return_stops)


def dedupe(deals: list[Deal]) -> list[Deal]:
    best: dict[tuple, Deal] = {}
    for d in deals:
        key = (d.kind, d.destination, d.return_origin, d.departure_date, d.return_date, d.airline)
        if key not in best or d.price < best[key].price:
            best[key] = d
    return list(best.values())


# --- Orden ------------------------------------------------------------------

def _effective_price(d: Deal) -> float:
    return d.estimated_total if d.estimated_total is not None else d.price


def sort_key(p: SearchParams):
    def key(d: Deal):
        price = _effective_price(d)
        if p.sort == SortBy.price_per_night:
            return (price / d.nights if d.nights else math.inf, price)
        if p.sort == SortBy.nights_desc:
            return (-(d.nights or 0), price)
        if p.sort == SortBy.closest:
            return (d.total_shift, price)
        if p.sort == SortBy.score:
            return (price + p.shift_penalty * d.total_shift, price)
        return (price, d.departure_date)
    return key


def best_per_destination(deals: list[Deal], key) -> list[Deal]:
    best: dict[str, Deal] = {}
    for d in deals:
        if d.destination not in best or key(d) < key(best[d.destination]):
            best[d.destination] = d
    return list(best.values())


# --- Combinaciones de billetes sueltos ---------------------------------------

def _cheapest_cities(legs: list[Leg], attr: str, exclude: set[str], n: int) -> list[str]:
    best: dict[str, float] = {}
    for leg in legs:
        city = getattr(leg, attr)
        if city in exclude:
            continue
        if city not in best or leg.price < best[city]:
            best[city] = leg.price
    return sorted(best, key=best.get)[:n]


def combine_round_trip(p: SearchParams, outbound: dict[str, list[Leg]], inbound: dict[str, list[Leg]],
                       return_cities: dict[str, list[str]]) -> list[Deal]:
    """Une billetes de ida (origen→X) con billetes de vuelta (Y→origen) en ofertas de ida y vuelta."""
    deals: list[Deal] = []
    for dest, outs in outbound.items():
        for ret_city in return_cities.get(dest, []):
            ins = inbound.get(ret_city, [])
            for o in outs:
                for i in ins:
                    if i.departure_date < o.departure_date:
                        continue
                    if o.arrival_at and i.departure_at and i.departure_at <= o.arrival_at:
                        continue
                    nights = (i.departure_date - o.departure_date).days
                    price = round(o.price + i.price, 2)
                    if not _in_ranges(p, o.departure_date, i.departure_date, nights, price, o.stops, i.stops):
                        continue
                    open_jaw = ret_city != dest
                    deals.append(Deal(
                        source=o.source,
                        kind=DealKind.open_jaw if open_jaw else DealKind.split,
                        origin=p.origin,
                        destination=dest,
                        destination_name=o.destination_name,
                        destination_country=o.destination_country,
                        departure_date=o.departure_date,
                        return_date=i.departure_date,
                        nights=nights,
                        price=price,
                        currency=o.currency,
                        airline=o.airline if o.airline == i.airline else "+".join(filter(None, (o.airline, i.airline))) or None,
                        stops=o.stops,
                        return_stops=i.stops,
                        return_origin=ret_city if open_jaw else None,
                        legs=[o, i],
                        notes=["Dos billetes separados: resérvalos por separado con el enlace de cada tramo"],
                    ))
    return deals


class FlightDealsService:
    def __init__(self, providers: list[Provider]):
        self.providers = providers

    def available(self) -> list[str]:
        return [p.name for p in self.providers if p.enabled]

    def _chosen(self, params: SearchParams) -> list[Provider]:
        chosen = [p for p in self.providers if p.enabled and (not params.sources or p.name in params.sources)]
        if not chosen:
            raise NoProvidersError(
                "No hay proveedores disponibles. Configura TRAVELPAYOUTS_TOKEN y/o SERPAPI_API_KEY "
                f"(activos: {self.available() or 'ninguno'})."
            )
        return chosen

    # --- Llamadas a proveedores ----------------------------------------------
    @staticmethod
    def _absorb(provider: Provider, res, deals: list, warnings: list[str], used: list[str]) -> None:
        if isinstance(res, ProviderQueryError):
            # El proveedor funciona pero no acepta esta consulta: sin resultados, con aviso.
            used.append(provider.name)
            warnings.append(str(res))
        elif isinstance(res, ProviderError):
            warnings.append(str(res))
        elif isinstance(res, Exception):
            warnings.append(f"{provider.name}: error inesperado ({res.__class__.__name__}: {res})")
        else:
            found, extra = res
            used.append(provider.name)
            warnings.extend(extra)
            deals.extend(found)

    async def _legs_many(self, prov: Provider, routes: set[tuple[str | None, str | None]], window, params,
                         warnings: list[str]) -> dict[tuple, list[Leg]]:
        routes = sorted(routes, key=str)
        res = await asyncio.gather(*(prov.one_way_legs(o, d, window, params.currency, params.direct_only)
                                     for o, d in routes), return_exceptions=True)
        out: dict[tuple, list[Leg]] = {}
        for route, r in zip(routes, res):
            if isinstance(r, Exception):
                if isinstance(r, ProviderError):
                    warnings.append(str(r))
                else:
                    warnings.append(f"{prov.name}: error inesperado ({r.__class__.__name__}: {r})")
                out[route] = []
            else:
                out[route] = r
        return out

    async def _split_tickets(self, p: SearchParams, prov: Provider) -> tuple[list[Deal], list[str]]:
        warnings: list[str] = []
        dsw, rsw = p.depart_search_window, p.return_search_window
        exclude = {p.origin}

        # 1) Destinos candidatos
        if p.destination:
            dests = [p.destination]
        else:
            broad = await self._legs_many(prov, {(p.origin, None)}, dsw, p, warnings)
            dests = _cheapest_cities(broad[(p.origin, None)], "destination", exclude, p.max_candidates)

        # 2) Billetes concretos de ida (en paralelo)
        outbound_res = await self._legs_many(prov, {(p.origin, d) for d in dests}, dsw, p, warnings)
        outbound = {d: outbound_res[(p.origin, d)] for d in dests}

        # 3) Ciudades desde las que se vuelve
        if p.return_origin:
            return_cities = {d: [p.return_origin] for d in dests}
        elif p.open_jaw:
            # Otras ciudades del mismo país que el destino conectadas con el origen. Se descubren con
            # dos consultas amplias (cualquiera→origen y origen→cualquiera), porque cada una solo
            # devuelve la opción más barata de cada día.
            wide = await self._legs_many(prov, {(None, p.origin), (p.origin, None)}, rsw, p, warnings)
            country_of: dict[str, str | None] = {}
            price_of: dict[str, float] = {}
            for leg in wide[(None, p.origin)]:
                country_of[leg.origin] = leg.origin_country
                price_of[leg.origin] = min(price_of.get(leg.origin, math.inf), leg.price)
            for leg in wide[(p.origin, None)]:
                country_of.setdefault(leg.destination, leg.destination_country)
                price_of.setdefault(leg.destination, leg.price)
            ranked = [c for c in sorted(price_of, key=price_of.get) if c not in exclude]
            return_cities = {}
            for d in dests:
                near = []
                for c in ranked:
                    if c == d:
                        continue
                    km = await prov.distance_km(d, c) if hasattr(prov, "distance_km") else None
                    # Sin coordenadas, se acepta solo si es del mismo país
                    ok = km <= p.open_jaw_max_km if km is not None else (
                        country_of.get(c) is not None and country_of.get(c) == country_of.get(d))
                    if ok:
                        near.append(c)
                return_cities[d] = near[: p.max_candidates]
            if not any(return_cities.values()):
                warnings.append(f"open_jaw: no hay vueltas con datos desde ciudades a menos de "
                                f"{p.open_jaw_max_km} km del destino; prueba con return_origin u open_jaw_max_km")
        else:
            return_cities = {d: [d] for d in dests}

        # 4) Billetes concretos de vuelta (en paralelo) y combinación
        in_routes = {(c, p.origin) for cities in return_cities.values() for c in cities}
        inbound_res = await self._legs_many(prov, in_routes, rsw, p, warnings)
        inbound = {c: inbound_res[(c, p.origin)] for c, _ in in_routes}
        return combine_round_trip(p, outbound, inbound, return_cities), warnings

    async def collect(self, params: SearchParams) -> tuple[list[Deal], list[str], list[str]]:
        """Consulta proveedores y devuelve (ofertas filtradas sin ordenar, avisos, fuentes usadas)."""
        chosen = self._chosen(params)
        warnings: list[str] = []
        deals: list[Deal] = []
        used: list[str] = []

        tasks = [prov.search(params) for prov in chosen]
        split_provs = [prov for prov in chosen if params.uses_split and prov.supports_one_way_legs]
        tasks += [self._split_tickets(params, prov) for prov in split_provs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for provider, res in zip(chosen + split_provs, results):
            self._absorb(provider, res, deals, warnings, used)
        if params.uses_split and not split_provs:
            warnings.append("Billetes sueltos / open-jaw solo están disponibles con travelpayouts")

        filtered = [d for d in dedupe(deals) if matches(d, params)]
        self._annotate(filtered, params)
        # open-jaw o return_origin: solo interesan las combinaciones que vuelven desde otra ciudad
        # (las normales se han usado antes como referencia para calcular el ahorro)
        if params.open_jaw or params.return_origin:
            filtered = [d for d in filtered if d.kind == DealKind.open_jaw]
        return filtered, list(dict.fromkeys(warnings)), list(dict.fromkeys(used))

    @staticmethod
    def _annotate(deals: list[Deal], p: SearchParams) -> None:
        """Añade desplazamiento de fechas y ahorro frente a la mejor opción con las fechas exactas."""
        flex = bool(p.depart_flex_days or p.return_flex_days)
        if flex:
            for d in deals:
                d.depart_shift = _shift(p.depart_window, d.departure_date)
                d.return_shift = _shift(p.return_window, d.return_date) if d.return_date else None
        if not (flex or p.uses_split):
            return
        reference: dict[str, float] = {}
        for d in deals:
            if d.kind in STANDARD_KINDS and d.total_shift == 0:
                reference[d.destination] = min(reference.get(d.destination, math.inf), d.price)
        for d in deals:
            ref = reference.get(d.destination)
            if ref is not None and (d.kind not in STANDARD_KINDS or d.total_shift):
                d.savings_vs_exact = round(ref - _effective_price(d), 2)

    async def calendar(self, params: SearchParams) -> CalendarResponse:
        """Precio más barato por fecha de ida (y de vuelta), para ver qué días salen mejor."""
        deals, warnings, used = await self.collect(params)
        best: dict[tuple, Deal] = {}
        for d in deals:
            key = (d.departure_date, d.return_date)
            if key not in best or _effective_price(d) < _effective_price(best[key]):
                best[key] = d
        ordered = sorted(best.values(), key=lambda d: (d.departure_date, d.return_date or d.departure_date))
        prices = sorted(_effective_price(d) for d in ordered)
        if prices:
            low_cut, high_cut = prices[(len(prices) - 1) // 3], prices[(2 * (len(prices) - 1)) // 3]
        cells: list[CalendarCell] = []
        grid: dict = {}
        for d in ordered:
            price = _effective_price(d)
            level = "low" if price <= low_cut else "high" if price > high_cut else "mid"
            cells.append(CalendarCell(
                departure_date=d.departure_date, return_date=d.return_date, nights=d.nights, price=price,
                price_level=level, destination=d.destination, destination_name=d.destination_name,
                kind=d.kind, airline=d.airline, stops=d.stops,
                link=d.link or (d.legs[0].link if d.legs else None),
            ))
            dep = d.departure_date.isoformat()
            if params.is_round_trip:
                grid.setdefault(dep, {})[d.return_date.isoformat()] = price
            else:
                grid[dep] = price
        return CalendarResponse(
            query=params.model_dump(mode="json", exclude_none=True),
            trip="round_trip" if params.is_round_trip else "one_way",
            currency=params.currency,
            sources_used=used,
            warnings=warnings,
            stats=CalendarStats(cells=len(prices), min=prices[0], max=prices[-1],
                                avg=round(sum(prices) / len(prices), 2)) if prices else None,
            cheapest=min(cells, key=lambda c: (c.price, c.departure_date)) if cells else None,
            cells=cells,
            grid=grid,
        )

    async def search(self, params: SearchParams) -> SearchResponse:
        deals, warnings, used = await self.collect(params)
        key = sort_key(params)
        if params.group_by_destination:
            deals = best_per_destination(deals, key)
        deals.sort(key=key)
        deals = deals[: params.limit]
        return SearchResponse(
            query=params.model_dump(mode="json", exclude_none=True),
            count=len(deals),
            sources_used=used,
            warnings=warnings,
            results=deals,
        )
