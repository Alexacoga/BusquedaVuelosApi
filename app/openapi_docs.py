"""Textos y ejemplos del contrato OpenAPI (descripciones, llamadas y respuestas de ejemplo).

Los precios de los ejemplos salen de búsquedas reales (noviembre de 2026); enlaces, horas de algunos
tramos y el calendario de solo ida son ilustrativos y están recortados para que se lean bien. Los
precios reales cambian a diario. Aquí no debe haber nunca tokens ni datos personales: este texto se publica
en /docs, /openapi.json y docs/openapi.json.
"""
from __future__ import annotations

VERSION = "1.2.0"
DATA_SOURCE_URL = "https://support.travelpayouts.com/hc/en-us/articles/203956163"

API_DESCRIPTION = f"""
Busca **ofertas de vuelos baratos** desde un origen a un destino concreto o **a cualquier destino**,
con rangos de fechas, filtros de precio, noches y escalas.

### De dónde salen los datos
Esta API **no vende billetes ni consulta aerolíneas directamente**. Recupera los precios de la
**[Aviasales Data API de Travelpayouts]({DATA_SOURCE_URL})** (gratuita, con token), que publica los
precios más baratos que han encontrado otros usuarios de Aviasales en los últimos días. Por eso:

- Son precios **orientativos** (caché de 2-7 días en origen): confirma siempre en el enlace `link`.
- Rutas poco buscadas pueden no tener datos; en ese caso se proponen `alternatives`.
- Opcionalmente puede usar SerpApi (Google Travel Explore) si se configura `SERPAPI_API_KEY`.

### Qué aporta frente a llamar directamente a Travelpayouts
- **Una sola llamada** en lugar de una por mes y combinación de fechas: rangos de días, `flex_days`
  (± días) y ordenación por precio, € por noche, cercanía a tus fechas o duración.
- **Combinaciones que Travelpayouts no da hechas**: dos billetes de solo ida (`split_tickets`),
  volver desde otra ciudad (`open_jaw`, `return_origin`) y enlazar dos vuelos por tu cuenta
  (`mode=advanced`, A→X→B, también en ida y vuelta) con márgenes de conexión y noches de escala.
- **Calendario de precios** (`/calendar`) para ver qué días salen más baratos.
- **Alternativas** cuando no hay resultados: fechas cercanas y ciudades cercanas.
- **El token no sale nunca**: viaja en cabecera hacia Travelpayouts y no aparece en respuestas,
  URLs ni logs. Caché en memoria (6 h), reintentos y límite de llamadas simultáneas.
- Nombres de ciudad, país y distancias entre ciudades a partir de los datos públicos de Travelpayouts.

### Endpoints
| Endpoint | Para qué |
|---|---|
| `GET /` | Información de la API en inglés (*API overview in English*) |
| `GET /health` | Estado y fuentes de datos activas |
| `GET /deals` | **Lista de ofertas** ordenadas: cada una con fechas, precio y enlace de reserva |
| `GET /calendar` | **Mapa de precios por día**: el más barato de cada fecha de ida (y vuelta) |

`/deals` y `/calendar` aceptan **los mismos parámetros** y hacen la misma búsqueda; cambian en cómo
devuelven el resultado (ver la descripción de cada uno).

### Códigos IATA
Usa códigos de **ciudad** (`LON`, `PAR`, `MIL`, `ROM`, `STO`) para incluir todos sus aeropuertos, o
de aeropuerto (`BCN`, `MAD`, `LIS`). Las fechas van en formato `YYYY-MM-DD` y no pueden haber pasado.
"""

TAGS = [
    {"name": "Ofertas", "description": "Búsqueda de vuelos: lista de ofertas o calendario de precios"},
    {"name": "Información", "description": "Descripción de la API y estado del servicio"},
]

# --- GET / (en inglés, para cualquiera que llegue a la API) -----------------------------------

