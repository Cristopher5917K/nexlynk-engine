"""Configuración de negocios (multi-tenant). Un JSON por negocio en businesses/."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    minutes: int
    price: float | None = None
    aliases: tuple[str, ...] = ()
    components: tuple[str, ...] = ()  # un combo declara sus partes: "sin barba" sobre el combo deja "corte"


@dataclass(frozen=True)
class Professional:
    id: str
    name: str
    calendar_id: str
    services: tuple[str, ...] | None = None  # None = ofrece todos los servicios

    def offers(self, service_id: str) -> bool:
        return self.services is None or service_id in self.services


@dataclass(frozen=True)
class Business:
    id: str
    name: str
    timezone: str
    hours: dict[int, tuple[tuple[time, time], ...]]  # 0 = lunes; varias franjas = pausa de almuerzo
    services: tuple[Service, ...]
    professionals: tuple[Professional, ...]
    slot_minutes: int = 30
    buffer_minutes: int = 0  # tiempo libre obligatorio entre citas
    max_options: int = 3  # 3 = límite de botones de respuesta en WhatsApp
    min_notice_minutes: int = 60
    horizon_days: int = 14
    address: str | None = None
    privacy_notice: str | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def service(self, service_id: str | None) -> Service | None:
        return next((s for s in self.services if s.id == service_id), None)

    def professional(self, pro_id: str | None) -> Professional | None:
        return next((p for p in self.professionals if p.id == pro_id), None)

    def professionals_for(self, service_id: str) -> list[Professional]:
        return [p for p in self.professionals if p.offers(service_id)]


def _parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def _parse_day(value) -> tuple[tuple[time, time], ...]:
    """Acepta null (cerrado), ["09:00", "18:00"] o [["09:00", "13:00"], ["14:00", "18:00"]]."""
    if not value:
        return ()
    ranges = [value] if isinstance(value[0], str) else value
    parsed = tuple((_parse_hhmm(a), _parse_hhmm(b)) for a, b in ranges)
    for start, end in parsed:
        if start >= end:
            raise ValueError(f"franja horaria inválida: {start}-{end}")
    return tuple(sorted(parsed))


def business_from_dict(data: dict) -> Business:
    hours = {i: _parse_day(data["hours"].get(day)) for i, day in enumerate(_DAYS)}
    services = tuple(
        Service(
            id=s["id"],
            name=s["name"],
            minutes=int(s["minutes"]),
            price=s.get("price"),
            aliases=tuple(s.get("aliases", ())),
            components=tuple(s.get("components", ())),
        )
        for s in data["services"]
    )
    pros = tuple(
        Professional(
            id=p["id"],
            name=p["name"],
            calendar_id=p["calendar_id"],
            services=tuple(p["services"]) if p.get("services") else None,
        )
        for p in data["professionals"]
    )
    biz = Business(
        id=data["id"],
        name=data["name"],
        timezone=data.get("timezone", "America/Guayaquil"),
        hours=hours,
        services=services,
        professionals=pros,
        slot_minutes=int(data.get("slot_minutes", 30)),
        buffer_minutes=int(data.get("buffer_minutes", 0)),
        max_options=int(data.get("max_options", 3)),
        min_notice_minutes=int(data.get("min_notice_minutes", 60)),
        horizon_days=int(data.get("horizon_days", 14)),
        address=data.get("address"),
        privacy_notice=data.get("privacy_notice"),
    )
    ids = {s.id for s in services}
    for s in services:
        if not set(s.components) <= ids:
            raise ValueError(f"el servicio {s.id} tiene componentes desconocidos: {s.components}")
    return biz


def load_businesses(directory: str | Path) -> dict[str, Business]:
    out: dict[str, Business] = {}
    for path in sorted(Path(directory).glob("*.json")):
        biz = business_from_dict(json.loads(path.read_text(encoding="utf-8")))
        out[biz.id] = biz
    return out
