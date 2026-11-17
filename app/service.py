"""Orquesta los proveedores, combina billetes, filtra, deduplica y ordena las ofertas."""
from __future__ import annotations

import asyncio
import math

from app.models import (MAX_FLEX_DAYS, CalendarCell, CalendarResponse, CalendarStats, Deal, DealKind, Leg,
                        SearchMode, SearchParams, SearchResponse, SortBy, TripType)
from app.providers.base import Provider, ProviderError, ProviderQueryError

STANDARD_KINDS = (DealKind.one_way, DealKind.round_trip)
NEARBY_FLEX = 3   # margen ± días mínimo al proponer ciudades cercanas


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
    stops, return_stops = deal.stops, deal.return_stops
    if deal.kind == DealKind.self_transfer and deal.legs:
        # la conexión propia no cuenta como escala: direct_only exige que cada tramo sea directo
        stops, return_stops = max((leg.stops or 0) for leg in deal.legs), None
    return _in_ranges(p, deal.departure_date, deal.return_date, deal.nights, deal.price, stops, return_stops)


def dedupe(deals: list[Deal]) -> list[Deal]:
    best: dict[tuple, Deal] = {}
    for d in deals:
        key = (d.kind, d.destination, d.return_origin, d.layover_city, d.departure_date, d.return_date,
               d.airline, d.legs[0].departure_at if d.legs else None)
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
        if p.sort == SortBy.duration:
            return (d.duration_minutes if d.duration_minutes is not None else math.inf, price)
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


SELF_TRANSFER_WARNING = (
    "Modo avanzado: las combinaciones self_transfer son dos billetes independientes. Si el primer vuelo "
    "se retrasa y pierdes el segundo, no hay protección ni compensación; recoges y vuelves a facturar "
    "el equipaje; revisa visados/Schengen en la ciudad intermedia y confirma el precio de AMBOS tramos."
)


def _merge_legs(*groups: list[Leg]) -> list[Leg]:
    seen: dict[tuple, Leg] = {}
    for group in groups:
        for leg in group:
            key = (leg.origin, leg.destination, leg.departure_at, leg.airline, leg.flight_number)
            if key not in seen or leg.price < seen[key].price:
                seen[key] = leg
    return list(seen.values())


