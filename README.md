# BusquedaVuelosApi ✈️

API REST en Python (FastAPI) que busca **ofertas de vuelos desde un origen a cualquier destino**
(o a uno concreto), con rangos opcionales de fechas de ida y vuelta, precio, noches de estancia
y solo vuelos directos. Devuelve los resultados unificados y ordenados por precio.

---

## 1. Qué hace y por qué existe

Los buscadores de vuelos (Google Flights, Skyscanner…) están pensados para una ruta concreta.
Esta API resuelve otra pregunta: **"¿adónde puedo volar barato desde X en estas fechas?"**,
devolviendo una lista de destinos ordenada por precio y filtrable, lista para consumir desde
un script, una app o un sistema de alertas.

Es la base para:
- Explorar destinos baratos ("a cualquier sitio") en un rango de fechas.
- Comparar rápido varias fechas de ida y vuelta flexibles.
- Construir encima **alertas de precio** (consultar a diario y avisar cuando un destino baja de un umbral).

## 2. Fuentes de datos y por qué hace falta un token

La API **no tiene datos propios**: los obtiene de proveedores externos, que exigen identificarse
con una clave (token) para controlar el uso de su servicio.

| Fuente | Estado | Qué es | Coste | Clave |
|---|---|---|---|---|
| `travelpayouts` | **En uso** | Aviasales Data API | Gratis | `TRAVELPAYOUTS_TOKEN` |
| `google_flights` | Opcional, **no activado** | SerpApi → Google Travel Explore | 250 búsquedas/mes gratis | `SERPAPI_API_KEY` |

### Travelpayouts (la fuente principal)
- **Por qué el token**: Travelpayouts solo sirve su Data API a afiliados registrados. Sin token, la
  API responde `401` y no hay resultados. Con el token vacío, la API arranca pero `/deals`
  responde `503` ("no hay proveedores configurados").
- **Cómo se envía**: en la cabecera HTTP `X-Access-Token` (no en la URL), para que no quede
  escrito en logs, historiales ni en la clave de la caché.
- **Cómo funciona**: devuelve **los precios más baratos que otros usuarios de Aviasales han
  encontrado en los últimos días**. No es una búsqueda en tiempo real:
  - ✅ Muy rápido, gratis, sin límite mensual, y permite "cualquier destino".
  - ⚠️ Los precios son orientativos: hay que confirmarlos en el enlace (`link`).
  - ⚠️ Rutas poco buscadas (p. ej. BCN → Vietnam o Kirguistán a meses vista) pueden no tener datos.
    No significa que no haya vuelos, sino que nadie los ha buscado recientemente.
  - ⚠️ Algunos códigos IATA no los reconoce (p. ej. `FRU`, Biskek). En ese caso la API responde
    `200` con 0 resultados y el motivo en `warnings`.
- **Alta**: travelpayouts.com → unirse al programa *Aviasales* → *Herramientas → API*.

### SerpApi / Google Flights (opcional, sin usar)
El código está preparado pero desactivado mientras `SERPAPI_API_KEY` esté vacío. Daría precios de
Google en tiempo casi real (solo próximos 6 meses), pero el registro pide número de teléfono y tiene
cuota mensual, por eso no se usa.

### Seguridad del token
- Va en `.env`, que está en `.gitignore`: **nunca se sube a git**. Usa `.env.example` como plantilla.
- Si la carpeta está en OneDrive/Dropbox, ten en cuenta que `.env` **sí se sincroniza en la nube**.
- Si el token se expone, regenéralo en el panel de Travelpayouts y actualiza `.env`.
- La API no tiene autenticación propia: déjala en `localhost`. Si la expones a una red, cualquiera
  podría usarla (y consumir tu token).

## 3. Puesta en marcha

Requisitos: Python 3.11+.

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate     Linux/macOS:  source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # y rellena TRAVELPAYOUTS_TOKEN
uvicorn app.main:app --port 8000
```

- Comprobación: `curl localhost:8000/health` → `{"status":"ok","providers":["travelpayouts"]}`
- Documentación interactiva: <http://localhost:8000/docs>

Con Docker (se ejecuta como usuario sin privilegios):

```bash
docker build -t flight-deals .
docker run -p 8000:8000 --env-file .env flight-deals
```

### Variables de entorno (`.env`)

| Variable | Defecto | Uso |
|---|---|---|
| `TRAVELPAYOUTS_TOKEN` | — | Token de la Data API (obligatorio para usar Travelpayouts) |
| `TRAVELPAYOUTS_MARKET` | `es` | Mercado de Aviasales |
| `TRAVELPAYOUTS_MARKER` | — | Marker de afiliado, se añade a los enlaces |
| `SERPAPI_API_KEY` | — | Activa Google Flights vía SerpApi (opcional) |
| `DEFAULT_CURRENCY` | `EUR` | Moneda por defecto |
| `LANGUAGE` / `COUNTRY` | `es` | Idioma de nombres de ciudad / país para Google |
| `CACHE_TTL_SECONDS` | `21600` (6 h) | Tiempo que se guardan en memoria las respuestas |
| `HTTP_TIMEOUT_SECONDS` | `30` | Timeout de las llamadas a proveedores |

## 4. Uso: `GET /deals`

| Parámetro | Obligatorio | Descripción |
|---|---|---|
| `origin` | ✅ | IATA de salida (ciudad o aeropuerto): `MAD`, `BCN`… |
| `destination` | | IATA de destino. **Vacío = cualquier destino** |
| `depart_from`, `depart_to` | | Rango de ida (`YYYY-MM-DD`). Solo `depart_from` = ese día exacto. Solo `depart_to` = desde hoy |
| `return_from`, `return_to` | | Rango de vuelta. Si se indica, la búsqueda es de ida y vuelta |
| `trip_type` | | `auto` (defecto), `one_way`, `round_trip` |
| `min_price`, `max_price` | | Rango de precio total |
| `min_nights`, `max_nights` | | Duración de la estancia (implica ida y vuelta) |
| `direct_only` | | `true` = solo sin escalas |
| `currency` | | `EUR` por defecto |
| `sources` | | `travelpayouts` y/o `google_flights`. Vacío = todas las configuradas |
| `group_by_destination` | | `true` = solo la oferta más barata de cada destino |
| `limit` | | Máx. resultados (1-500, defecto 50) |

### Ejemplos

```bash
# Desde Barcelona a cualquier sitio, ida 18-22 y vuelta 27-31 de marzo, lo más barato por destino
curl "localhost:8000/deals?origin=BCN&depart_from=2027-03-18&depart_to=2027-03-22&return_from=2027-03-27&return_to=2027-03-31&group_by_destination=true"

