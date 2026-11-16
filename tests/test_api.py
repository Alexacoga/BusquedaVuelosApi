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
