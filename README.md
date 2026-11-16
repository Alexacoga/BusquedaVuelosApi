# BusquedaVuelosApi ✈️

API REST (FastAPI) que busca **ofertas de vuelos desde un origen a cualquier destino**
(o a uno concreto), con rangos opcionales de fechas de ida y/o vuelta, rango de precio,
duración de la estancia y solo vuelos directos.

Combina dos fuentes de datos y devuelve los resultados unificados y ordenados por precio:

| Fuente | Qué es | Coste | Notas |
|---|---|---|---|
| `travelpayouts` | Aviasales Data API | Gratis | Precios cacheados (encontrados por usuarios en los últimos 2-7 días). Ideal para "a cualquier sitio". Rangos de fechas por meses. |
| `google_flights` | SerpApi → Google Travel Explore (datos de Google Flights) | 250 búsquedas/mes gratis | Precios de Google. Solo los próximos 6 meses. 1 consulta por mes del rango de ida. |

Basta con configurar **una** de las dos. Si hay ambas, se consultan en paralelo; si una falla,
la respuesta sigue con la otra y lo indica en `warnings`.

> Google no tiene API pública de vuelos (QPX Express cerró en 2018) y Amadeus cerró su
> portal gratuito *Self-Service* el 17/07/2026, por eso se usan estas dos.

## Puesta en marcha

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # y pon tu TRAVELPAYOUTS_TOKEN y/o SERPAPI_API_KEY
uvicorn app.main:app --reload
```

Documentación interactiva (para probar desde el navegador): <http://localhost:8000/docs>

Con Docker:

```bash
docker build -t flight-deals .
docker run -p 8000:8000 --env-file .env flight-deals
```

### Conseguir las claves
- **Travelpayouts**: regístrate en travelpayouts.com, únete al programa *Aviasales* y copia el token
  en *Herramientas → API*. El acceso a la Data API es inmediato.
- **SerpApi**: regístrate en serpapi.com; la clave está en el panel (plan gratuito de 250 búsquedas/mes).

## Uso: `GET /deals`

| Parámetro | Obligatorio | Descripción |
|---|---|---|
| `origin` | ✅ | IATA de salida (ciudad o aeropuerto): `MAD`, `BCN`, `SVQ`… |
| `destination` | | IATA de destino. **Vacío = cualquier destino** |
| `depart_from`, `depart_to` | | Rango de fechas de ida (`YYYY-MM-DD`). Solo `depart_from` = ese día exacto. Solo `depart_to` = desde hoy hasta esa fecha |
| `return_from`, `return_to` | | Rango de vuelta. Si se indica, la búsqueda es de ida y vuelta |
| `trip_type` | | `auto` (por defecto), `one_way`, `round_trip` |
| `min_price`, `max_price` | | Rango de precio (total del billete) |
| `min_nights`, `max_nights` | | Duración de la estancia (implica ida y vuelta) |
| `direct_only` | | `true` = solo vuelos sin escalas |
| `currency` | | `EUR` por defecto |
| `sources` | | `travelpayouts` y/o `google_flights` (repetible). Vacío = todas las configuradas |
| `group_by_destination` | | `true` = solo la oferta más barata de cada destino |
| `limit` | | Máx. resultados (1-500, defecto 50) |

### Ejemplos

```bash
# Lo más barato desde Madrid a cualquier sitio, saliendo en diciembre, por menos de 80 €
curl "localhost:8000/deals?origin=MAD&depart_from=2026-12-01&depart_to=2026-12-31&max_price=80&group_by_destination=true"

# Escapada desde Barcelona: ida un viernes de enero, 2-4 noches, directo
curl "localhost:8000/deals?origin=BCN&depart_from=2027-01-08&depart_to=2027-01-30&min_nights=2&max_nights=4&direct_only=true"

# Madrid → Lisboa con rangos de ida y vuelta
curl "localhost:8000/deals?origin=MAD&destination=LIS&depart_from=2027-02-10&depart_to=2027-02-15&return_from=2027-02-16&return_to=2027-02-22"

# Solo ida, solo Google Flights
curl "localhost:8000/deals?origin=VLC&depart_from=2027-03-01&depart_to=2027-03-20&trip_type=one_way&sources=google_flights"
```

### Respuesta

```json
{
  "query": { "origin": "MAD", "depart_from": "2026-12-01", "...": "..." },
  "count": 2,
  "sources_used": ["travelpayouts", "google_flights"],
  "warnings": [],
  "results": [
    {
      "source": "travelpayouts",
      "origin": "MAD", "destination": "BCN", "destination_name": "Barcelona", "destination_country": "ES",
      "departure_date": "2026-12-05", "return_date": "2026-12-08", "nights": 3,
      "price": 35.0, "currency": "EUR",
      "airline": "VY", "flight_number": "VY1002", "stops": 0, "return_stops": 0,
      "duration_minutes": 75,
      "link": "https://www.aviasales.com/search/MAD0512BCN08121"
    }
  ]
}
```

Códigos de error: `422` parámetros no válidos (con el motivo), `503` ninguna fuente configurada,
`502` todas las fuentes fallaron.

## Cosas a tener en cuenta
- **Los precios son orientativos**: Travelpayouts usa caché de búsquedas recientes y Google Explore
  muestra el "desde". Confirma siempre en el enlace (`link`) antes de comprar.
- **Cuota de SerpApi**: cada mes del rango de ida cuesta 1 búsqueda. Las respuestas se guardan en
  caché 6 h (`CACHE_TTL_SECONDS`) para no repetir consultas.
- **Travelpayouts** limita a 12 combinaciones mes-ida × mes-vuelta por búsqueda; si te pasas, acorta el rango.
- Usa códigos IATA de **ciudad** (`LON`, `PAR`, `MIL`) para cubrir todos sus aeropuertos en Travelpayouts.

## Tests

```bash
pytest -q
```

Usan respuestas simuladas, así que no gastan cuota ni necesitan claves.

## Estructura

```
app/
  main.py             # endpoints FastAPI
  models.py           # parámetros, validación y modelo de oferta
  service.py          # combina fuentes, filtra, deduplica y ordena
  cache.py            # caché en memoria con caducidad
  providers/
    travelpayouts.py  # Aviasales Data API
    serpapi.py        # Google Travel Explore vía SerpApi
tests/test_api.py
```

Para añadir otra fuente (p. ej. Duffel, Kiwi, un scraper de Google Flights como `fast-flights`),
crea una clase que herede de `Provider` en `app/providers/` y añádela en `build_service()` de `main.py`.