# Desde Madrid, salidas en diciembre por menos de 80 €
curl "localhost:8000/deals?origin=MAD&depart_from=2026-12-01&depart_to=2026-12-31&max_price=80&group_by_destination=true"

# Escapada: 2-4 noches, solo directos
curl "localhost:8000/deals?origin=BCN&depart_from=2027-01-08&depart_to=2027-01-30&min_nights=2&max_nights=4&direct_only=true"
```

> Consejo: con fechas exactas suele haber pocos resultados (los datos son de búsquedas de otros
> usuarios). Deja ±2-3 días de margen para ver muchos más destinos.

### Respuesta

```json
{
  "query": { "origin": "BCN", "...": "..." },
  "count": 1,
  "sources_used": ["travelpayouts"],
  "warnings": [],
  "results": [{
    "source": "travelpayouts", "origin": "BCN", "destination": "LIS",
    "destination_name": "Lisboa", "destination_country": "PT",
    "departure_date": "2027-03-22", "return_date": "2027-03-27", "nights": 5,
    "price": 95.0, "currency": "EUR", "airline": "U2", "stops": 0,
    "link": "https://www.aviasales.com/search/..."
  }]
}
```

### Códigos de respuesta

| Código | Cuándo |
|---|---|
| `200` | Búsqueda correcta (puede tener 0 resultados; los motivos van en `warnings`) |
| `422` | Parámetros no válidos (fechas pasadas, rangos invertidos, IATA mal escrito…) |
| `502` | Todas las fuentes fallaron (token inválido, red caída, error del proveedor) |
| `503` | No hay ninguna fuente configurada |

## 5. Cómo funciona por dentro

```
GET /deals
  └─ main.py        valida parámetros (models.SearchParams) → 422 si algo está mal
      └─ service.py  lanza en paralelo todos los proveedores activos
          ├─ providers/travelpayouts.py  1 llamada por combinación mes-ida × mes-vuelta (máx. 12)
          └─ providers/serpapi.py        1 llamada por mes de ida (máx. 6 meses)
              └─ providers/base.py       GET con caché, 1 reintento ante 429/5xx/red,
                                         clasifica errores (consulta rechazada vs fallo real)
      ← service.py   unifica, deduplica, filtra (fechas, precio, noches, escalas),
                     agrupa por destino si se pide y ordena por precio
```

| Archivo | Responsabilidad |
|---|---|
| `app/main.py` | Endpoints `/health` y `/deals`, ciclo de vida del cliente HTTP, mapeo de errores a códigos HTTP |
| `app/models.py` | `SearchParams` (parámetros y validación), `Deal` (oferta), `SearchResponse`, ventanas de fechas |
| `app/service.py` | Orquesta proveedores; errores de un proveedor se convierten en `warnings` sin tumbar la respuesta |
| `app/cache.py` | Caché en memoria con caducidad (TTL) y tamaño máximo. **Se pierde al reiniciar** |
| `app/config.py` | Lee `.env` / variables de entorno sin dependencias externas |
| `app/providers/base.py` | Clase base `Provider`, GET con caché y reintento, `ProviderError` / `ProviderQueryError` |
| `app/providers/travelpayouts.py` | Construye consultas a Aviasales v3, traduce resultados a `Deal`, añade nombres de ciudad |
| `app/providers/serpapi.py` | Construye consultas a Google Travel Explore (opcional) |
| `tests/test_api.py` | 17 tests con respuestas simuladas: no usan red ni claves |

### Tipos de error de proveedor
- `ProviderQueryError` (HTTP 400): el proveedor funciona pero no acepta la consulta (p. ej. IATA
  desconocido). Resultado: `200` con 0 resultados y aviso.
- `ProviderError` (401, 5xx, red, JSON inválido): fallo real. Si fallan todas las fuentes, `502`.

## 6. Tests

```bash
pytest -q
```

Cubren búsqueda con rangos, filtros de precio/escalas/noches, agrupación por destino, validaciones,
caché, fallo de un proveedor, consulta rechazada, token en cabecera y reintento ante errores
transitorios.

## 7. Limitaciones conocidas y próximos pasos
- Los precios de Travelpayouts son de búsquedas recientes de otros usuarios, no en tiempo real.
- La caché es solo en memoria: no hay histórico de precios.
- **Siguiente objetivo**: alertas de precio (consulta diaria + histórico persistente + aviso cuando
  un destino baje de un umbral).
- Para añadir otra fuente, crea una clase que herede de `Provider` en `app/providers/` y añádela en
  `build_service()` de `app/main.py`.
