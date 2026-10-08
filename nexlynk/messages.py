"""Textos del bot (español) y formato de fechas. Aquí se ajusta el tono; la lógica no cambia."""
from __future__ import annotations

from datetime import date, datetime, time

from .models import Business, Professional, Service

_DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_DAYS_SHORT = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
_MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
           "septiembre", "octubre", "noviembre", "diciembre"]

MENU_BUTTONS = ["Agendar", "Mis citas", "Reprogramar"]
ASK_DATE = "¿Para qué día? Puedes decirme \"mañana\", \"el viernes\" o una fecha."
ASK_NAME = "¿A nombre de quién dejo la reserva?"
ASK_NAME_AGAIN = "No alcancé a entender el nombre. ¿A nombre de quién dejo la reserva?"
PAST_DATE = "Esa fecha ya pasó. ¿Para qué otro día?"
OUT_OF_RANGE = "Todavía no tengo agenda abierta para esa fecha. ¿Te sirve un día más cercano?"
NO_PROFESSIONALS = "Por ahora no tengo a nadie disponible para ese servicio."
NO_AVAILABILITY = "No encuentro horarios libres en los próximos días. Si quieres, te comunico con alguien del equipo."
PICK_HINT = "Responde con el número, dime otra fecha u hora, o escribe \"más opciones\"."
ASK_OTHER_TIME = "No hay problema. ¿Qué día y más o menos a qué hora te vendría bien?"
NO_MORE_OPTIONS = "No tengo más horarios después de esos. ¿Probamos otro día?"
MORE_INTRO = "Claro, estos son los siguientes horarios:"
CHANGE_ACK = "Perfecto, lo cambio. "
NO_PROBLEM = "Sin problema. "
CONFIRM_REPROMPT = "¿Confirmas? Responde Sí o No, o dime qué quieres cambiar."
EDIT_DRAFT = "Claro, todavía no he agendado nada. ¿Qué quieres cambiar: el día, la hora, el servicio o el profesional?"
SLOT_TAKEN = "Uy, ese horario se acaba de ocupar. "
HANDOFF = "Entiendo. Le aviso a una persona del equipo para que te atienda; te responderá por aquí lo antes posible."
CALENDAR_DOWN = "Tuve un problema para consultar la agenda y no pude completar tu solicitud. Intenta de nuevo en unos minutos."
NO_APPOINTMENTS = "No tienes citas próximas."
YOUR_APPOINTMENTS = "Tus próximas citas:"
APPT_GONE = "No encontré esa cita. Escribe \"mis citas\" para ver las vigentes."
CANCELLED = "Listo, tu cita fue cancelada. Cuando quieras agendar otra, aquí estoy."
FLOW_ABORTED = "Listo, dejé sin efecto lo que estábamos agendando. Si querías cancelar una cita ya agendada, escribe \"cancelar mi cita\"."
KEPT = "De acuerdo, tu cita sigue en pie."
LOCATION_UNKNOWN = "No tengo la dirección a mano. Si quieres, te comunico con alguien del equipo."
RESUME_FLOW = "¿Seguimos con tu reserva?"


def welcome(biz: Business) -> str:
    return f"¡Hola! Soy el asistente virtual de {biz.name}. Puedo agendar, reprogramar o cancelar tu cita. ¿Qué necesitas?"


def ask_service(biz: Business) -> str:
    items = ", ".join(s.name.lower() for s in biz.services)
    return f"¿Qué servicio te gustaría: {items}?"


def fmt_dt(dt: datetime) -> str:
    return f"{_DAYS[dt.weekday()]} {dt.day} de {_MONTHS[dt.month - 1]}, {dt:%H:%M}"


def fmt_day(d: date) -> str:
    return f"{_DAYS[d.weekday()]} {d.day} de {_MONTHS[d.month - 1]}"


def short_dt(dt: datetime) -> str:
    return f"{_DAYS_SHORT[dt.weekday()]} {dt.day} {dt:%H:%M}"  # ≤ 20 caracteres (límite de WhatsApp)


def price(service: Service) -> str:
    return f"${service.price:g}" if service.price is not None else "precio por confirmar"


def service_line(service: Service) -> str:
    return f"{service.name}: {price(service)}, {service.minutes} min"


def ranges_text(ranges) -> str:
    return " y ".join(f"{a:%H:%M} a {b:%H:%M}" for a, b in ranges)


def week_hours(biz: Business) -> str:
    """Agrupa días consecutivos con el mismo horario: "lunes a viernes: 09:00 a 13:00 y 14:00 a 18:00"."""
    groups: list[tuple[int, int, tuple]] = []
    for i in range(7):
        rng = biz.hours.get(i, ())
        if groups and groups[-1][2] == rng:
            groups[-1] = (groups[-1][0], i, rng)
        else:
            groups.append((i, i, rng))
    lines = []
    for a, b, rng in groups:
        days = _DAYS[a] if a == b else f"{_DAYS[a]} a {_DAYS[b]}"
        lines.append(f"• {days}: {ranges_text(rng) if rng else 'cerrado'}")
    return "\n".join(lines)


def today_hours(biz: Business, today: date) -> str:
    rng = biz.hours.get(today.weekday(), ())
    if not rng:
        return f"Hoy {_DAYS[today.weekday()]} no atendemos."
    return f"Hoy {_DAYS[today.weekday()]} atendemos de {ranges_text(rng)}."


def time_unavailable(wanted: time) -> str:
    return f"A las {wanted:%H:%M} no tengo espacio. "


def closest_slots(wanted: time | None) -> str:
    return "Lo más cercano que tengo:" if wanted else "Tengo estos horarios disponibles:"


def no_slots_that_day(day: date) -> str:
    return f"El {fmt_day(day)} ya no me quedan horarios. Lo más cercano que tengo:"


def choose_range(n: int) -> str:
    if n <= 1:
        return "¿Te sirve ese horario? Responde Sí o dime otro día u hora."
    return f"Elige una opción del 1 al {n}, o dime otro día u hora."


def which_of(pro_name: str, lines: list[str]) -> str:
    return f"Tengo {len(lines)} opciones con {pro_name}:\n" + "\n".join(lines) + "\n¿Cuál prefieres?"


def confirm_text(service: Service, pro: Professional, when: str, name: str | None, old: str | None) -> str:
    head = f"Voy a mover tu cita del {old} al:" if old else "Quedaría así:"
    who = f"\n• A nombre de: {name}" if name else ""
    return (
        f"{head}\n• {service.name} ({price(service)}, {service.minutes} min)\n• Con {pro.name}\n• {when}{who}"
        "\n\n¿Confirmo? (Sí / No)"
    )


def booked_text(service: Service, pro: Professional, when: str, name: str | None, rescheduled: bool) -> str:
    verb = "reprogramada" if rescheduled else "confirmada"
    hello = f"Listo, {name}" if name else "Listo"
    return (
        f"✅ {hello}: tu cita quedó {verb} para el {when}, con {pro.name}. {service.name}.\n"
        "Si necesitas cambiarla o cancelarla, escríbeme."
    )


def already_booked(desc: str) -> str:
    return f"Tu cita ya quedó confirmada: {desc}. ¿Necesitas algo más?"


def confirm_cancel(desc: str) -> str:
    return f"¿Cancelo esta cita?\n• {desc}\n(Sí / No)"


def reschedule_intro(desc: str) -> str:
    return f"Tienes tu cita: {desc}. Todavía no la he cambiado."


def suggest_booking(service: Service) -> str:
    return f"¿Quieres agendar un {service.name.lower()}? Dime el día."
