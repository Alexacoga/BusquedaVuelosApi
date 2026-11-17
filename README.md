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
| `group_by_destination` | | `true` = solo la mejor oferta de cada destino (según `sort`) |
| `limit` | | Máx. resultados (1-500, defecto 50) |
| `sort` | | `price` (defecto), `price_per_night`, `nights_desc`, `closest`, `score`, `duration` |

### Búsqueda ampliada (fechas flexibles y billetes sueltos)

| Parámetro | Descripción |
|---|---|
| `flex_days` | Margen ± días en ida y vuelta (0-7) |
| `depart_flex`, `return_flex` | Margen ± días solo en la ida / solo en la vuelta (tienen prioridad sobre `flex_days`) |
| `max_shift_total` | Máx. días movidos sumando ida y vuelta |
| `shift_penalty` | € por día movido; con `sort=score` ordena por `precio + penalización` |
| `split_tickets` | `true` = compara también con **dos billetes de solo ida** (a menudo más barato con low-cost) |
| `open_jaw` | `true` = **volver desde otra ciudad** cercana al destino (solo devuelve combinaciones open-jaw) |
| `return_origin` | IATA de la ciudad desde la que volver (implica open-jaw) |
| `open_jaw_max_km` | Con `open_jaw` sin ciudad: distancia máx. entre destino y ciudad de vuelta (defecto 600) |
| `max_candidates` | Destinos / ciudades a combinar como máximo (1-15, defecto 8) |

Campos extra en cada resultado: `kind` (`one_way`, `round_trip`, `split`, `open_jaw`, `self_transfer`),
`depart_shift` / `return_shift` (días respecto a lo pedido, negativo = antes), `savings_vs_exact`
(ahorro frente a la mejor opción con las fechas exactas) y `legs` (cada billete con su propio enlace).

### Búsqueda ampliada avanzada (`mode=advanced`): dos vuelos enlazados por tu cuenta

Para un trayecto A→B (requiere `destination` y fechas de ida), busca además combinaciones
**A→X + X→B** con dos billetes independientes, aunque la espera sea de horas o días.
Ejemplo: Estocolmo → París → Barcelona más barato que el directo.

**Ida y vuelta**: si además das fechas de vuelta, resuelve cada sentido por separado (A→X→B y B→Y→A,
o el vuelo directo en uno de los dos) y los cruza respetando noches y precios. La vuelta indica su
conexión en `return_layover_city` / `return_layover_hours` y `legs` trae los 3-4 billetes en orden.

| Parámetro | Defecto | Descripción |
|---|---|---|
| `max_layover_days` | 1 | Noches máximas en la ciudad intermedia (0 = mismo día, máx. 3) |
| `min_connection_hours` | 3 | Margen mínimo entre la llegada del 1.er vuelo y la salida del 2.º |
| `allow_airport_change` | true | Permitir llegar y salir por aeropuertos distintos (p. ej. BVA → ORY) |
| `stopover_cost_per_night` | 0 | Coste estimado de alojamiento por noche; se suma en `estimated_total` y se usa para ordenar |
| `max_candidates` | 8 | Ciudades intermedias a probar (máx. 15) |

Campos extra: `layover_city`, `layover_hours`, `airport_change`, `stopover_type`
(`connection` mismo día · `overnight` pasas una noche · `stopover` 24 h o más), `estimated_total`, `notes`.

> ⚠️ **Riesgos de enlazar vuelos por tu cuenta** (la API lo avisa en `warnings`): si el primer vuelo se
> retrasa y pierdes el segundo no hay protección ni compensación; hay que recoger y volver a facturar el
> equipaje; revisa visados / Schengen en la ciudad intermedia; y confirma el precio de **ambos** tramos.

### Alternativas cuando no hay resultados

Si una búsqueda devuelve 0 resultados (ruta poco buscada o fechas sin datos), la API prueba sola y
devuelve en `alternatives` (sin mezclarlas con `results`), cada una con el motivo en `alternative`:

- **Fechas cercanas**: la misma ruta con ±7 días.
- **Destino cercano**: hasta 3 ciudades a menos de `nearby_km` del destino con vuelos desde tu origen
  (p. ej. pides Oporto → te propone Lisboa), con al menos ±3 días.
- **Origen cercano**: hasta 2 ciudades a menos de `nearby_km` de tu origen con vuelos al destino.

| Parámetro | Defecto | Descripción |
|---|---|---|
| `suggest_alternatives` | true | `false` = no buscar alternativas |
| `nearby_km` | 400 | Distancia máxima de las ciudades alternativas (50-1500) |

