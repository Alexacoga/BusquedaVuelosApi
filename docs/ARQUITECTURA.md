# Arquitectura y contrato de la API

Diagramas de las llamadas de **Flight Deals API v1.2.0** (Mermaid: GitHub los dibuja solos) y resumen
del contrato. El contrato completo y exacto está en [`openapi.json`](openapi.json). Para regenerarlo:
`python scripts/export_openapi.py`. Un test comprueba que no se desincroniza del código.

## 1. Vista general

```mermaid
flowchart LR
    C([Cliente]) -->|GET / · /health · /deals · /calendar| M[main.py<br/>validación SearchParams]
    M -->|422 si no es válido| C
    M --> S[service.py<br/>FlightDealsService]
    S --> TP[TravelpayoutsProvider]
    S -.->|opcional, sin clave| SP[SerpApiProvider]
    TP --> B[base.py · _get_json<br/>caché TTL 6 h · 1 reintento · máx. 6 simultáneas]
    SP --> B
    B -->|X-Access-Token en cabecera| A1[(api.travelpayouts.com<br/>/aviasales/v3/prices_for_dates)]
    B --> A2[(api.travelpayouts.com<br/>/data/es/cities.json)]
    B -.-> A3[(serpapi.com<br/>Google Travel Explore)]
```

| Pieza | Qué hace |
|---|---|
| `main.py` | Lee los parámetros, los valida y traduce errores a HTTP (422 / 502 / 503) |
| `service.py` | Decide qué consultas lanzar según el modo, combina billetes, filtra, deduplica, anota y ordena |
| `providers/travelpayouts.py` | Construye cada consulta, convierte la respuesta en `Deal` / `Leg`, nombres, coordenadas y distancias |
| `providers/base.py` | GET con caché (el token no forma parte de la clave), reintento ante 429/5xx/red, clasificación de errores |

## 2. `GET /deals`: búsqueda normal

```mermaid
sequenceDiagram
    autonumber
    participant C as Cliente
    participant M as main.py
    participant S as service
    participant T as Travelpayouts provider
    participant API as Travelpayouts API
    C->>M: GET /deals?origin=BCN&depart_from=…&return_from=…
    M->>M: SearchParams (fechas, IATA, rangos)
    alt parámetros no válidos
        M-->>C: 422 {detail:[{field, error}]}
    end
    M->>S: search(params)
    S->>T: search(params)
    par una llamada por mes-ida × mes-vuelta (máx. 12)
        T->>API: prices_for_dates (origin, destination?, departure_at, return_at)
    and nombres (una vez por arranque)
        T->>API: cities.json
    end
    API-->>T: precios cacheados de búsquedas recientes
    T-->>S: Deals
    S->>S: dedupe → filtros → shifts/ahorro → agrupar/ordenar → limit
    opt 0 resultados y suggest_alternatives
        S->>S: _alternatives() (ver §6)
    end
    S-->>M: SearchResponse
    M-->>C: 200 {count, results, alternatives, warnings}
```

Errores de proveedor: una consulta rechazada (HTTP 400, p. ej. un IATA desconocido) devuelve `200`
con aviso. Si fallan todas las fuentes, `502`. Si no hay ninguna configurada, `503`.

## 3. Billetes sueltos y open-jaw (`split_tickets`, `open_jaw`, `return_origin`)

Se lanzan **en paralelo** a la búsqueda normal, que sirve de referencia para `savings_vs_exact`.

```mermaid
flowchart TD
    P[params] --> Q{¿destination?}
    Q -->|no| D1[A→cualquiera con fechas<br/>destinos más baratos · max_candidates]
    Q -->|sí| D2[ese destino]
    D1 --> R{¿ciudad de vuelta conocida?<br/>split o return_origin}
    D2 --> R
    R -->|sí| PAR[en paralelo:<br/>ida A→X · vuelta Y→A]
    R -->|open_jaw sin ciudad| W[en paralelo:<br/>ida A→X + red de rutas sin fechas<br/>cualquiera→A y A→cualquiera]
    W --> NEAR[ciudades a ≤ open_jaw_max_km del destino<br/>distancia con coordenadas de cities.json]
    NEAR --> IN[vuelta Y→A por cada ciudad]
    PAR --> COMB[combine_round_trip:<br/>vuelta después de llegar · noches · precio]
    IN --> COMB
    COMB --> OUT[Deals kind=split / open_jaw<br/>legs con un enlace por billete]
```

