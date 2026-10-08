"""Cálculo de horarios libres: franjas del negocio menos lo ocupado en el calendario de cada profesional."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .calendar_backend import CalendarBackend
from .models import Business, Professional, Service


@dataclass(frozen=True)
class Slot:
    start: datetime
    professional_id: str


def _conflicts(start, end, busy, buffer: timedelta, ignore) -> bool:
    """Choca si se solapa con algo ocupado o si no deja `buffer` libre antes/después."""
    return any(
        b_s < end + buffer and b_e + buffer > start
        for b_s, b_e in busy
        if (b_s, b_e) != ignore
    )


def is_free(
    business: Business,
    calendar: CalendarBackend,
    pro: Professional,
    start: datetime,
    end: datetime,
    ignore: tuple[datetime, datetime] | None = None,
) -> bool:
    """Revisión en vivo justo antes de escribir. `ignore` es la propia cita al reprogramarla."""
    buffer = timedelta(minutes=business.buffer_minutes)
    busy = calendar.busy(pro.calendar_id, start - buffer, end + buffer)
    return not _conflicts(start, end, busy, buffer, ignore)


def free_slots(
    business: Business,
    calendar: CalendarBackend,
    pros: list[Professional],
    service: Service,
    *,
    now: datetime,
    after: datetime | None = None,
    only_date: date | None = None,
    prefer_time: time | None = None,
    exclude: tuple[datetime, str] | None = None,
    ignore: tuple[datetime, datetime] | None = None,
    limit: int | None = None,
) -> list[Slot]:
    """Huecos libres, uno por hora de inicio (si hay varios profesionales libres se ofrece el primero).

    after:       solo huecos que empiecen estrictamente después (paginación de "más opciones").
    only_date:   limita la búsqueda a un día.
    prefer_time: ordena por cercanía a esa hora (en vez de cronológico) antes de recortar a `limit`.
    exclude:     (inicio, profesional) que no debe ofrecerse: la hora actual al reprogramar (J06).
    ignore:      bloque ocupado que es la propia cita; no cuenta como conflicto al reprogramar.
    """
    tz = business.tz
    limit = limit or business.max_options
    earliest = now + timedelta(minutes=business.min_notice_minutes)
    first_day = only_date or max(earliest, after or earliest).astimezone(tz).date()
    last_day = only_date or (now.astimezone(tz).date() + timedelta(days=business.horizon_days))
    if first_day > last_day:
        return []
    window_start = datetime.combine(first_day, time.min, tz)
    window_end = datetime.combine(last_day + timedelta(days=1), time.min, tz)

    busy_by_pro = {p.id: calendar.busy(p.calendar_id, window_start, window_end) for p in pros}
    step = timedelta(minutes=business.slot_minutes)
    length = timedelta(minutes=service.minutes)
    buffer = timedelta(minutes=business.buffer_minutes)

    found: dict[datetime, Slot] = {}
    day = first_day
    while day <= last_day:
        for open_t, close_t in business.hours.get(day.weekday(), ()):
            start = datetime.combine(day, open_t, tz)
            close_dt = datetime.combine(day, close_t, tz)
            while start + length <= close_dt:
                end = start + length
                ok_time = start >= earliest and (after is None or start > after)
                if ok_time and start not in found:
                    for pro in pros:
                        if exclude and exclude == (start, pro.id):
                            continue
                        if _conflicts(start, end, busy_by_pro[pro.id], buffer, ignore):
                            continue
                        found[start] = Slot(start, pro.id)
                        break
                start += step
        day += timedelta(days=1)

    slots = sorted(found.values(), key=lambda s: s.start)
    if prefer_time is not None:
        target = prefer_time.hour * 60 + prefer_time.minute

        def distance(s: Slot) -> int:
            local = s.start.astimezone(tz)
            return abs(local.hour * 60 + local.minute - target)

        return sorted(sorted(slots, key=distance)[:limit], key=lambda s: s.start)
    return slots[:limit]