ROOT_INFO = {
    "name": "Flight Deals API",
    "version": VERSION,
    "description": (
        "Finds cheap flight deals from an origin to a given destination or to anywhere, with date "
        "ranges, flexible dates and price, stay and stops filters. It does not sell tickets: prices come "
        "from the Travelpayouts / Aviasales Data API (cheapest fares found by Aviasales users in the "
        "last few days), so they are indicative and must be confirmed through each result's booking link."
    ),
    "data_source": {"name": "Travelpayouts / Aviasales Data API", "docs": DATA_SOURCE_URL},
    "what_it_adds": [
        "One request instead of one per month and date combination; date ranges and +/- flexible days",
        "Sorting by price, price per night, closeness to your dates, score or duration",
        "Split tickets (two one-way tickets) and open-jaw trips (return from another city)",
        "Advanced mode: self-transfer A->X->B with connection margins and layover nights, one-way or round trip",
        "Price calendar to see the cheapest days",
        "Alternatives (nearby dates and cities) when a search has no results",
        "The provider token never leaves the server; in-memory cache, retries and concurrency limits",
    ],
    "endpoints": {
        "GET /": "This overview",
        "GET /health": "Service status and active data sources",
        "GET /deals": "List of deals sorted by `sort`, each with dates, price and booking link",
        "GET /calendar": "Cheapest price per departure (and return) date, as cells and a grid",
    },
    "examples": [
        "/deals?origin=BCN&depart_from=2027-03-20&return_from=2027-03-29&flex_days=2&group_by_destination=true",
        "/deals?origin=BCN&destination=LIS&depart_from=2027-03-20&return_from=2027-03-29&split_tickets=true",
        "/deals?origin=STO&destination=BCN&depart_from=2027-03-20&mode=advanced",
        "/calendar?origin=BCN&destination=LIS&depart_from=2027-03-18&depart_to=2027-03-22"
        "&return_from=2027-03-27&return_to=2027-03-31",
    ],
    "docs": {"swagger_ui": "/docs", "redoc": "/redoc", "openapi": "/openapi.json"},
    "notes": [
        "Use city IATA codes (LON, PAR, MIL) to include every airport, or airport codes (BCN, LIS)",
        "Dates are YYYY-MM-DD and cannot be in the past",
        "Parameter and field descriptions in /docs are in Spanish",
    ],
}

ROOT_EXAMPLE = {**ROOT_INFO, "providers": ["travelpayouts"]}

# --- GET /health -----------------------------------------------------------------------------

HEALTH_DESCRIPTION = "Devuelve `ok` y las fuentes de datos configuradas (p. ej. `travelpayouts`)."

# --- GET /deals ------------------------------------------------------------------------------

DEALS_DESCRIPTION = """
Devuelve una **lista de ofertas** (`results`), ordenada según `sort` y recortada a `limit`. Cada
oferta es un viaje concreto: fechas, precio total, aerolínea, escalas y **enlace de reserva**.

**Úsalo para** encontrar adónde ir o el viaje más barato y reservarlo.
**Diferencia con `/calendar`:** aquí puede haber varias ofertas para el mismo día, se pueden agrupar
por destino (`group_by_destination`) y, si no hay resultados, se proponen `alternatives`.

Tipos de oferta (`kind`): `one_way`, `round_trip`, `split` (dos billetes de solo ida), `open_jaw`
(vuelta desde otra ciudad) y `self_transfer` (vuelos enlazados por tu cuenta, `mode=advanced`). Las
combinaciones traen cada billete en `legs` con su propio enlace.

### Llamadas de ejemplo
| Qué busca | Llamada |
|---|---|
| Cualquier destino, lo más barato por destino | `/deals?origin=BCN&depart_from=2027-03-18&depart_to=2027-03-22&return_from=2027-03-27&return_to=2027-03-31&group_by_destination=true` |
| Ruta concreta comparando billetes sueltos | `/deals?origin=BCN&destination=LIS&depart_from=2027-03-20&depart_to=2027-03-22&return_from=2027-03-27&return_to=2027-03-29&split_tickets=true` |
| Fechas ±2 días, más noches por euro | `/deals?origin=BCN&depart_from=2027-03-20&return_from=2027-03-29&flex_days=2&sort=price_per_night` |
| Ida a Lisboa y vuelta desde Oporto | `/deals?origin=BCN&destination=LIS&depart_from=2027-03-20&return_from=2027-03-29&flex_days=2&return_origin=OPO` |
| Escapada de 2-4 noches, solo directos, < 100 € | `/deals?origin=MAD&depart_from=2027-01-08&depart_to=2027-01-30&min_nights=2&max_nights=4&direct_only=true&max_price=100` |
| Enlazar dos vuelos (Estocolmo → ? → Barcelona) | `/deals?origin=STO&destination=BCN&depart_from=2027-03-20&mode=advanced&max_layover_days=1` |

> `mode=advanced` combina billetes independientes: si pierdes el segundo vuelo no hay protección.
> La respuesta lo avisa en `warnings`.
"""

_LINK = "https://www.aviasales.com/search/BCN2203LIS27031?t=U2...&expected_price=95"