## 4. Modo avanzado (`mode=advanced`): conexión por cuenta propia

### Solo ida A→X→B

```mermaid
flowchart TD
    A[A, B, fechas de ida] --> NET[Red de rutas sin fechas, en paralelo:<br/>A→cualquiera · cualquiera→B]
    NET --> HUBS[Hubs = ciudades en ambas redes<br/>ordenadas por precio · max_candidates]
    HUBS -->|ninguno| WARN[aviso: sin ciudades intermedias]
    HUBS --> FIX[En paralelo por hub X:<br/>A→X en la ventana de ida<br/>X→B en ida + max_layover_days + 1]
    FIX --> COMB[combine_self_transfer]
    COMB --> CHECK{conexión ≥ min_connection_hours<br/>noches ≤ max_layover_days<br/>cambio de aeropuerto permitido}
    CHECK -->|sí| DEAL[Deal self_transfer<br/>layover_city · stopover_type · estimated_total]
```

### Ida y vuelta

```mermaid
flowchart LR
    RT[fechas de ida y de vuelta] --> O[Ida A→X→B<br/>como solo ida]
    RT --> I[Vuelta B→Y→A<br/>como solo ida]
    RT --> DIR[Directos A→B y B→A]
    O --> X[Cruce: las 30 opciones más baratas de cada sentido<br/>al menos un sentido con conexión propia<br/>vuelta posterior a la llegada · noches · precio]
    I --> X
    DIR --> X
    X --> R[Deal self_transfer con 3-4 legs<br/>layover_city · return_layover_city]
```

## 5. `GET /calendar`

Usa el mismo `collect()` que `/deals`, con todos sus modos. Luego se queda con el más barato por
(fecha de ida, fecha de vuelta), calcula `stats` y el tercio de precio (`price_level`), y monta `grid`.

```mermaid
flowchart LR
    C[collect: igual que /deals] --> B[mejor oferta por fecha de ida, fecha de vuelta]
    B --> L[price_level por terciles<br/>low · mid · high]
    L --> G[cells + grid + stats + cheapest]
```

## 6. Alternativas cuando no hay resultados

```mermaid
flowchart TD
    Z[/deals con 0 resultados<br/>y suggest_alternatives=true/] --> V1[Misma ruta ±7 días]
    Z --> NET[Red de rutas sin fechas:<br/>origen→cualquiera · cualquiera→destino]
    NET --> V2[≤3 destinos a ≤ nearby_km del destino<br/>búsqueda normal con ±3 días como mínimo]
    NET --> V3[≤2 orígenes a ≤ nearby_km del origen]
    V1 --> M[en paralelo · 5 mejores de cada una<br/>campo alternative = motivo]
    V2 --> M
    V3 --> M
    M --> R[response.alternatives<br/>results sigue vacío]
```

## 7. Llamadas externas por modo

Todas pasan por la caché (6 h), así que repetir una búsqueda parecida no vuelve a llamar.

| Modo | Llamadas a `prices_for_dates` |
|---|---|
| Normal | 1 por mes-ida × mes-vuelta (máx. 12) |
| `split_tickets` con destino | +1-2 por mes |
| `split_tickets` a cualquier destino | +1 de descubrimiento + 2 por candidato |
| `open_jaw` sin ciudad | +2 de red de rutas + 1 por ciudad de vuelta |
| `mode=advanced` solo ida | +2 de red de rutas + 2 por hub (máx. 15 hubs) |
| `mode=advanced` ida y vuelta | el doble que solo ida, +2 de directos |
| Alternativas (solo con 0 resultados) | +2 de red de rutas + 1 búsqueda normal por alternativa (máx. 6) |

