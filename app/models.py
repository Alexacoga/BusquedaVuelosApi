"""Modelos de entrada y salida de la API."""
from __future__ import annotations

from datetime import date, timedelta
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator


class TripType(str, Enum):
    auto = "auto"            # ida y vuelta si hay fechas de vuelta; si no, solo ida
    one_way = "one_way"
    round_trip = "round_trip"


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


def make_window(start: date | None, end: date | None) -> DateWindow | None:
    """Una sola fecha = ese día exacto; dos fechas = rango; ninguna = sin filtro."""
    if start is None and end is None:
        return None
    if start is None:  # solo "hasta": desde hoy hasta esa fecha
        start = date.today()
    if end is None:    # solo "desde": ese día exacto

        end = start
    return DateWindow(start=start, end=end)


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

    @field_validator("origin", "destination")
    @classmethod
    def _iata(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        v = v.strip().upper()
        if len(v) != 3 or not v.isalpha():
            raise ValueError("debe ser un código IATA de 3 letras (p. ej. MAD, BCN, LON)")
        return v

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
        return self

    # --- Derivados -------------------------------------------------------
    @property
    def is_round_trip(self) -> bool:
        if self.trip_type == TripType.auto:
            return bool(self.return_from or self.return_to or self.min_nights or self.max_nights)
        return self.trip_type == TripType.round_trip

    @property
    def depart_window(self) -> DateWindow | None:
        return make_window(self.depart_from, self.depart_to)

    @property
    def return_window(self) -> DateWindow | None:
        return make_window(self.return_from, self.return_to)


class Deal(BaseModel):
    source: str
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


class SearchResponse(BaseModel):
    query: dict
    count: int
    sources_used: list[str]
    warnings: list[str] = []
    results: list[Deal]
