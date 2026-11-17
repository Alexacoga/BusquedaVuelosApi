"""Tests con respuestas simuladas de Travelpayouts y SerpApi (sin red)."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.cache import TTLCache
from app.config import get_settings
from app.main import app
from app.models import SearchParams
from app.providers import SerpApiProvider, TravelpayoutsProvider
from app.service import FlightDealsService

T = date.today()
D = lambda n: (T + timedelta(days=n)).isoformat()  # noqa: E731

TP_DATA = [
    # destino, salida(días), vuelta(días), precio, escalas
    ("BCN", 10, 14, 35, 0),
    ("LIS", 12, 19, 60, 0),
    ("ROM", 20, 25, 45, 1),
    ("PAR", 40, 44, 30, 0),   # fuera del rango de ida de los tests
    ("BCN", 11, 15, 80, 0),
]

SERP_DESTS = [
    {"name": "Lisboa", "country": "Portugal", "destination_airport": {"code": "LIS"},
     "start_date": D(12), "end_date": D(19), "flight_price": 52, "airline": "TAP",
     "number_of_stops": 0, "flight_duration": 75, "link": "https://www.google.com/travel/flights?x"},
    {"name": "Londres", "country": "Reino Unido", "destination_airport": {"code": "LHR"},
     "start_date": D(15), "end_date": D(18), "flight_price": 70, "airline": "Iberia",
     "number_of_stops": 0, "flight_duration": 140, "link": "https://www.google.com/travel/flights?y"},
]


class Recorder:
    def __init__(self):
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        if "cities.json" in url:
            return httpx.Response(200, json=[{"code": "BCN", "name": "Barcelona", "country_code": "ES"},
                                             {"code": "LIS", "name": "Lisboa", "country_code": "PT"}])
        if "travelpayouts" in url:
            one_way = request.url.params["one_way"] == "true"
            dest = request.url.params.get("destination")
            data = [
                {"origin": "MAD", "destination": d, "price": p, "airline": "VY", "flight_number": 1000 + i,
                 "departure_at": f"{D(dep)}T08:00:00+01:00",
                 "return_at": None if one_way else f"{D(ret)}T20:00:00+01:00",
                 "transfers": s, "return_transfers": s, "duration_to": 90, "link": f"/search/MAD{d}?t=1"}
                for i, (d, dep, ret, p, s) in enumerate(TP_DATA) if not dest or d == dest
            ]
            return httpx.Response(200, json={"success": True, "data": data, "currency": "eur"})
        if "serpapi" in url:
            return httpx.Response(200, json={"destinations": SERP_DESTS})
        return httpx.Response(404)


def make_service(rec: Recorder, tp=True, serp=True) -> FlightDealsService:
    s = replace(get_settings(), travelpayouts_token="tok" if tp else None,
                serpapi_api_key="key" if serp else None, travelpayouts_marker="12345")
    client = httpx.AsyncClient(transport=httpx.MockTransport(rec))
    cache = TTLCache(3600)
    return FlightDealsService([TravelpayoutsProvider(client, cache, s), SerpApiProvider(client, cache, s)])


@pytest.fixture
def rec():
    return Recorder()


@pytest.fixture
def client(rec):
    app.state.service = make_service(rec)
    with TestClient(app) as c:
        yield c
    app.state.service = None


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "providers": ["travelpayouts", "google_flights"]}


def test_anywhere_round_trip_with_windows(client):
    r = client.get("/deals", params={"origin": "mad", "depart_from": D(9), "depart_to": D(21),
                                     "return_from": D(13), "return_to": D(26)})
    assert r.status_code == 200, r.text
    body = r.json()
    prices = [d["price"] for d in body["results"]]
    assert prices == sorted(prices)
    dests = {d["destination"] for d in body["results"]}
    assert "PAR" not in dests                     # fuera de rango de ida
    assert {"BCN", "LIS", "ROM", "LHR"} <= dests
    assert set(body["sources_used"]) == {"travelpayouts", "google_flights"}
    bcn = next(d for d in body["results"] if d["destination"] == "BCN")
    assert bcn["destination_name"] == "Barcelona" and bcn["nights"] == 4
    assert bcn["link"].startswith("https://www.aviasales.com/search/MADBCN") and "marker=12345" in bcn["link"]


def test_price_and_direct_filters(client):
    r = client.get("/deals", params={"origin": "MAD", "depart_from": D(9), "depart_to": D(21),
                                     "return_to": D(30), "min_price": 40, "max_price": 65, "direct_only": True})
    body = r.json()
    assert all(40 <= d["price"] <= 65 and d["stops"] == 0 for d in body["results"])
    assert {d["destination"] for d in body["results"]} == {"LIS"}


def test_group_by_destination(client):
    r = client.get("/deals", params={"origin": "MAD", "depart_from": D(9), "depart_to": D(21),
                                     "return_to": D(30), "group_by_destination": True})
    res = r.json()["results"]
    assert len(res) == len({d["destination"] for d in res})
    assert next(d for d in res if d["destination"] == "LIS")["price"] == 52  # Google más barato que TP (60)
    assert next(d for d in res if d["destination"] == "BCN")["price"] == 35


def test_one_way_only_depart(client, rec):
    r = client.get("/deals", params={"origin": "MAD", "destination": "BCN", "depart_from": D(9),
                                     "depart_to": D(12), "sources": "travelpayouts"})
    body = r.json()
    assert body["count"] == 2 and all("return_date" not in d for d in body["results"])
    tp_calls = [c for c in rec.calls if "prices_for_dates" in str(c.url)]
    assert all(c.url.params["one_way"] == "true" and c.url.params["destination"] == "BCN" for c in tp_calls)


def test_nights_filter(client):
    r = client.get("/deals", params={"origin": "MAD", "depart_from": D(9), "depart_to": D(25),
                                     "min_nights": 5, "max_nights": 7, "sources": "travelpayouts"})
    assert {d["destination"] for d in r.json()["results"]} == {"LIS", "ROM"}


def test_validation_errors(client):
    assert client.get("/deals", params={"origin": "MADRID"}).status_code == 422
    assert client.get("/deals", params={"origin": "MAD", "depart_from": D(10), "depart_to": D(5)}).status_code == 422
    assert client.get("/deals", params={"origin": "MAD", "min_price": 100, "max_price": 50}).status_code == 422
    assert client.get("/deals", params={"origin": "MAD", "depart_from": D(-3)}).status_code == 422
    r = client.get("/deals", params={"origin": "MAD", "trip_type": "one_way", "return_from": D(5)})
    assert r.status_code == 422 and "one_way" in r.text


def test_no_providers_configured(rec):
    app.state.service = make_service(rec, tp=False, serp=False)
    with TestClient(app) as c:
        r = c.get("/deals", params={"origin": "MAD"})
    app.state.service = None
    assert r.status_code == 503 and "TRAVELPAYOUTS_TOKEN" in r.json()["detail"]


def test_provider_failure_is_a_warning(rec):
    def failing(request):
        if "serpapi" in str(request.url):
            return httpx.Response(401, json={"error": "Invalid API key"})
        return rec(request)
    s = replace(get_settings(), travelpayouts_token="tok", serpapi_api_key="bad")
    client = httpx.AsyncClient(transport=httpx.MockTransport(failing))
    cache = TTLCache(0)
    app.state.service = FlightDealsService([TravelpayoutsProvider(client, cache, s), SerpApiProvider(client, cache, s)])
    with TestClient(app) as c:
        body = c.get("/deals", params={"origin": "MAD"}).json()
    app.state.service = None
    assert body["sources_used"] == ["travelpayouts"] and "HTTP 401" in body["warnings"][0]


def test_rejected_query_is_empty_result_not_error(rec):
    def unknown_code(request):
        if "travelpayouts" in str(request.url) and request.url.params.get("destination") == "FRU":
            return httpx.Response(400, json={"error": "bad request: unknown location code `FRU`",
                                             "success": False})
        return rec(request)
    s = replace(get_settings(), travelpayouts_token="tok", serpapi_api_key=None)
    client = httpx.AsyncClient(transport=httpx.MockTransport(unknown_code))
    app.state.service = FlightDealsService([TravelpayoutsProvider(client, TTLCache(0), s),
                                            SerpApiProvider(client, TTLCache(0), s)])
    with TestClient(app) as c:
        r = c.get("/deals", params={"origin": "BCN", "destination": "FRU", "depart_from": D(5)})
    app.state.service = None
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 0 and body["sources_used"] == ["travelpayouts"]
    assert "unknown location code" in body["warnings"][0]


def test_auth_failure_still_502(rec):
    s = replace(get_settings(), travelpayouts_token="bad", serpapi_api_key=None)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="Unauthorized")))
    app.state.service = FlightDealsService([TravelpayoutsProvider(client, TTLCache(0), s),
                                            SerpApiProvider(client, TTLCache(0), s)])
    with TestClient(app) as c:
        r = c.get("/deals", params={"origin": "MAD"})
    app.state.service = None
    assert r.status_code == 502


def test_token_sent_in_header_not_url(client, rec):
    client.get("/deals", params={"origin": "MAD", "sources": "travelpayouts"})
    tp_calls = [c for c in rec.calls if "prices_for_dates" in str(c.url)]
    assert tp_calls and all("token" not in c.url.params for c in tp_calls)
    assert all(c.headers.get("X-Access-Token") == "tok" for c in tp_calls)


def test_transient_error_is_retried(rec, monkeypatch):
    import app.providers.base as base
    monkeypatch.setattr(base, "RETRY_DELAY_SECONDS", 0)
    hits = {"n": 0}

    def flaky(request):
        if "prices_for_dates" in str(request.url):
            hits["n"] += 1
            if hits["n"] == 1:
                return httpx.Response(503)
        return rec(request)
    s = replace(get_settings(), travelpayouts_token="tok", serpapi_api_key=None)
    client = httpx.AsyncClient(transport=httpx.MockTransport(flaky))
    app.state.service = FlightDealsService([TravelpayoutsProvider(client, TTLCache(0), s),
                                            SerpApiProvider(client, TTLCache(0), s)])
    with TestClient(app) as c:
        r = c.get("/deals", params={"origin": "MAD", "destination": "BCN", "depart_from": D(10)})
    app.state.service = None
    assert r.status_code == 200 and r.json()["count"] >= 1 and hits["n"] == 2


def test_cache_avoids_repeat_calls(client, rec):
    q = {"origin": "MAD", "depart_from": D(9), "depart_to": D(21), "sources": "google_flights"}
    client.get("/deals", params=q)
    n = len(rec.calls)
    client.get("/deals", params=q)
    assert len(rec.calls) == n


# --- Construcción de consultas a los proveedores ---------------------------

def _serp():
    s = replace(get_settings(), serpapi_api_key="k")
    return SerpApiProvider(None, TTLCache(0), s)


def test_serpapi_exact_dates_use_outbound_return():
    qs, _ = _serp().build_queries(SearchParams(origin="MAD", destination="LIS",
                                               depart_from=T + timedelta(5), return_from=T + timedelta(9)))
    assert len(qs) == 1 and qs[0]["outbound_date"] == D(5) and qs[0]["return_date"] == D(9)
    assert qs[0]["type"] == 1 and qs[0]["arrival_id"] == "LIS"


def test_serpapi_range_one_query_per_month_and_6_month_cap():
    qs, warns = _serp().build_queries(SearchParams(origin="MAD", depart_from=T + timedelta(1),
                                                   depart_to=T + timedelta(250), max_price=120))
    assert 6 <= len(qs) <= 7 and all(q["type"] == 2 and q["max_price"] == 120 for q in qs)
    assert warns  # meses más allá de 6 avisados


def test_travelpayouts_too_many_combinations():
    s = replace(get_settings(), travelpayouts_token="t")
    rec = Recorder()
    p = TravelpayoutsProvider(httpx.AsyncClient(transport=httpx.MockTransport(rec)), TTLCache(0), s)
    params = SearchParams(origin="MAD", depart_from=T + timedelta(1), depart_to=T + timedelta(150),
                          return_from=T + timedelta(2), return_to=T + timedelta(200))
    import asyncio
    from app.providers import ProviderError
    with pytest.raises(ProviderError):
        asyncio.run(p.search(params))


# --- Fase 1: búsqueda ampliada (fechas flexibles, orden, billetes sueltos, open-jaw) --------

def tp_client(handler):
    s = replace(get_settings(), travelpayouts_token="tok", serpapi_api_key=None)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.service = FlightDealsService([TravelpayoutsProvider(client, TTLCache(0), s),
                                            SerpApiProvider(client, TTLCache(0), s)])
    return TestClient(app)


def test_flex_days_shift_and_savings(rec):
    with tp_client(rec) as c:
        r = c.get("/deals", params={"origin": "MAD", "destination": "BCN", "depart_from": D(11),
                                    "return_from": D(15), "flex_days": 1})
    app.state.service = None
    res = r.json()["results"]
    assert [d["price"] for d in res] == [35, 80]
    flex, exact = res
    assert (flex["depart_shift"], flex["return_shift"]) == (-1, -1) and flex["savings_vs_exact"] == 45
    assert (exact["depart_shift"], exact["return_shift"]) == (0, 0)


def test_max_shift_total_and_sorting(rec):
    base = {"origin": "MAD", "destination": "BCN", "depart_from": D(11), "return_from": D(15), "flex_days": 1}
    with tp_client(rec) as c:
        assert [d["price"] for d in c.get("/deals", params=base | {"max_shift_total": 1}).json()["results"]] == [80]
        assert c.get("/deals", params=base | {"sort": "closest"}).json()["results"][0]["price"] == 80
        assert c.get("/deals", params=base | {"sort": "score", "shift_penalty": 30}).json()["results"][0]["price"] == 80
        assert c.get("/deals", params=base | {"sort": "score", "shift_penalty": 10}).json()["results"][0]["price"] == 35
    app.state.service = None


def test_flex_requires_dates(client):
    assert client.get("/deals", params={"origin": "MAD", "flex_days": 2}).status_code == 422
    assert client.get("/deals", params={"origin": "MAD", "depart_from": D(5), "return_flex": 2}).status_code == 422
    assert client.get("/deals", params={"origin": "MAD", "depart_from": D(5), "open_jaw": True}).status_code == 422


# Billetes de solo ida simulados: (origen, destino, día, hora, precio, duración min, escalas)
ONE_WAYS = [
    ("MAD", "LIS", 12, 8, 20, 60, 0),
    ("MAD", "LIS", 13, 9, 50, 60, 0),
    ("LIS", "MAD", 19, 18, 25, 60, 0),
    ("OPO", "MAD", 18, 10, 15, 70, 0),
    ("LIS", "MAD", 12, 7, 5, 60, 0),     # sale antes de llegar la ida: nunca debe combinarse
    ("MAD", "ROM", 20, 7, 30, 150, 0),
    ("ROM", "MAD", 25, 20, 30, 150, 0),
]
def _city(code, name, country, lat, lon):
    return {"code": code, "name": name, "country_code": country, "coordinates": {"lat": lat, "lon": lon}}


CITIES = [_city("LIS", "Lisboa", "PT", 38.72, -9.14), _city("OPO", "Oporto", "PT", 41.15, -8.61),
          _city("ROM", "Roma", "IT", 41.89, 12.49), _city("BCN", "Barcelona", "ES", 41.39, 2.17),
          _city("MAD", "Madrid", "ES", 40.42, -3.70)]


def legs_handler(rec):
    def handler(request: httpx.Request):
        url = str(request.url)
        if "cities.json" in url:
            return httpx.Response(200, json=CITIES)
        if "prices_for_dates" in url and request.url.params.get("one_way") == "true":
            o, d = request.url.params.get("origin"), request.url.params.get("destination")
            data = [{"origin": a, "destination": b, "origin_airport": a, "destination_airport": b,
                     "price": p, "airline": "VY", "flight_number": 100 + i, "transfers": s,
                     "departure_at": f"{D(day)}T{hour:02d}:00:00+01:00", "duration_to": dur,
                     "link": f"/search/{a}{b}?t={i}"}
                    for i, (a, b, day, hour, p, dur, s) in enumerate(ONE_WAYS)
                    if (not o or a == o) and (not d or b == d)]
            return httpx.Response(200, json={"success": True, "data": data, "currency": "eur"})
        return rec(request)
    return handler


def test_split_tickets_beat_round_trip(rec):
    with tp_client(legs_handler(rec)) as c:
        r = c.get("/deals", params={"origin": "MAD", "destination": "LIS", "depart_from": D(11),
                                    "depart_to": D(13), "return_from": D(18), "return_to": D(20),
                                    "split_tickets": True})
    app.state.service = None
    res = r.json()["results"]
    best = res[0]
    assert best["kind"] == "split" and best["price"] == 45 and len(best["legs"]) == 2
    assert best["legs"][0]["link"].startswith("https://www.aviasales.com/search/MADLIS")
    assert best["savings_vs_exact"] == 15                     # ida y vuelta normal a LIS: 60
    assert all(d["kind"] != "split" or d["price"] != 25 for d in res)  # vuelta de 5 € anterior a la ida descartada


def test_open_jaw_with_return_origin(rec):
    with tp_client(legs_handler(rec)) as c:
        r = c.get("/deals", params={"origin": "MAD", "destination": "LIS", "depart_from": D(12),
                                    "return_from": D(17), "return_to": D(19), "return_origin": "OPO"})
    app.state.service = None
    res = r.json()["results"]
    assert res and all(d["kind"] == "open_jaw" and d["return_origin"] == "OPO" for d in res)
    assert res[0]["price"] == 35 and res[0]["legs"][1]["origin"] == "OPO"


def test_open_jaw_same_country_auto(rec):
    with tp_client(legs_handler(rec)) as c:
        r = c.get("/deals", params={"origin": "MAD", "depart_from": D(11), "depart_to": D(21),
                                    "return_from": D(17), "return_to": D(26), "open_jaw": True})
    app.state.service = None
    res = r.json()["results"]
    pairs = {(d["destination"], d["return_origin"]) for d in res}
    assert ("LIS", "OPO") in pairs and all(a != b for a, b in pairs)   # Lisboa-Oporto ≈ 275 km
    assert not any(d["destination"] == "ROM" for d in res)   # no hay ciudad a < 600 km de Roma con vuelta


def test_open_jaw_max_km(rec):
    with tp_client(legs_handler(rec)) as c:
        r = c.get("/deals", params={"origin": "MAD", "destination": "LIS", "depart_from": D(12),
                                    "return_from": D(17), "return_to": D(19), "open_jaw": True,
                                    "open_jaw_max_km": 100})
    app.state.service = None
    body = r.json()
    assert body["count"] == 0 and "100 km" in body["warnings"][0]


# --- Fase 2: /calendar ------------------------------------------------------------------

def test_calendar_one_way(rec):
    with tp_client(rec) as c:
        body = c.get("/calendar", params={"origin": "MAD", "destination": "BCN", "depart_from": D(9),
                                          "depart_to": D(12), "trip_type": "one_way"}).json()
    app.state.service = None
    assert body["trip"] == "one_way" and body["grid"] == {D(10): 35, D(11): 80}
    assert body["cheapest"]["price"] == 35 and body["stats"] == {"cells": 2, "min": 35, "max": 80, "avg": 57.5}
    assert [c["price_level"] for c in body["cells"]] == ["low", "high"]


def test_calendar_round_trip_grid_cheapest_per_cell(rec):
    with tp_client(rec) as c:
        body = c.get("/calendar", params={"origin": "MAD", "depart_from": D(9), "depart_to": D(21),
                                          "return_from": D(13), "return_to": D(26)}).json()
    app.state.service = None
    assert body["trip"] == "round_trip"
    assert body["grid"][D(10)] == {D(14): 35} and body["grid"][D(12)] == {D(19): 60}
    assert body["cheapest"]["destination"] == "BCN"


def test_calendar_requires_depart_dates(client):
    r = client.get("/calendar", params={"origin": "MAD"})
    assert r.status_code == 422 and "fechas de ida" in r.text


# --- Fase 3: modo avanzado (dos vuelos enlazados por cuenta propia) ------------------------

# (origen, destino, día, hora, precio, duración, escalas, aeropuerto salida, aeropuerto llegada)
ADV_LEGS = [
    ("STO", "BCN", 20, 7, 300, 240, 0, "ARN", "BCN"),    # directo caro: referencia
    ("STO", "MIL", 20, 6, 40, 180, 0, "ARN", "MXP"),     # llega 09:00
    ("MIL", "BCN", 20, 10, 10, 95, 0, "MXP", "BCN"),     # 1 h de margen: no da tiempo
    ("MIL", "BCN", 20, 15, 30, 95, 0, "BGY", "BCN"),     # 6 h, cambio de aeropuerto
    ("STO", "BER", 20, 8, 20, 120, 0, "ARN", "BER"),     # llega 10:00
    ("BER", "BCN", 22, 9, 25, 150, 0, "BER", "BCN"),     # 2 noches en Berlín
]


def adv_handler(rec):
    def handler(request: httpx.Request):
        url = str(request.url)
        if "cities.json" in url:
            return httpx.Response(200, json=CITIES)
        if "prices_for_dates" in url:
            o, d = request.url.params.get("origin"), request.url.params.get("destination")
            data = [{"origin": a, "destination": b, "origin_airport": oa, "destination_airport": da,
                     "price": p, "airline": f"A{i}", "flight_number": 100 + i, "transfers": s,
                     "departure_at": f"{D(day)}T{hour:02d}:00:00+01:00", "duration_to": dur,
                     "link": f"/search/{a}{b}?t={i}"}
                    for i, (a, b, day, hour, p, dur, s, oa, da) in enumerate(ADV_LEGS)
                    if (not o or a == o) and (not d or b == d)]
            return httpx.Response(200, json={"success": True, "data": data, "currency": "eur"})
        return rec(request)
    return handler


ADV = {"origin": "STO", "destination": "BCN", "depart_from": D(20), "mode": "advanced"}


def test_advanced_links_two_flights(rec):
    with tp_client(adv_handler(rec)) as c:
        body = c.get("/deals", params=ADV).json()
    app.state.service = None
    res = body["results"]
    best = res[0]
    assert best["kind"] == "self_transfer" and best["layover_city"] == "MIL" and best["price"] == 70
    assert best["layover_hours"] == 6.0 and best["airport_change"] is True and best["stopover_type"] == "connection"
    assert best["savings_vs_exact"] == 230 and len(best["legs"]) == 2
    assert not any(d.get("layover_city") == "MIL" and d["price"] == 50 for d in res)   # 1 h: descartado
    assert not any(d.get("layover_city") == "BER" for d in res)                        # 2 noches > 1
    assert any(d["kind"] == "one_way" and d["price"] == 300 for d in res)              # el directo sigue
    assert "protección" in body["warnings"][0]


def test_advanced_layover_days_and_stopover_cost(rec):
    with tp_client(adv_handler(rec)) as c:
        body = c.get("/deals", params=ADV | {"max_layover_days": 2, "stopover_cost_per_night": 40}).json()
    app.state.service = None
    ber = next(d for d in body["results"] if d.get("layover_city") == "BER")
    assert ber["price"] == 45 and ber["estimated_total"] == 125 and ber["stopover_type"] == "stopover"
    assert body["results"][0]["layover_city"] == "MIL"    # 70 < 125 al contar el alojamiento


def test_advanced_no_airport_change_and_validation(rec, client):
    with tp_client(adv_handler(rec)) as c:
        res = c.get("/deals", params=ADV | {"allow_airport_change": False}).json()["results"]
    app.state.service = None
    assert all(d["kind"] != "self_transfer" for d in res)
    assert client.get("/deals", params={"origin": "STO", "depart_from": D(20), "mode": "advanced"}).status_code == 422
    assert client.get("/deals", params=ADV | {"return_from": D(25)}).status_code == 422