`cities.json` se pide una sola vez por arranque.

## 8. Contrato (resumen)

### Endpoints

| Método y ruta | Respuesta 200 | Errores |
|---|---|---|
| `GET /` | Presentación en inglés: qué hace, fuente de datos, endpoints, ejemplos y fuentes activas | — |
| `GET /health` | `HealthResponse {status, providers[]}` | — |
| `GET /deals` | `SearchResponse` | 422, 502, 503 |
| `GET /calendar` | `CalendarResponse` (necesita fechas de ida) | 422, 502, 503 |

**`/deals` frente a `/calendar`:** aceptan los mismos parámetros y hacen la misma búsqueda.
`/deals` devuelve una lista de ofertas ordenada (`sort`, `limit`, `group_by_destination`) y
`alternatives` si no hay resultados. `/calendar` devuelve una celda por fecha (o par de fechas) con
el precio más barato, `price_level`, `stats` y `grid`, y necesita fechas de ida.

Las descripciones completas, las llamadas de ejemplo y las respuestas de ejemplo están en el
contrato: `/docs` y `docs/openapi.json` (textos en `app/openapi_docs.py`).

### Parámetros de `/deals` y `/calendar` (query string)

| Grupo | Parámetros |
|---|---|
| Ruta | `origin`* · `destination` · `sources` · `currency` |
| Fechas | `depart_from` · `depart_to` · `return_from` · `return_to` · `trip_type` (`auto`/`one_way`/`round_trip`) |
| Filtros | `min_price` · `max_price` · `min_nights` · `max_nights` · `direct_only` |
| Resultado | `sort` (`price`/`price_per_night`/`nights_desc`/`closest`/`score`/`duration`) · `group_by_destination` · `limit` |
| Fechas flexibles | `flex_days` · `depart_flex` · `return_flex` (0-7) · `max_shift_total` · `shift_penalty` |
| Billetes sueltos | `split_tickets` · `open_jaw` · `return_origin` · `open_jaw_max_km` · `max_candidates` |
| Modo avanzado | `mode` (`standard`/`advanced`) · `max_layover_days` · `min_connection_hours` · `allow_airport_change` · `stopover_cost_per_night` |
| Alternativas | `suggest_alternatives` · `nearby_km` |

\* obligatorio. Las reglas de validación (fechas pasadas, rangos invertidos, combinaciones
incompatibles…) están en `SearchParams._check` y devuelven `422`.

### Respuestas

```mermaid
classDiagram
    class SearchResponse {
        query: dict
        count: int
        sources_used: list~str~
        warnings: list~str~
        results: list~Deal~
        alternatives: list~Deal~
    }
    class Deal {
        source, kind: DealKind
        origin, destination, destination_name, destination_country
        departure_date, return_date, nights
        price, currency, airline, flight_number
        stops, return_stops, duration_minutes, link
        depart_shift, return_shift, savings_vs_exact
        return_origin, layover_city, layover_hours
        return_layover_city, return_layover_hours
        airport_change, stopover_type, estimated_total
        notes: list~str~, alternative
    }
    class Leg {
        origin, origin_airport, destination, destination_airport
        departure_at, arrival_at, departure_date
        price, currency, airline, flight_number, stops, link
    }
    class CalendarResponse {
        trip, currency, sources_used, warnings
        stats: CalendarStats
        cheapest: CalendarCell
        cells: list~CalendarCell~
        grid: dict
    }
    SearchResponse "1" --> "*" Deal
    Deal "1" --> "0..4" Leg : legs
    CalendarResponse "1" --> "*" CalendarCell
```

`DealKind`: `one_way` · `round_trip` · `split` · `open_jaw` · `self_transfer`.
Los campos vacíos se omiten de la respuesta.

| Código | Cuerpo |
|---|---|
| `422` | `{"detail": [{"field": "depart_from", "error": "…"}]}` |
| `502` | `{"detail": ["travelpayouts: …"]}` (avisos de cada fuente) |
| `503` | `{"detail": "No hay proveedores disponibles…"}` |
