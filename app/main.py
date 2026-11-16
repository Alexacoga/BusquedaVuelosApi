"""API de ofertas de vuelos.

Arranque:  uvicorn app.main:app --reload
Docs interactivas: http://localhost:8000/docs
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import ValidationError

from app.cache import TTLCache
from app.config import get_settings
from app.models import SearchParams, SearchResponse, TripType
from app.providers import SerpApiProvider, TravelpayoutsProvider
from app.service import FlightDealsService, NoProvidersError


def build_service(client: httpx.AsyncClient) -> FlightDealsService:
    s = get_settings()
    cache = TTLCache(s.cache_ttl_seconds)
    return FlightDealsService([
        TravelpayoutsProvider(client, cache, s),
        SerpApiProvider(client, cache, s),
    ])


@asynccontextmanager
async def lifespan(app: FastAPI):
    if getattr(app.state, "service", None) is None:  # los tests inyectan el suyo
        client = httpx.AsyncClient(timeout=get_settings().http_timeout_seconds)
        app.state.service = build_service(client)
        try:
            yield
        finally:
            await client.aclose()
    else:
        yield


app = FastAPI(
    title="Flight Deals API",
    version="1.0.0",
    description="Busca ofertas de vuelos desde un origen a cualquier destino, con rangos de fechas y precio.",
    lifespan=lifespan,
)


def get_service(request: Request) -> FlightDealsService:
    return request.app.state.service


@app.get("/health")
def health(service: Annotated[FlightDealsService, Depends(get_service)]):
    return {"status": "ok", "providers": service.available()}


@app.get("/deals", response_model=SearchResponse, response_model_exclude_none=True)
async def deals(
    service: Annotated[FlightDealsService, Depends(get_service)],
    origin: Annotated[str, Query(description="IATA de salida (ciudad o aeropuerto), p. ej. MAD")],
    destination: Annotated[str | None, Query(description="IATA de destino. Vacío = cualquier destino")] = None,
    depart_from: Annotated[date | None, Query(description="Ida desde (YYYY-MM-DD). Solo esta = día exacto")] = None,
    depart_to: Annotated[date | None, Query(description="Ida hasta (YYYY-MM-DD)")] = None,
    return_from: Annotated[date | None, Query(description="Vuelta desde. Si hay vuelta, se busca ida y vuelta")] = None,
    return_to: Annotated[date | None, Query(description="Vuelta hasta")] = None,
    trip_type: Annotated[TripType, Query(description="auto | one_way | round_trip")] = TripType.auto,
    min_price: Annotated[float | None, Query(ge=0)] = None,
    max_price: Annotated[float | None, Query(ge=0)] = None,
    min_nights: Annotated[int | None, Query(ge=0, description="Estancia mínima (ida y vuelta)")] = None,
    max_nights: Annotated[int | None, Query(ge=0, description="Estancia máxima (ida y vuelta)")] = None,
    direct_only: bool = False,
    currency: str | None = None,
    sources: Annotated[list[str] | None, Query(description="travelpayouts y/o google_flights. Vacío = todos")] = None,
    group_by_destination: Annotated[bool, Query(description="Solo la oferta más barata por destino")] = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
):
    try:
        params = SearchParams(
            origin=origin, destination=destination,
            depart_from=depart_from, depart_to=depart_to,
            return_from=return_from, return_to=return_to,
            trip_type=trip_type, min_price=min_price, max_price=max_price,
            min_nights=min_nights, max_nights=max_nights, direct_only=direct_only,
            currency=currency or get_settings().default_currency,
            sources=sources, group_by_destination=group_by_destination, limit=limit,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=[
            {"field": ".".join(map(str, e["loc"])) or "query", "error": e["msg"].removeprefix("Value error, ")}
            for e in exc.errors()
        ])

    try:
        result = await service.search(params)
    except NoProvidersError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not result.sources_used and result.warnings:
        raise HTTPException(status_code=502, detail=result.warnings)
    return result