DEALS_EXAMPLES = {
    "round_trip": {
        "summary": "Ruta concreta: BCN ⇄ LIS",
        "description": "`/deals?origin=BCN&destination=LIS&depart_from=2027-03-20&depart_to=2027-03-22"
                       "&return_from=2027-03-27&return_to=2027-03-29&limit=2`",
        "value": {
            "query": {"origin": "BCN", "destination": "LIS", "depart_from": "2027-03-20",
                      "depart_to": "2027-03-22", "return_from": "2027-03-27", "return_to": "2027-03-29",
                      "limit": 2, "sort": "price", "currency": "EUR"},
            "count": 2, "sources_used": ["travelpayouts"], "warnings": [],
            "results": [
                {"source": "travelpayouts", "kind": "round_trip", "origin": "BCN", "destination": "LIS",
                 "destination_name": "Lisboa", "destination_country": "PT", "departure_date": "2027-03-22",
                 "return_date": "2027-03-27", "nights": 5, "price": 95.0, "currency": "EUR",
                 "airline": "U2", "flight_number": "U27682", "stops": 0, "return_stops": 0,
                 "duration_minutes": 125, "link": _LINK},
                {"source": "travelpayouts", "kind": "round_trip", "origin": "BCN", "destination": "LIS",
                 "destination_name": "Lisboa", "destination_country": "PT", "departure_date": "2027-03-20",
                 "return_date": "2027-03-27", "nights": 7, "price": 99.0, "currency": "EUR",
                 "airline": "FR", "flight_number": "FR3078", "stops": 0, "return_stops": 0,
                 "duration_minutes": 125,
                 "link": "https://www.aviasales.com/search/BCN2003LIS27031?t=FR...&expected_price=99"},
            ],
            "alternatives": [],
        },
    },
    "open_jaw": {
        "summary": "Billetes sueltos: ida a Lisboa, vuelta desde Oporto",
        "description": "`/deals?origin=BCN&destination=LIS&depart_from=2027-03-20&return_from=2027-03-29"
                       "&flex_days=2&return_origin=OPO&limit=1`",
        "value": {
            "query": {"origin": "BCN", "destination": "LIS", "depart_from": "2027-03-20",
                      "return_from": "2027-03-29", "flex_days": 2, "return_origin": "OPO", "limit": 1},
            "count": 1, "sources_used": ["travelpayouts"], "warnings": [],
            "results": [{
                "source": "travelpayouts", "kind": "open_jaw", "origin": "BCN", "destination": "LIS",
                "destination_name": "Lisboa", "destination_country": "PT", "departure_date": "2027-03-20",
                "return_date": "2027-03-27", "nights": 7, "price": 51.0, "currency": "EUR",
                "airline": "FR", "stops": 0, "return_stops": 0, "depart_shift": 0, "return_shift": -2,
                "savings_vs_exact": 44.0, "return_origin": "OPO",
                "legs": [
                    {"source": "travelpayouts", "origin": "BCN", "origin_airport": "BCN", "destination": "LIS",
                     "destination_airport": "LIS", "departure_at": "2027-03-20T20:30:00+01:00",
                     "arrival_at": "2027-03-20T22:35:00+01:00", "departure_date": "2027-03-20",
                     "price": 27.0, "currency": "EUR", "airline": "FR", "stops": 0, "duration_minutes": 125,
                     "link": "https://www.aviasales.com/search/BCN2003LIS1?t=FR..."},
                    {"source": "travelpayouts", "origin": "OPO", "origin_airport": "OPO", "destination": "BCN",
                     "destination_airport": "BCN", "departure_at": "2027-03-27T06:00:00+00:00",
                     "arrival_at": "2027-03-27T07:55:00+00:00", "departure_date": "2027-03-27",
                     "price": 24.0, "currency": "EUR", "airline": "FR", "stops": 0, "duration_minutes": 115,
                     "link": "https://www.aviasales.com/search/OPO2703BCN1?t=FR..."},
                ],
                "notes": ["Dos billetes separados: resérvalos por separado con el enlace de cada tramo"],
            }],
            "alternatives": [],
        },
    },
    "self_transfer": {
        "summary": "Modo avanzado: Estocolmo → París → Barcelona",
        "description": "`/deals?origin=STO&destination=BCN&depart_from=2027-03-20&mode=advanced&limit=1`",
        "value": {
            "query": {"origin": "STO", "destination": "BCN", "depart_from": "2027-03-20", "mode": "advanced",
                      "limit": 1},
            "count": 1, "sources_used": ["travelpayouts"],
            "warnings": ["Modo avanzado: las combinaciones self_transfer son dos billetes independientes. "
                         "Si el primer vuelo se retrasa y pierdes el segundo, no hay protección ni "
                         "compensación; …"],
            "results": [{
                "source": "travelpayouts", "kind": "self_transfer", "origin": "STO", "destination": "BCN",
                "destination_name": "Barcelona", "destination_country": "ES", "departure_date": "2027-03-20",
                "price": 103.0, "currency": "EUR", "airline": "FR+TO", "stops": 1, "duration_minutes": 1265,
                "layover_city": "PAR", "layover_hours": 17.2, "airport_change": False,
                "stopover_type": "overnight",
                "legs": [
                    {"source": "travelpayouts", "origin": "STO", "origin_airport": "ARN", "destination": "PAR",
                     "destination_airport": "ORY", "departure_at": "2027-03-20T06:45:00+01:00",
                     "arrival_at": "2027-03-20T09:40:00+01:00", "departure_date": "2027-03-20",
                     "price": 71.0, "currency": "EUR", "airline": "TO", "stops": 0,
                     "link": "https://www.aviasales.com/search/STO2003PAR1?t=TO..."},
                    {"source": "travelpayouts", "origin": "PAR", "origin_airport": "ORY", "destination": "BCN",
                     "destination_airport": "BCN", "departure_at": "2027-03-21T02:50:00+01:00",
                     "arrival_at": "2027-03-21T04:50:00+01:00", "departure_date": "2027-03-21",
                     "price": 32.0, "currency": "EUR", "airline": "FR", "stops": 0,
                     "link": "https://www.aviasales.com/search/PAR2103BCN1?t=FR..."},
                ],
                "notes": ["Dos billetes separados: resérvalos por separado con el enlace de cada tramo",
                          "Conexión por tu cuenta en PAR: 17.2 h", "1 noche(s) en PAR"],
            }],
            "alternatives": [],
        },
    },
    "alternatives": {
        "summary": "Sin resultados: alternativas cercanas",
        "description": "`/deals?origin=BCN&destination=OPO&depart_from=2027-03-20&return_from=2027-03-29&limit=2`",
        "value": {
            "query": {"origin": "BCN", "destination": "OPO", "depart_from": "2027-03-20",
                      "return_from": "2027-03-29", "limit": 2},
            "count": 0, "sources_used": ["travelpayouts"],
            "warnings": ["Sin resultados con los criterios exactos; 2 alternativas cercanas en `alternatives`"],
            "results": [],
            "alternatives": [
                {"source": "travelpayouts", "kind": "round_trip", "origin": "BCN", "destination": "LIS",
                 "destination_name": "Lisboa", "destination_country": "PT", "departure_date": "2027-03-22",
                 "return_date": "2027-03-26", "nights": 4, "price": 91.0, "currency": "EUR",
                 "depart_shift": 2, "return_shift": -3, "link": "https://www.aviasales.com/search/...",
                 "alternative": "destino cercano: LIS a 274 km de OPO"},
                {"source": "travelpayouts", "kind": "round_trip", "origin": "BCN", "destination": "OPO",
                 "destination_name": "Oporto", "destination_country": "PT", "departure_date": "2027-03-20",
                 "return_date": "2027-03-26", "nights": 6, "price": 116.0, "currency": "EUR",
                 "depart_shift": 0, "return_shift": -3, "link": "https://www.aviasales.com/search/...",
                 "alternative": "fechas cercanas (±7 días)"},
            ],
        },
    },
}