def combine_self_transfer(p: SearchParams, first: dict[str, list[Leg]], second: dict[str, list[Leg]]) -> list[Deal]:
    """Enlaza A→X con X→B respetando la conexión mínima y los días máximos en la ciudad intermedia."""
    deals: list[Deal] = []
    min_gap_h = p.min_connection_hours
    for hub, legs1 in first.items():
        legs2 = second.get(hub, [])
        for a in legs1:
            if not a.arrival_at:
                continue
            for b in legs2:
                if not b.departure_at:
                    continue
                gap_h = (b.departure_at - a.arrival_at).total_seconds() / 3600
                if gap_h < min_gap_h:
                    continue
                arrival_local = a.arrival_at.astimezone(b.departure_at.tzinfo)
                nights = (b.departure_at.date() - arrival_local.date()).days
                if nights > p.max_layover_days:
                    continue
                airport_change = bool(a.destination_airport and b.origin_airport
                                      and a.destination_airport != b.origin_airport)
                if airport_change and not p.allow_airport_change:
                    continue
                price = round(a.price + b.price, 2)
                if not _in_ranges(p, a.departure_date, None, None, price, a.stops, b.stops):
                    continue
                total = round(price + nights * p.stopover_cost_per_night, 2) if p.stopover_cost_per_night else None
                notes = ["Dos billetes separados: resérvalos por separado con el enlace de cada tramo",
                         f"Conexión por tu cuenta en {hub}: {gap_h:.1f} h"]
                if airport_change:
                    notes.append(f"Cambio de aeropuerto en {hub}: llegas a {a.destination_airport} "
                                 f"y sales de {b.origin_airport}")
                if nights:
                    notes.append(f"{nights} noche(s) en {hub}")
                end = b.arrival_at or b.departure_at
                deals.append(Deal(
                    source=a.source,
                    kind=DealKind.self_transfer,
                    origin=p.origin,
                    destination=p.destination,
                    destination_name=b.destination_name,
                    destination_country=b.destination_country,
                    departure_date=a.departure_date,
                    price=price,
                    currency=a.currency,
                    airline="+".join(filter(None, (a.airline, b.airline))) or None,
                    stops=(a.stops or 0) + (b.stops or 0) + 1,
                    duration_minutes=int((end - a.departure_at).total_seconds() // 60),
                    legs=[a, b],
                    layover_city=hub,
                    layover_hours=round(gap_h, 1),
                    airport_change=airport_change,
                    # connection: mismo día · overnight: pasas una noche · stopover: 24 h o más en la ciudad
                    stopover_type="stopover" if gap_h >= 24 else "overnight" if nights else "connection",
                    estimated_total=total,
                    notes=notes,
                ))
    return deals


def leg_as_deal(leg: Leg) -> Deal:
    """Un billete de solo ida suelto, para combinarlo con una opción self_transfer."""
    return Deal(source=leg.source, kind=DealKind.one_way, origin=leg.origin, destination=leg.destination,
                destination_name=leg.destination_name, destination_country=leg.destination_country,
                departure_date=leg.departure_date, price=leg.price, currency=leg.currency, airline=leg.airline,
                flight_number=leg.flight_number, stops=leg.stops, duration_minutes=leg.duration_minutes,
                link=leg.link, legs=[leg])


ADVANCED_ROUND_TRIP_TOP = 30   # opciones más baratas por sentido que se cruzan entre sí


def combine_advanced_round_trip(p: SearchParams, outs: list[Deal], ins: list[Deal]) -> list[Deal]:
    """Cruza opciones de ida y de vuelta (directas o self_transfer); al menos una debe ser self_transfer
    (las que no lo son ya las cubren la búsqueda normal y split_tickets)."""
    outs = sorted(outs, key=_effective_price)[:ADVANCED_ROUND_TRIP_TOP]
    ins = sorted(ins, key=_effective_price)[:ADVANCED_ROUND_TRIP_TOP]
    deals: list[Deal] = []
    for o in outs:
        o_end = o.legs[-1].arrival_at or o.legs[-1].departure_at
        for i in ins:
            if o.kind != DealKind.self_transfer and i.kind != DealKind.self_transfer:
                continue
            i_start = i.legs[0].departure_at
            if o_end and i_start and i_start <= o_end:
                continue
            dep, ret = o.departure_date, i.departure_date
            nights = (ret - dep).days
            price = round(o.price + i.price, 2)
            max_stops = max((leg.stops or 0) for leg in o.legs + i.legs)
            if not _in_ranges(p, dep, ret, nights, price, max_stops, None):
                continue
            extra = (o.estimated_total or o.price) + (i.estimated_total or i.price)
            notes = ["Varios billetes separados: resérvalos por separado con el enlace de cada tramo"]
            for label, d in (("Ida", o), ("Vuelta", i)):
                notes += [f"{label}: {n}" for n in d.notes or [] if not n.startswith("Dos billetes")]
            airlines = list(dict.fromkeys(filter(None, (leg.airline for leg in o.legs + i.legs))))
            deals.append(Deal(
                source=o.source,
                kind=DealKind.self_transfer,
                origin=p.origin,
                destination=p.destination,
                destination_name=o.destination_name,
                destination_country=o.destination_country,
                departure_date=dep,
                return_date=ret,
                nights=nights,
                price=price,
                currency=o.currency,
                airline="+".join(airlines) or None,
                stops=o.stops,
                return_stops=i.stops,
                duration_minutes=o.duration_minutes,
                legs=o.legs + i.legs,
                layover_city=o.layover_city,
                layover_hours=o.layover_hours,
                return_layover_city=i.layover_city,
                return_layover_hours=i.layover_hours,
                airport_change=bool(o.airport_change or i.airport_change),
                stopover_type=o.stopover_type or i.stopover_type,
                estimated_total=round(extra, 2) if (o.estimated_total or i.estimated_total) else None,
                notes=notes,
            ))
    return deals


def one_way_view(p: SearchParams, origin: str, destination: str, start, end, flex: int) -> SearchParams:
    """Copia de los parámetros como búsqueda de solo ida (para cada sentido del modo avanzado)."""
    return p.model_copy(update=dict(
        origin=origin, destination=destination, depart_from=start, depart_to=end, depart_flex=flex,
        flex_days=None, return_from=None, return_to=None, return_flex=None, trip_type=TripType.one_way,
        min_nights=None, max_nights=None, min_price=None, max_price=None, max_shift_total=None))


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
                         warnings: list[str], windows: dict | None = None) -> dict[tuple, list[Leg]]:
        """Billetes de solo ida para varias rutas en paralelo; `windows` permite una ventana por ruta."""
        routes = sorted(routes, key=str)
        res = await asyncio.gather(*(prov.one_way_legs(o, d, (windows or {}).get((o, d), window),
                                                       params.currency, params.direct_only)
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

        out_routes = {(p.origin, d) for d in dests}
        if not p.open_jaw or p.return_origin:
            # Ciudades de vuelta conocidas de antemano: ida y vuelta se piden a la vez
            return_cities = {d: [p.return_origin or d] for d in dests}
            in_routes = {(c, p.origin) for cities in return_cities.values() for c in cities}
            out_res, in_res = await asyncio.gather(self._legs_many(prov, out_routes, dsw, p, warnings),
                                                   self._legs_many(prov, in_routes, rsw, p, warnings))
            outbound = {d: out_res[(p.origin, d)] for d in dests}
            inbound = {c: in_res[(c, p.origin)] for c, _ in in_routes}
            return combine_round_trip(p, outbound, inbound, return_cities), warnings

        # 2) open_jaw sin ciudad: ida concreta y descubrimiento de ciudades de vuelta a la vez
        outbound_res, wide = await asyncio.gather(
            self._legs_many(prov, out_routes, dsw, p, warnings),
            self._legs_many(prov, {(None, p.origin), (p.origin, None)}, None, p, warnings))
        outbound = {d: outbound_res[(p.origin, d)] for d in dests}
        # 3) Ciudades cercanas al destino conectadas con el origen. Se descubren con la red de rutas
        #    (cualquiera→origen y origen→cualquiera, sin fechas): con fechas solo devuelve la opción
        #    más barata de cada día y salen muy pocas ciudades.
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

        # 4) Billetes concretos de vuelta (en paralelo) y combinación
        in_routes = {(c, p.origin) for cities in return_cities.values() for c in cities}
        inbound_res = await self._legs_many(prov, in_routes, rsw, p, warnings)
        inbound = {c: inbound_res[(c, p.origin)] for c, _ in in_routes}
        return combine_round_trip(p, outbound, inbound, return_cities), warnings

    async def _self_transfer(self, p: SearchParams, prov: Provider) -> tuple[list[Deal], list[str]]:
        if not p.is_round_trip:
            return await self._self_transfer_leg(p, prov)
        # Ida y vuelta: cada sentido se resuelve como un tramo de solo ida y luego se cruzan,
        # admitiendo también el vuelo directo en un sentido y la conexión propia en el otro.
        a, b = p.origin, p.destination
        out_p = one_way_view(p, a, b, p.depart_from, p.depart_to, p.depart_flex_days)
        in_p = one_way_view(p, b, a, p.return_from, p.return_to, p.return_flex_days)
        warnings: list[str] = []
        (outs, w_out), (ins, w_in), direct = await asyncio.gather(
            self._self_transfer_leg(out_p, prov), self._self_transfer_leg(in_p, prov),
            self._legs_many(prov, {(a, b), (b, a)}, None, p, warnings,
                            windows={(a, b): out_p.depart_search_window, (b, a): in_p.depart_search_window}))
        warnings += [f"Ida: {w}" for w in w_out] + [f"Vuelta: {w}" for w in w_in]
        outs += [leg_as_deal(leg) for leg in direct[(a, b)]]
        ins += [leg_as_deal(leg) for leg in direct[(b, a)]]
        return combine_advanced_round_trip(p, outs, ins), warnings

    async def _self_transfer_leg(self, p: SearchParams, prov: Provider) -> tuple[list[Deal], list[str]]:
        warnings: list[str] = []
        a, b = p.origin, p.destination
        first_w = p.depart_search_window
        # El segundo vuelo puede salir hasta max_layover_days (+1 por llegadas de madrugada) después
        second_w = first_w.expand(0, p.max_layover_days + 1)

        # 1) Descubrir ciudades intermedias con la red de rutas: A→cualquiera y cualquiera→B SIN filtro
        #    de fechas (con fechas solo devuelve la opción más barata de cada día y casi nunca coinciden).
        wide = await self._legs_many(prov, {(a, None), (None, b)}, None, p, warnings)
        network_a, network_b = wide[(a, None)], wide[(None, b)]
        best1: dict[str, float] = {}
        best2: dict[str, float] = {}
        for leg in network_a:
            best1[leg.destination] = min(best1.get(leg.destination, math.inf), leg.price)
        for leg in network_b:
            best2[leg.origin] = min(best2.get(leg.origin, math.inf), leg.price)
        # Solo sirven ciudades conectadas por los dos lados; se priorizan las rutas más baratas
        hubs = sorted((h for h in best1.keys() & best2.keys() if h not in {a, b}),
                      key=lambda h: best1[h] + best2[h])[: p.max_candidates]
        if not hubs:
            warnings.append(f"Modo avanzado: no hay ciudades intermedias conectadas con {a} y con {b}")
            return [], warnings
        from_a = [l for l in network_a if first_w.contains(l.departure_date)]
        to_b = [l for l in network_b if second_w.contains(l.departure_date)]

        # 2) Vuelos concretos A→X y X→B para cada ciudad intermedia (en paralelo)
        fixed = await self._legs_many(
            prov, {(a, h) for h in hubs} | {(h, b) for h in hubs}, None, p, warnings,
            windows={**{(a, h): first_w for h in hubs}, **{(h, b): second_w for h in hubs}})
        first = {h: _merge_legs(fixed[(a, h)], [l for l in from_a if l.destination == h]) for h in hubs}
        second = {h: _merge_legs(fixed[(h, b)], [l for l in to_b if l.origin == h]) for h in hubs}
        deals = combine_self_transfer(p, first, second)
        if not deals:
            warnings.append(f"Modo avanzado: se probaron {len(hubs)} ciudades intermedias ({', '.join(hubs)}) "
                            "pero no hay vuelos con datos que enlacen en esas fechas; amplía flex_days o "
                            "max_layover_days")
        return deals, warnings

    async def collect(self, params: SearchParams) -> tuple[list[Deal], list[str], list[str]]:
        """Consulta proveedores y devuelve (ofertas filtradas sin ordenar, avisos, fuentes usadas)."""
        chosen = self._chosen(params)
        warnings: list[str] = []
        deals: list[Deal] = []
        used: list[str] = []

        leg_provs = [prov for prov in chosen if prov.supports_one_way_legs]
        advanced = params.mode == SearchMode.advanced
        tasks = [prov.search(params) for prov in chosen]
        owners = list(chosen)
        if params.uses_split:
            tasks += [self._split_tickets(params, prov) for prov in leg_provs]
            owners += leg_provs
        if advanced:
            tasks += [self._self_transfer(params, prov) for prov in leg_provs]
            owners += leg_provs
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for provider, res in zip(owners, results):
            self._absorb(provider, res, deals, warnings, used)
        if (params.uses_split or advanced) and not leg_provs:
            warnings.append("Billetes sueltos, open-jaw y modo avanzado solo están disponibles con travelpayouts")
        if any(d.kind == DealKind.self_transfer for d in deals):
            warnings.insert(0, SELF_TRANSFER_WARNING)

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
        if not (flex or p.uses_split or p.mode == SearchMode.advanced):
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

    # --- Alternativas cuando no hay resultados --------------------------------
    async def _nearby(self, prov, legs: list[Leg], attr: str, center: str, p: SearchParams,
                      exclude: set[str], n: int) -> list[tuple[str, float]]:
        """Ciudades de la red de rutas a menos de nearby_km de `center`, de más barata a más cara."""
        best: dict[str, float] = {}
        for leg in legs:
            city = getattr(leg, attr)
            if city not in exclude:
                best[city] = min(best.get(city, math.inf), leg.price)
        out = []
        for city in sorted(best, key=best.get):
            km = await prov.distance_km(center, city)
            if km is not None and km <= p.nearby_km:
                out.append((city, km))
                if len(out) == n:
                    break
        return out

    async def _alternatives(self, p: SearchParams) -> list[Deal]:
        """Sin resultados: prueba fechas cercanas (±MAX_FLEX_DAYS) y ciudades cercanas de destino/origen."""
        variants: list[tuple[SearchParams, str]] = []
        if p.depart_window and (p.depart_flex_days < MAX_FLEX_DAYS
                                or (p.return_window and p.return_flex_days < MAX_FLEX_DAYS)):
            variants.append((p.model_copy(update=dict(flex_days=MAX_FLEX_DAYS, depart_flex=None, return_flex=None,
                                                      max_shift_total=None)),
                             f"fechas cercanas (±{MAX_FLEX_DAYS} días)"))
        prov = next((x for x in self._chosen(p) if x.supports_one_way_legs and hasattr(x, "distance_km")), None)
        if prov and p.destination:
            # Red de rutas (sin fechas, 2 llamadas cacheadas): qué ciudades cercanas tienen datos
            wide = await self._legs_many(prov, {(p.origin, None), (None, p.destination)}, None, p, [])
            exclude = {p.origin, p.destination}
            # Ciudades cercanas: búsqueda normal y al menos ±NEARBY_FLEX días (con fechas exactas casi nunca hay datos)
            simple = dict(mode=SearchMode.standard, open_jaw=False, return_origin=None,
                          flex_days=max(p.flex_days or 0, NEARBY_FLEX) if p.depart_window else None)
            for city, km in await self._nearby(prov, wide[(p.origin, None)], "destination", p.destination,
                                               p, exclude, 3):
                variants.append((p.model_copy(update=dict(destination=city, **simple)),
                                 f"destino cercano: {city} a {km:.0f} km de {p.destination}"))
            for city, km in await self._nearby(prov, wide[(None, p.destination)], "origin", p.origin,
                                               p, exclude, 2):
                variants.append((p.model_copy(update=dict(origin=city, **simple)),
                                 f"origen cercano: {city} a {km:.0f} km de {p.origin}"))
        results = await asyncio.gather(*(self.collect(v) for v, _ in variants), return_exceptions=True)
        key = sort_key(p)
        out: list[Deal] = []
        for (_, reason), res in zip(variants, results):
            if isinstance(res, Exception):
                continue
            found = sorted(res[0], key=key)[:5]      # las 5 mejores de cada alternativa, para variar
            for d in found:
                d.alternative = reason
            out += found
        out.sort(key=key)
        return out[: p.limit]

    async def search(self, params: SearchParams) -> SearchResponse:
        deals, warnings, used = await self.collect(params)
        key = sort_key(params)
        if params.group_by_destination:
            deals = best_per_destination(deals, key)
        deals.sort(key=key)
        deals = deals[: params.limit]
        alternatives: list[Deal] = []
        if not deals and used and params.suggest_alternatives:
            alternatives = await self._alternatives(params)
            warnings.append(f"Sin resultados con los criterios exactos; {len(alternatives)} alternativas "
                            "cercanas en `alternatives`" if alternatives else
                            "Sin resultados ni alternativas cercanas (fechas ±7 días, ciudades a menos de "
                            f"{params.nearby_km} km): esta ruta apenas tiene búsquedas recientes")
        return SearchResponse(
            query=params.model_dump(mode="json", exclude_none=True),
            count=len(deals),
            sources_used=used,
            warnings=warnings,
            results=deals,
            alternatives=alternatives,
        )
