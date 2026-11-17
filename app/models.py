"""Modelos de entrada y salida de la API."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_FLEX_DAYS = 7
MAX_LAYOVER_DAYS = 3


class TripType(str, Enum):
    auto = "auto"            # ida y vuelta si hay fechas de vuelta; si no, solo ida
    one_way = "one_way"
    round_trip = "round_trip"


class SortBy(str, Enum):
    price = "price"                      # más barato primero
    price_per_night = "price_per_night"  # más noches por euro (solo ida y vuelta)
    nights_desc = "nights_desc"          # estancia más larga primero
    closest = "closest"                  # más parecido a las fechas pedidas
    score = "score"                      # precio + shift_penalty € por día desplazado
    duration = "duration"                # menos tiempo total de viaje primero


class SearchMode(str, Enum):
    standard = "standard"                # vuelos tal cual los vende el proveedor
    advanced = "advanced"                # además enlaza dos vuelos por cuenta propia (A→X→B)


class DealKind(str, Enum):
    one_way = "one_way"                  # billete de solo ida
    round_trip = "round_trip"            # billete de ida y vuelta
    split = "split"                      # dos billetes de solo ida (ida y vuelta por separado)
    open_jaw = "open_jaw"                # dos billetes, la vuelta sale de otra ciudad
    self_transfer = "self_transfer"      # dos vuelos enlazados por cuenta propia (modo avanzado)


class DateWindow(BaseModel):
    start: date
    end: date

    @property
    def is_single_day(self) -> bool:
        return self.start == self.end

    def contains(self, d: date | None) -> bool:
        return d is not None and self.start <= d <= self.end

    def months(self) -> list[str]:
        """Meses (YYYY-MM) que abarca la ventana."""
        out, cur = [], self.start.replace(day=1)
        while cur <= self.end:
            out.append(cur.strftime("%Y-%m"))
            cur = (cur + timedelta(days=32)).replace(day=1)
        return out

    def expand(self, before: int, after: int | None = None) -> "DateWindow":
        """Amplía la ventana; nunca empieza antes de hoy."""
        after = before if after is None else after
        start = max(self.start - timedelta(days=before), date.today())
        return DateWindow(start=start, end=self.end + timedelta(days=after))

    def shift(self, d: date | None) -> int | None:
        """Días que `d` queda fuera de la ventana (negativo = antes, 0 = dentro)."""
        if d is None:
            return None
        if d < self.start:
            return (d - self.start).days
        if d > self.end:
            return (d - self.end).days
        return 0


def make_window(start: date | None, end: date | None) -> DateWindow | None:
    """Una sola fecha = ese día exacto; dos fechas = rango; ninguna = sin filtro."""
    if start is None and end is None:
        return None
    if start is None:  # solo "hasta": desde hoy hasta esa fecha
        start = date.today()
    if end is None:    # solo "desde": ese día exacto
        end = start
    return DateWindow(start=start, end=end)


def _iata_or_none(v: str | None) -> str | None:
    if v is None or v == "":
        return None
    v = v.strip().upper()
    if len(v) != 3 or not v.isalpha():
        raise ValueError("debe ser un código IATA de 3 letras (p. ej. MAD, BCN, LON)")
    return v


class SearchParams(BaseModel):
    origin: str = Field(..., description="IATA de ciudad o aeropuerto de salida (p. ej. MAD)")
    destination: str | None = Field(None, description="IATA de destino. Vacío = cualquier destino")
    depart_from: date | None = None
    depart_to: date | None = None
    return_from: date | None = None
    return_to: date | None = None
    trip_type: TripType = TripType.auto
    min_price: float | None = Field(None, ge=0)
    max_price: float | None = Field(None, ge=0)
    min_nights: int | None = Field(None, ge=0)
    max_nights: int | None = Field(None, ge=0)
    direct_only: bool = False
    currency: str = "EUR"
    sources: list[str] | None = None
    group_by_destination: bool = False
    limit: int = Field(50, ge=1, le=500)
    # --- Búsqueda ampliada (fechas flexibles) -----------------------------
    flex_days: int | None = Field(None, ge=0, le=MAX_FLEX_DAYS)
    depart_flex: int | None = Field(None, ge=0, le=MAX_FLEX_DAYS)
    return_flex: int | None = Field(None, ge=0, le=MAX_FLEX_DAYS)
    max_shift_total: int | None = Field(None, ge=0)
    shift_penalty: float = Field(0, ge=0)
    sort: SortBy = SortBy.price
    # --- Billetes sueltos / open-jaw ----------------------------------------
    split_tickets: bool = False
    open_jaw: bool = False
    return_origin: str | None = None
    open_jaw_max_km: int = Field(600, ge=50, le=3000)
    max_candidates: int = Field(8, ge=1, le=15)
    # --- Modo avanzado: dos vuelos enlazados por cuenta propia ---------------
    mode: SearchMode = SearchMode.standard
    max_layover_days: int = Field(1, ge=0, le=MAX_LAYOVER_DAYS)
    min_connection_hours: float = Field(3, ge=1, le=24)
    allow_airport_change: bool = True
    stopover_cost_per_night: float = Field(0, ge=0)
    # --- Alternativas cuando no hay resultados -----------------------------
    suggest_alternatives: bool = True
    nearby_km: int = Field(400, ge=50, le=1500)

    @field_validator("origin", "destination", "return_origin")
    @classmethod
    def _iata(cls, v: str | None) -> str | None:
        return _iata_or_none(v)

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 3 or not v.isalpha():
            raise ValueError("moneda ISO de 3 letras (EUR, USD...)")
        return v

    @model_validator(mode="after")
    def _check(self) -> "SearchParams":
        today = date.today()
        if self.min_price is not None and self.max_price is not None and self.min_price > self.max_price:
            raise ValueError("min_price no puede ser mayor que max_price")
        if self.min_nights is not None and self.max_nights is not None and self.min_nights > self.max_nights:
            raise ValueError("min_nights no puede ser mayor que max_nights")
        for a, b, name in ((self.depart_from, self.depart_to, "ida"), (self.return_from, self.return_to, "vuelta")):
            if a and b and a > b:
                raise ValueError(f"el rango de {name} está invertido (desde > hasta)")
            for d in (a, b):
                if d and d < today:
                    raise ValueError(f"la fecha de {name} {d} ya ha pasado")
        if self.trip_type == TripType.one_way and (self.return_from or self.return_to):
            raise ValueError("trip_type=one_way no admite fechas de vuelta")
        if self.origin == self.destination:
            raise ValueError("origen y destino no pueden ser iguales")
        if self.depart_flex_days and self.depart_window is None:
            raise ValueError("la flexibilidad de ida requiere fechas de ida (depart_from/depart_to)")
        if self.return_flex and self.return_window is None:
            raise ValueError("return_flex requiere fechas de vuelta (return_from/return_to)")
        if (self.open_jaw or self.return_origin) and not self.is_round_trip:
            raise ValueError("open_jaw / return_origin solo tienen sentido en ida y vuelta")
        if self.return_origin and self.return_origin == self.origin:
            raise ValueError("return_origin no puede ser igual al origen")
        if self.mode == SearchMode.advanced:
            if not self.destination:
                raise ValueError("mode=advanced necesita destination (el tramo A→B que quieres abaratar)")
            if self.depart_window is None:
                raise ValueError("mode=advanced necesita fechas de ida (depart_from y/o depart_to)")
            if self.is_round_trip and self.return_window is None:
                raise ValueError("mode=advanced en ida y vuelta necesita fechas de vuelta (return_from y/o return_to)")
        return self

    # --- Derivados -------------------------------------------------------
    @property
    def is_round_trip(self) -> bool:
        if self.trip_type == TripType.auto:
            return bool(self.return_from or self.return_to or self.min_nights or self.max_nights)
        return self.trip_type == TripType.round_trip

    @property
    def uses_split(self) -> bool:
        return self.is_round_trip and (self.split_tickets or self.open_jaw or bool(self.return_origin))

    @property
    def depart_flex_days(self) -> int:
        return self.depart_flex if self.depart_flex is not None else (self.flex_days or 0)

    @property
    def return_flex_days(self) -> int:
        return self.return_flex if self.return_flex is not None else (self.flex_days or 0)

    @property
    def depart_window(self) -> DateWindow | None:
        """Fechas de ida pedidas (sin flexibilidad)."""
        return make_window(self.depart_from, self.depart_to)

    @property
    def return_window(self) -> DateWindow | None:
        """Fechas de vuelta pedidas (sin flexibilidad)."""
        return make_window(self.return_from, self.return_to)

    @property
    def depart_search_window(self) -> DateWindow | None:
        """Fechas de ida que se consultan (pedidas ± flexibilidad)."""
        w = self.depart_window
        return w.expand(self.depart_flex_days) if w and self.depart_flex_days else w

    @property
    def return_search_window(self) -> DateWindow | None:
        w = self.return_window
        return w.expand(self.return_flex_days) if w and self.return_flex_days else w


class Leg(BaseModel):
    """Un tramo/billete individual dentro de una combinación."""
    source: str
    origin: str
    origin_airport: str | None = None
    origin_country: str | None = None
    destination: str
    destination_airport: str | None = None
    destination_name: str | None = None
    destination_country: str | None = None
    departure_at: datetime | None = None
    arrival_at: datetime | None = None
    departure_date: date
    price: float
    currency: str
    airline: str | None = None
    flight_number: str | None = None
    stops: int | None = None
    duration_minutes: int | None = None
    link: str | None = None


class Deal(BaseModel):
    source: str
    kind: DealKind | None = None
    origin: str
    destination: str
    destination_name: str | None = None
    destination_country: str | None = None
    departure_date: date
    return_date: date | None = None
    nights: int | None = None
    price: float
    currency: str
    airline: str | None = None
    flight_number: str | None = None
    stops: int | None = None
    return_stops: int | None = None
    duration_minutes: int | None = None
    link: str | None = None
    found_at: str | None = None
    # Búsqueda ampliada
    depart_shift: int | None = None
    return_shift: int | None = None
    savings_vs_exact: float | None = None
    # Combinaciones de varios billetes (split / open_jaw / self_transfer)
    return_origin: str | None = None
    legs: list[Leg] | None = None
    layover_hours: float | None = None
    layover_city: str | None = None
    return_layover_city: str | None = None     # modo avanzado ida y vuelta: conexión propia en la vuelta
    return_layover_hours: float | None = None
    airport_change: bool | None = None
    stopover_type: str | None = None
    estimated_total: float | None = None
    notes: list[str] | None = None
    # Solo en `alternatives`: por qué se propone (fechas cercanas, destino u origen cercano)
    alternative: str | None = None

    @property
    def total_shift(self) -> int:
        return abs(self.depart_shift or 0) + abs(self.return_shift or 0)


class SearchResponse(BaseModel):
    query: dict
    count: int
    sources_used: list[str]
    warnings: list[str] = []
    results: list[Deal]
    # Solo si `results` está vacío y suggest_alternatives=true: fechas o ciudades cercanas con datos
    alternatives: list[Deal] = []


class CalendarCell(BaseModel):
    """Precio más barato para una fecha de ida (y de vuelta, si es ida y vuelta)."""
    departure_date: date
    return_date: date | None = None
    nights: int | None = None
    price: float
    price_level: str  # low | mid | high (tercio de precios en el que cae)
    destination: str
    destination_name: str | None = None
    kind: DealKind | None = None
    airline: str | None = None
    stops: int | None = None
    link: str | None = None


class CalendarStats(BaseModel):
    cells: int
    min: float
    max: float
    avg: float


class CalendarResponse(BaseModel):
    query: dict
    trip: str                       # one_way | round_trip
    currency: str
    sources_used: list[str]
    warnings: list[str] = []
    stats: CalendarStats | None = None
    cheapest: CalendarCell | None = None
    cells: list[CalendarCell]
    # Rejilla compacta: {ida: precio} o {ida: {vuelta: precio}}
    grid: dict[str, float | dict[str, float]]