# --- GET /calendar ---------------------------------------------------------------------------

CALENDAR_DESCRIPTION = """
Devuelve un **mapa de precios por día**: para cada fecha de ida (y de vuelta, si es ida y vuelta)
el precio **más barato** encontrado, con su nivel (`low` / `mid` / `high`, por tercios) para pintarlo
como mapa de calor. Incluye `stats` (mínimo, máximo, media) y `cheapest` (la mejor celda).

**Úsalo para** elegir fechas cuando el destino ya está decidido o para ver cómo varía el precio.

**Diferencias con `/deals`:**
- Acepta los mismos parámetros y hace la misma búsqueda (incluidos `flex_days`, `split_tickets`,
  `open_jaw` y `mode=advanced`), pero devuelve **una sola celda por fecha** (o par de fechas).
- **Necesita fechas de ida** (`depart_from` y/o `depart_to`), si no responde `422`.
- No usa `limit`, `sort` ni `group_by_destination`, y no propone `alternatives`.
- `grid` da la misma información compacta: `{ida: precio}` en solo ida o `{ida: {vuelta: precio}}`
  en ida y vuelta.

### Llamadas de ejemplo
| Qué busca | Llamada |
|---|---|
| Ida y vuelta BCN ⇄ LIS, qué combinación de días sale mejor | `/calendar?origin=BCN&destination=LIS&depart_from=2027-03-20&depart_to=2027-03-22&return_from=2027-03-27&return_to=2027-03-28` |
| Solo ida a Londres durante un mes | `/calendar?origin=MAD&destination=LON&depart_from=2027-02-01&depart_to=2027-02-28&trip_type=one_way` |
| Cualquier destino: el más barato de cada día | `/calendar?origin=BCN&depart_from=2027-03-01&depart_to=2027-03-15&trip_type=one_way` |
"""