Si tampoco hay alternativas, `warnings` lo dice: esa ruta casi no tiene búsquedas recientes en
Travelpayouts (pasa con destinos lejanos poco demandados, p. ej. Vietnam o Kirguistán desde BCN).

### Enlaces de reserva
Cada resultado (o cada tramo en `legs`) trae un enlace de Aviasales con ese vuelo ya cargado: al abrirlo,
Aviasales repite la búsqueda en tiempo real y muestra las agencias / aerolínea con su precio para reservar.

### Ejemplos

```bash
# Desde Barcelona a cualquier sitio, ida 18-22 y vuelta 27-31 de marzo, lo más barato por destino
curl "localhost:8000/deals?origin=BCN&depart_from=2027-03-18&depart_to=2027-03-22&return_from=2027-03-27&return_to=2027-03-31&group_by_destination=true"

# Desde Madrid, salidas en diciembre por menos de 80 €
curl "localhost:8000/deals?origin=MAD&depart_from=2026-12-01&depart_to=2026-12-31&max_price=80&group_by_destination=true"

# Escapada: 2-4 noches, solo directos
curl "localhost:8000/deals?origin=BCN&depart_from=2027-01-08&depart_to=2027-01-30&min_nights=2&max_nights=4&direct_only=true"

# Fechas 20→29 de marzo con ±2 días, más días de viaje por euro, comparando billetes sueltos
curl "localhost:8000/deals?origin=BCN&depart_from=2027-03-20&return_from=2027-03-29&flex_days=2&split_tickets=true&sort=price_per_night&group_by_destination=true"

# Ida a Lisboa y vuelta desde Oporto
curl "localhost:8000/deals?origin=BCN&destination=LIS&depart_from=2027-03-20&return_from=2027-03-29&flex_days=2&return_origin=OPO"

# Estocolmo → Barcelona enlazando dos vuelos, hasta 2 noches en la ciudad intermedia (40 €/noche)
curl "localhost:8000/deals?origin=STO&destination=BCN&depart_from=2027-03-27&depart_to=2027-03-31&mode=advanced&max_layover_days=2&stopover_cost_per_night=40"

# Lo mismo en ida y vuelta (conexión propia en la ida, en la vuelta o en las dos)
curl "localhost:8000/deals?origin=STO&destination=BCN&depart_from=2027-03-20&return_from=2027-03-29&flex_days=1&mode=advanced"
```

## 4.b `GET /calendar`: precio más barato por día

Acepta **los mismos parámetros** que `/deals` (incluidos `flex_days`, `split_tickets`, `open_jaw`…) y
requiere fechas de ida. Devuelve la mejor oferta de cada fecha de ida (y de vuelta):

```bash
curl "localhost:8000/calendar?origin=BCN&destination=LIS&depart_from=2027-03-18&depart_to=2027-03-22&return_from=2027-03-27&return_to=2027-03-31"
```

```json
{
  "trip": "round_trip", "currency": "EUR",
  "stats": { "cells": 3, "min": 95.0, "max": 116.0, "avg": 103.33 },
  "cheapest": { "departure_date": "2027-03-22", "return_date": "2027-03-27", "price": 95.0, "...": "..." },
  "cells": [ { "departure_date": "2027-03-20", "return_date": "2027-03-27", "price": 99.0, "price_level": "mid", "...": "..." } ],
  "grid": { "2027-03-20": { "2027-03-27": 99.0 }, "2027-03-22": { "2027-03-27": 95.0, "2027-03-28": 116.0 } }
}
```

`price_level` (`low` / `mid` / `high`) indica en qué tercio de precios cae cada celda, para pintarlo como mapa de calor.

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
GET /deals  ·  GET /calendar
  └─ main.py        search_params() valida (models.SearchParams) → 422 si algo está mal
      └─ service.collect()  lanza en paralelo:
          ├─ búsqueda normal en cada proveedor activo (fechas ± flex)
          ├─ split_tickets / open_jaw → _split_tickets()   (solo travelpayouts)
          └─ mode=advanced            → _self_transfer()   (solo travelpayouts; ida y vuelta = 2 sentidos)
      └─ /deals sin resultados       → _alternatives()    (fechas ±7, ciudades cercanas)
              └─ providers/base.py   GET con caché, 1 reintento ante 429/5xx/red, máx. 6 llamadas
                                     simultáneas, clasifica errores (consulta rechazada vs fallo real)
      ← service.py   deduplica, filtra, calcula desplazamientos y ahorro,
                     /deals: agrupa y ordena según sort · /calendar: mejor precio por fecha
```

### Llamadas a Travelpayouts por modo (todas cacheadas `CACHE_TTL_SECONDS`)

| Modo | Llamadas |
|---|---|
| Normal | 1 por combinación mes-ida × mes-vuelta (máx. 12) + `cities.json` (una vez) |
| `split_tickets` con destino | + 1-2 por mes (ida y vuelta, en paralelo) |
| `split_tickets` a cualquier destino | + 1 para descubrir destinos + 2 por destino candidato (`max_candidates`) |
| `open_jaw` sin ciudad | + 2 de red de rutas (sin fechas) + 1 por ciudad de vuelta cercana |
| `mode=advanced` | + 2 de red de rutas (sin fechas) + 2 por ciudad intermedia (`max_candidates`); el doble en ida y vuelta, + 2 de vuelos directos |
| Alternativas (solo con 0 resultados) | + 2 de red de rutas + 1 búsqueda normal por alternativa (máx. 6) |

> Travelpayouts, si se deja vacío el origen o el destino, devuelve solo **la opción más barata de cada
> día**. Por eso los modos combinados descubren primero las ciudades con la **red de rutas** (consulta sin
> fechas, que devuelve cientos de vuelos de los próximos meses) y después piden cada ruta concreta.

| Archivo | Responsabilidad |
|---|---|
| `app/main.py` | Endpoints `/health`, `/deals` y `/calendar`; parámetros comunes; mapeo de errores a códigos HTTP |
| `app/models.py` | `SearchParams` (parámetros y validación), `Deal`, `Leg` (tramo), `SearchResponse`, `CalendarResponse`, ventanas de fechas con flexibilidad |
| `app/service.py` | Orquesta proveedores, combina billetes (split, open-jaw, self-transfer), filtra, anota ahorro y ordena; los errores de un proveedor se convierten en `warnings` |
| `app/cache.py` | Caché en memoria con caducidad (TTL) y tamaño máximo. **Se pierde al reiniciar** |
| `app/config.py` | Lee `.env` / variables de entorno sin dependencias externas |
| `app/providers/base.py` | Clase base `Provider`, GET con caché y reintento, `ProviderError` / `ProviderQueryError` |
| `app/providers/travelpayouts.py` | Consultas a Aviasales v3, `one_way_legs()` (billetes de solo ida con hora de llegada y aeropuertos), nombres y coordenadas de ciudades, `distance_km()` |
| `app/providers/serpapi.py` | Construye consultas a Google Travel Explore (opcional) |
| `docs/ARQUITECTURA.md` | Diagramas de llamadas (Mermaid) y resumen del contrato |
| `docs/openapi.json` | Contrato OpenAPI 3.1 exportado (`python scripts/export_openapi.py`) |
| `tests/test_api.py` | 33 tests con respuestas simuladas: no usan red ni claves |

### Tipos de error de proveedor
- `ProviderQueryError` (HTTP 400): el proveedor funciona pero no acepta la consulta (p. ej. IATA
  desconocido). Resultado: `200` con 0 resultados y aviso.
- `ProviderError` (401, 5xx, red, JSON inválido): fallo real. Si fallan todas las fuentes, `502`.

## 6. Tests

```bash
pytest -q
```

Cubren búsqueda con rangos, filtros de precio/escalas/noches, agrupación por destino, validaciones,
caché, fallo de un proveedor, consulta rechazada, token en cabecera, reintento ante errores
transitorios, fechas flexibles y ordenación, billetes sueltos, open-jaw (con ciudad y por distancia),
`/calendar`, el modo avanzado (conexión mínima, noches en la escala, cambio de aeropuerto, coste
de alojamiento e ida y vuelta) y las alternativas (fechas y destino cercanos).

## 7. Limitaciones conocidas y próximos pasos
- Los precios de Travelpayouts son de búsquedas recientes de otros usuarios, no en tiempo real.
- La caché es solo en memoria: no hay histórico de precios.
- Rutas poco buscadas pueden no tener datos; en ese caso se proponen `alternatives` (fechas y ciudades
  cercanas), pero si la ruta no existe en Travelpayouts no hay nada que proponer.
- Billetes sueltos, open-jaw y modo avanzado solo funcionan con Travelpayouts (es la única fuente que
  da billetes de solo ida con hora y aeropuerto). Para ampliar a otra fuente, debería implementar
  `one_way_legs()`.
- **Siguiente objetivo**: alertas de precio (consulta diaria + histórico persistente + aviso cuando
  un destino baje de un umbral).
- Para añadir otra fuente, crea una clase que herede de `Provider` en `app/providers/` y añádela en
  `build_service()` de `app/main.py`.