_CELL = {"destination": "LIS", "destination_name": "Lisboa", "kind": "round_trip", "stops": 0}

CALENDAR_EXAMPLES = {
    "round_trip": {
        "summary": "Ida y vuelta BCN ⇄ LIS",
        "description": "`/calendar?origin=BCN&destination=LIS&depart_from=2027-03-20&depart_to=2027-03-22"
                       "&return_from=2027-03-27&return_to=2027-03-28`",
        "value": {
            "query": {"origin": "BCN", "destination": "LIS", "depart_from": "2027-03-20",
                      "depart_to": "2027-03-22", "return_from": "2027-03-27", "return_to": "2027-03-28"},
            "trip": "round_trip", "currency": "EUR", "sources_used": ["travelpayouts"], "warnings": [],
            "stats": {"cells": 3, "min": 95.0, "max": 116.0, "avg": 103.33},
            "cheapest": {"departure_date": "2027-03-22", "return_date": "2027-03-27", "nights": 5,
                         "price": 95.0, "price_level": "low", "airline": "U2", **_CELL, "link": _LINK},
            "cells": [
                {"departure_date": "2027-03-20", "return_date": "2027-03-27", "nights": 7, "price": 99.0,
                 "price_level": "mid", "airline": "FR", **_CELL,
                 "link": "https://www.aviasales.com/search/BCN2003LIS27031?t=FR..."},
                {"departure_date": "2027-03-22", "return_date": "2027-03-27", "nights": 5, "price": 95.0,
                 "price_level": "low", "airline": "U2", **_CELL, "link": _LINK},
                {"departure_date": "2027-03-22", "return_date": "2027-03-28", "nights": 6, "price": 116.0,
                 "price_level": "high", "airline": "TP", **_CELL,
                 "link": "https://www.aviasales.com/search/BCN2203LIS28031?t=TP..."},
            ],
            "grid": {"2027-03-20": {"2027-03-27": 99.0},
                     "2027-03-22": {"2027-03-27": 95.0, "2027-03-28": 116.0}},
        },
    },
    "one_way": {
        "summary": "Solo ida: grid de fecha → precio (valores ilustrativos)",
        "description": "`/calendar?origin=MAD&destination=BCN&depart_from=2027-02-01&depart_to=2027-02-03"
                       "&trip_type=one_way`",
        "value": {
            "query": {"origin": "MAD", "destination": "BCN", "depart_from": "2027-02-01",
                      "depart_to": "2027-02-03", "trip_type": "one_way"},
            "trip": "one_way", "currency": "EUR", "sources_used": ["travelpayouts"], "warnings": [],
            "stats": {"cells": 3, "min": 19.0, "max": 42.0, "avg": 29.0},
            "cheapest": {"departure_date": "2027-02-02", "price": 19.0, "price_level": "low",
                         "destination": "BCN", "destination_name": "Barcelona", "kind": "one_way",
                         "airline": "VY", "stops": 0, "link": "https://www.aviasales.com/search/MAD0202BCN1?t=VY..."},
            "cells": [
                {"departure_date": d, "price": p, "price_level": lvl, "destination": "BCN",
                 "destination_name": "Barcelona", "kind": "one_way", "airline": "VY", "stops": 0,
                 "link": f"https://www.aviasales.com/search/MAD{d[8:]}02BCN1?t=VY..."}
                for d, p, lvl in (("2027-02-01", 26.0, "mid"), ("2027-02-02", 19.0, "low"),
                                  ("2027-02-03", 42.0, "high"))
            ],
            "grid": {"2027-02-01": 26.0, "2027-02-02": 19.0, "2027-02-03": 42.0},
        },
    },
}


def ok_examples(examples: dict) -> dict:
    """Bloque `responses[200]` con ejemplos con nombre (Swagger los muestra en un desplegable)."""
    return {"content": {"application/json": {"examples": examples}}}
