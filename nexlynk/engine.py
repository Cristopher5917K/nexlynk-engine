"""Motor conversacional de agendamiento (multi-negocio).

Cada mensaje pasa primero por la capa de intención (NLU) y solo después se compara con el estado,
así un "mejor mañana" en medio de la confirmación no se trata como una respuesta inválida (J02/J05).
Los cambios de datos son parciales: cambiar el día no borra el servicio ni el profesional (J01).
El estado vive en `Store` bajo (business_id, wa_id) (J08).

Estados: idle → collect → pick_slot → name → confirm → idle
         idle → pick_appt → confirm_cancel / collect (reprogramar) → ...
         cualquiera → handoff (el bot calla hasta resume())
"""
from __future__ import annotations

import copy
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable

from . import messages as msg
from .availability import Slot, free_slots, is_free
from .calendar_backend import CalendarBackend, CalendarError
from .models import Business
from .nlu import NLU, NLUContext, RuleNLU, Understanding, clean_name, normalize
from .store import Appointment, Store

log = logging.getLogger(__name__)

BOOKING_STATES = ("collect", "pick_slot", "name", "confirm")
_NOT_A_NAME_START = ("con ", "el ", "la ", "mejor ", "para ", "a ", "no ", "si ", "otro ", "otra ")


def _mentions_appointment(text: str) -> bool:
    return any(w in normalize(text) for w in ("cita", "turno", "reserva"))


@dataclass
class Reply:
    text: str
    buttons: list[str] = field(default_factory=list)  # máx. 3: botones de respuesta rápida de WhatsApp


class Engine:
    def __init__(
        self,
        businesses: dict[str, Business],
        store: Store,
        calendar: CalendarBackend,
        nlu: NLU | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        on_handoff: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self.businesses = businesses
        self.store = store
        self.calendar = calendar
        self.nlu = nlu or RuleNLU()
        self.clock = clock
        self.on_handoff = on_handoff or (
            lambda business_id, wa_id, text: log.warning("HANDOFF %s/%s: %s", business_id, wa_id, text)
        )
        self._locks: dict[tuple[str, str], threading.Lock] = defaultdict(threading.Lock)
        self._booking_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------------------------------
    def handle_message(self, business_id: str, wa_id: str, text: str, message_id: str | None = None) -> Reply:
        biz = self.businesses.get(business_id)
        if biz is None:
            raise KeyError(f"negocio desconocido: {business_id}")
        wa_id = (wa_id or "").strip()
        if not wa_id:
            raise ValueError("wa_id vacío: sin remitente no se puede aislar la conversación")
        if message_id and not self.store.mark_message(biz.id, message_id):
            return Reply("")  # reintento de WhatsApp del mismo mensaje: ya se respondió

        with self._conversation_lock(biz.id, wa_id):
            # Se trabaja sobre una copia: si algo falla a mitad, no se persiste un estado a medias.
            session = copy.deepcopy(self.store.get_session(biz.id, wa_id))
            now = self.clock().astimezone(biz.tz)
            first_contact = self.store.ensure_customer(biz.id, wa_id)
            try:
                reply = self._process(biz, wa_id, session, text, now)
            except CalendarError:
                log.exception("fallo de calendario (%s/%s)", biz.id, wa_id)
                reply = Reply(msg.CALENDAR_DOWN)
            else:
                self.store.save_session(biz.id, wa_id, session)
            if first_contact and biz.privacy_notice and reply.text:
                reply.text = f"{biz.privacy_notice}\n\n{reply.text}"
            return reply

    def resume(self, business_id: str, wa_id: str) -> None:
        """Un humano terminó de atender: el bot vuelve a responder."""
        with self._conversation_lock(business_id, wa_id):
            self.store.save_session(business_id, wa_id, {})

    # ------------------------------------------------------------------------------------------
    # Núcleo
    # ------------------------------------------------------------------------------------------
    def _conversation_lock(self, business_id: str, wa_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks[(business_id, wa_id)]

    def _booking_lock(self, business_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._booking_locks[business_id]

    def _process(self, biz: Business, wa_id: str, s: dict, text: str, now: datetime) -> Reply:
        state = s.get("state", "idle")
        if state == "handoff":
            return Reply("")  # un humano atiende: el bot calla hasta resume()

        offered = len(s.get("offered", [])) if state == "pick_slot" else len(s.get("choices", [])) if state == "pick_appt" else 0
        ctx = NLUContext(business=biz, now=now, state=state, offered_count=offered, draft=s.get("draft", {}))
        u = self.nlu.understand(text, ctx)
        if u.name:
            self.store.set_customer_name(biz.id, wa_id, u.name)
        in_booking = state in BOOKING_STATES and s.get("mode") == "book"

        # Intenciones globales: válidas en cualquier estado
        if u.intent == "handoff":
            s.clear()
            s["state"] = "handoff"
            self.on_handoff(biz.id, wa_id, text)
            return Reply(msg.HANDOFF)
        if u.intent == "my_appointments":
            return self._list_appointments(biz, wa_id, now)
        if u.intent == "info":
            return self._answer_info(biz, s, u, now, state)
        if u.intent == "cancel":
            if in_booking and not _mentions_appointment(text):
                s.clear()
                return Reply(msg.FLOW_ABORTED, msg.MENU_BUTTONS)
            return self._start_cancel(biz, wa_id, s, now)
        if u.intent == "reschedule":
            if in_booking:  # J05: "quiero cambiar mi cita" durante la reserva edita el borrador
                if changed := self._patch(biz, s, u):
                    return self._advance(biz, wa_id, s, now, prefix=self._change_ack(biz, s, changed))
                s["state"], s["chosen"] = "collect", None
                return Reply(msg.EDIT_DRAFT)
            return self._start_reschedule(biz, wa_id, s, u, now)

        handler = {
            "idle": self._on_idle,
            "collect": self._on_collect,
            "pick_slot": self._on_pick_slot,
            "name": self._on_name,
            "confirm": self._on_confirm,
            "pick_appt": self._on_pick_appt,
            "confirm_cancel": self._on_confirm_cancel,
        }.get(state)
        if handler is None:  # estado desconocido (sesión vieja): se reinicia sin romper
            s.clear()
            return Reply(msg.welcome(biz), msg.MENU_BUTTONS)
        return handler(biz, wa_id, s, u, now, text)

    # ------------------------------------------------------------------------------------------
    # Borrador
    # ------------------------------------------------------------------------------------------
    @staticmethod
    def _reset(s: dict, mode: str = "book") -> None:
        s.clear()
        s.update(state="collect", mode=mode, draft={}, offered=[], cursor=None, chosen=None, target=None, choices=[])

    @staticmethod
    def _patch(biz: Business, s: dict, u: Understanding) -> list[str]:
        """Actualización parcial del borrador. Devuelve los campos que cambiaron realmente."""
        draft = s.setdefault("draft", {})
        if u.without and not u.service:  # "sin barba" sobre un combo deja la parte restante
            current = biz.service(draft.get("service"))
            if current and u.without in current.components:
                remaining = [c for c in current.components if c != u.without]
                if len(remaining) == 1:
                    u.service = remaining[0]
        changed: list[str] = []
        for key, value in u.slots().items():
            if draft.get(key) != value:
                draft[key] = value
                changed.append(key)
        service_id, pro_id = draft.get("service"), draft.get("professional")
        if service_id and pro_id and pro_id != "any":
            pro = biz.professional(pro_id)
            if pro is None or not pro.offers(service_id):
                draft.pop("professional")
                s["pro_notice"] = pro.name if pro else None
        return changed  # una fecha nueva conserva la hora anterior: "mejor mañana" mantiene "a las 10"

    @staticmethod
    def _change_ack(biz: Business, s: dict, changed: list[str]) -> str:
        """"Perfecto: corte y barba, viernes 9 de octubre." — dice qué cambió y qué se mantiene."""
        draft = s["draft"]
        parts = []
        if service := biz.service(draft.get("service")):
            parts.append(service.name.lower())
        if "professional" in changed and (pro := biz.professional(draft.get("professional"))):
            parts.append(f"con {pro.name}")
        if draft.get("date"):
            try:
                parts.append(msg.fmt_day(date.fromisoformat(draft["date"])))
            except ValueError:
                pass
        if "time" in changed and draft.get("time"):
            parts.append(f"a las {draft['time']}")
        return f"Perfecto: {', '.join(parts)}. " if parts else msg.CHANGE_ACK

    # ------------------------------------------------------------------------------------------
    # Avance del flujo de reserva / reprogramación
    # ------------------------------------------------------------------------------------------
    def _advance(self, biz: Business, wa_id: str, s: dict, now: datetime, prefix: str = "") -> Reply:
        draft = s["draft"]
        s["state"], s["chosen"] = "collect", None
        notice = s.pop("pro_notice", None)
        pre = prefix + (f"{notice} no ofrece ese servicio. " if notice else "")

        service = biz.service(draft.get("service"))
        if service is None:
            return Reply(pre + msg.ask_service(biz), [x.name for x in biz.services][:3])

        eligible = biz.professionals_for(service.id)
        if not eligible:
            return Reply(pre + msg.NO_PROFESSIONALS)
        if len(eligible) == 1:
            draft["professional"] = eligible[0].id
        elif not draft.get("professional"):
            draft["professional"] = "any"  # no se pregunta: las opciones muestran con quién es

        if not draft.get("date"):
            return Reply(pre + msg.ASK_DATE, ["Hoy", "Mañana"])
        try:
            wanted_day = date.fromisoformat(draft["date"])
        except ValueError:
            draft.pop("date")
            return Reply(pre + msg.ASK_DATE, ["Hoy", "Mañana"])
        if wanted_day < now.date():
            draft.pop("date")
            return Reply(pre + msg.PAST_DATE)
        if wanted_day > now.date() + timedelta(days=biz.horizon_days):
            draft.pop("date")
            return Reply(pre + msg.OUT_OF_RANGE)

        wanted_time = time.fromisoformat(draft["time"]) if draft.get("time") else None
        slots = self._search(biz, wa_id, s, now, only_date=wanted_day, prefer_time=wanted_time)
        if slots:
            if wanted_time:
                exact = next((sl for sl in slots if sl.start.astimezone(biz.tz).time() == wanted_time), None)
                if exact:
                    return self._choose(biz, wa_id, s, exact, pre)
                pre += msg.time_unavailable(wanted_time)
            return self._offer(biz, s, slots, pre + msg.closest_slots(wanted_time))

        slots = self._search(biz, wa_id, s, now, after=datetime.combine(wanted_day, time.max, biz.tz))
        if not slots:
            return Reply(pre + msg.NO_AVAILABILITY)
        return self._offer(biz, s, slots, pre + msg.no_slots_that_day(wanted_day))

    def _search(self, biz, wa_id, s, now, *, only_date=None, after=None, prefer_time=None) -> list[Slot]:
        draft = s["draft"]
        service = biz.service(draft["service"])
        eligible = biz.professionals_for(service.id)
        pros = eligible if draft.get("professional") == "any" else [p for p in eligible if p.id == draft["professional"]]
        exclude = ignore = None
        if s.get("mode") == "reschedule" and s.get("target"):
            appt = self.store.get_appointment(biz.id, wa_id, s["target"])
            if appt:
                exclude = (appt.start, appt.professional_id)  # J06: no ofrecer la misma hora
                ignore = (appt.start, appt.end)  # la propia cita no bloquea moverla a una hora cercana
        return free_slots(
            biz, self.calendar, pros, service,
            now=now, after=after, only_date=only_date, prefer_time=prefer_time, exclude=exclude, ignore=ignore,
        )

    def _option_line(self, biz: Business, i: int, start: datetime, pro_id: str) -> str:
        return f"{i}. {msg.fmt_dt(start.astimezone(biz.tz))} con {biz.professional(pro_id).name}"

    def _offer(self, biz: Business, s: dict, slots: list[Slot], intro: str) -> Reply:
        s["state"] = "pick_slot"
        s["offered"] = [{"start": sl.start.isoformat(), "pro": sl.professional_id} for sl in slots]
        s["cursor"] = slots[-1].start.isoformat()
        lines = [self._option_line(biz, i, sl.start, sl.professional_id) for i, sl in enumerate(slots, 1)]
        buttons = [msg.short_dt(sl.start.astimezone(biz.tz)) for sl in slots]
        return Reply(intro + "\n" + "\n".join(lines) + "\n\n" + msg.PICK_HINT, buttons)

    def _choose(self, biz: Business, wa_id: str, s: dict, slot: Slot, prefix: str = "") -> Reply:
        local = slot.start.astimezone(biz.tz)
        s["draft"]["date"], s["draft"]["time"] = local.date().isoformat(), local.strftime("%H:%M")
        s["chosen"] = {"start": slot.start.isoformat(), "pro": slot.professional_id}
        if s.get("mode") == "book" and not self.store.customer_name(biz.id, wa_id):
            s["state"] = "name"
            return Reply(prefix + msg.ASK_NAME)
        return self._ask_confirm(biz, wa_id, s, prefix)

    def _ask_confirm(self, biz: Business, wa_id: str, s: dict, prefix: str = "") -> Reply:
        s["state"] = "confirm"
        chosen = s["chosen"]
        service = biz.service(s["draft"]["service"])
        pro = biz.professional(chosen["pro"])
        when = msg.fmt_dt(datetime.fromisoformat(chosen["start"]).astimezone(biz.tz))
        old = None
        if s.get("mode") == "reschedule" and s.get("target"):
            appt = self.store.get_appointment(biz.id, wa_id, s["target"])
            old = msg.fmt_dt(appt.start.astimezone(biz.tz)) if appt else None
        name = self.store.customer_name(biz.id, wa_id)
        return Reply(prefix + msg.confirm_text(service, pro, when, name, old), ["Sí, confirmar", "No"])

    # ------------------------------------------------------------------------------------------
    # Estados del flujo de reserva
    # ------------------------------------------------------------------------------------------
    def _on_idle(self, biz, wa_id, s, u: Understanding, now, text) -> Reply:
        if u.intent == "yes":
            if s.get("suggest_service"):
                service_id = s["suggest_service"]
                self._reset(s)
                s["draft"]["service"] = service_id
                return self._advance(biz, wa_id, s, now)
            if s.get("last_booked"):  # CE01: un segundo "sí" no crea otra cita
                return Reply(msg.already_booked(s["last_booked"]))
        if u.intent == "book" or u.slots() or u.without:
            suggested = s.get("suggest_service")
            self._reset(s)
            if suggested and not u.service:
                s["draft"]["service"] = suggested
            self._patch(biz, s, u)
            return self._advance(biz, wa_id, s, now)
        return Reply(msg.welcome(biz), msg.MENU_BUTTONS)

    def _on_collect(self, biz, wa_id, s, u: Understanding, now, text) -> Reply:
        self._patch(biz, s, u)
        return self._advance(biz, wa_id, s, now)

    def _on_pick_slot(self, biz, wa_id, s, u: Understanding, now, text) -> Reply:
        offered = [(datetime.fromisoformat(o["start"]), o["pro"]) for o in s.get("offered", [])]

        if u.choice is not None:
            if 1 <= u.choice <= len(offered):
                return self._choose(biz, wa_id, s, Slot(*offered[u.choice - 1]))
            return Reply(msg.choose_range(len(offered)))

        # "la de Alex": elige si hay una sola opción con Alex; si hay varias, pregunta (J07)
        if u.professional and u.professional != "any" and not (u.service or u.date or u.time):
            matches = [o for o in offered if o[1] == u.professional]
            if len(matches) == 1:
                return self._choose(biz, wa_id, s, Slot(*matches[0]))
            if len(matches) > 1:
                s["offered"] = [{"start": st.isoformat(), "pro": p} for st, p in matches]
                lines = [self._option_line(biz, i, st, p) for i, (st, p) in enumerate(matches, 1)]
                return Reply(msg.which_of(biz.professional(u.professional).name, lines))

        # "la de las 3": coincide con una opción mostrada
        if u.time and not u.service and not u.professional:
            for start, pro_id in offered:
                local = start.astimezone(biz.tz)
                if local.strftime("%H:%M") == u.time and (not u.date or local.date().isoformat() == u.date):
                    return self._choose(biz, wa_id, s, Slot(start, pro_id))

        if changed := self._patch(biz, s, u):
            return self._advance(biz, wa_id, s, now, prefix=self._change_ack(biz, s, changed))

        if u.intent == "more_options":
            slots = self._search(biz, wa_id, s, now, after=datetime.fromisoformat(s["cursor"]))
            if not slots:
                return Reply(msg.NO_MORE_OPTIONS)
            return self._offer(biz, s, slots, msg.MORE_INTRO)
        if u.intent == "yes" and len(offered) == 1:
            return self._choose(biz, wa_id, s, Slot(*offered[0]))
        if u.intent == "no":
            s["draft"].pop("time", None)
            s["state"] = "collect"
            return Reply(msg.ASK_OTHER_TIME)
        return Reply(msg.choose_range(len(offered)))

    def _on_name(self, biz, wa_id, s, u: Understanding, now, text) -> Reply:
        if u.name:
            return self._ask_confirm(biz, wa_id, s)
        # Respuesta suelta a "¿a nombre de quién?": "Julian". Va antes del patch para que un
        # cliente que se llama igual que un profesional no cambie de barbero sin querer.
        plain = normalize(text) + " "
        if (
            u.intent == "other" and not (u.service or u.date or u.time)
            and not plain.startswith(_NOT_A_NAME_START)
            and (name := clean_name(text))
        ):
            self.store.set_customer_name(biz.id, wa_id, name)
            return self._ask_confirm(biz, wa_id, s)
        if changed := self._patch(biz, s, u):
            return self._advance(biz, wa_id, s, now, prefix=self._change_ack(biz, s, changed))
        return Reply(msg.ASK_NAME_AGAIN)

    def _on_confirm(self, biz, wa_id, s, u: Understanding, now, text) -> Reply:
        # Un cambio de datos ("sí, pero sin barba", "mejor mañana") tiene prioridad sobre sí/no (RS01)
        if changed := self._patch(biz, s, u):
            return self._advance(biz, wa_id, s, now, prefix=self._change_ack(biz, s, changed))
        if u.intent == "yes":
            return self._finalize(biz, wa_id, s, now)
        if u.intent == "no":
            s["draft"].pop("date", None)
            s["draft"].pop("time", None)
            s["state"], s["chosen"] = "collect", None
            return Reply(msg.ASK_OTHER_TIME)
        return Reply(msg.CONFIRM_REPROMPT, ["Sí, confirmar", "No"])

    def _finalize(self, biz: Business, wa_id: str, s: dict, now: datetime) -> Reply:
        draft, chosen = s["draft"], s["chosen"]
        service = biz.service(draft["service"])
        pro = biz.professional(chosen["pro"])
        start = datetime.fromisoformat(chosen["start"])
        end = start + timedelta(minutes=service.minutes)
        name = self.store.customer_name(biz.id, wa_id)
        old: Appointment | None = None
        if s.get("mode") == "reschedule":
            old = self.store.get_appointment(biz.id, wa_id, s["target"])
            if old is None:
                s.clear()
                return Reply(msg.APPT_GONE)

        # Las reservas de un negocio se serializan: revisión + escritura sin carreras entre clientes.
        # (Protege dentro de un proceso; con varios servidores hará falta un bloqueo en la base.)
        with self._booking_lock(biz.id):
            ignore = (old.start, old.end) if old and old.professional_id == pro.id else None
            if not is_free(biz, self.calendar, pro, start, end, ignore):
                s["draft"].pop("time", None)
                return self._advance(biz, wa_id, s, now, prefix=msg.SLOT_TAKEN)

            private = {"business_id": biz.id, "wa_id": wa_id}
            who = f"{name} ({wa_id})" if name else wa_id
            summary = f"{service.name} - {who}"
            description = f"Agendado por WhatsApp.\nCliente: {who}\nServicio: {service.name}"
            if old is None:
                event_id = self.calendar.create_event(pro.calendar_id, start, end, summary, description, private)
                self.store.add_appointment(biz.id, wa_id, pro.id, service.id, start, end, event_id)
            elif old.professional_id == pro.id and old.service_id == service.id:
                self.calendar.move_event(pro.calendar_id, old.event_id, start, end)
                self.store.update_appointment(biz.id, wa_id, old.id, pro.id, service.id, start, end, old.event_id)
            else:  # cambia de profesional o de servicio: evento nuevo y se retira el viejo
                event_id = self.calendar.create_event(pro.calendar_id, start, end, summary, description, private)
                self.store.update_appointment(biz.id, wa_id, old.id, pro.id, service.id, start, end, event_id)
                old_pro = biz.professional(old.professional_id)
                try:
                    self.calendar.delete_event(old_pro.calendar_id, old.event_id)
                except CalendarError:
                    log.exception("no se pudo borrar el evento viejo %s; revisar a mano", old.event_id)

        when = msg.fmt_dt(start.astimezone(biz.tz))
        s.clear()
        s.update(state="idle", last_booked=f"{service.name} con {pro.name}, {when}")
        return Reply(msg.booked_text(service, pro, when, name, rescheduled=old is not None))

    # ------------------------------------------------------------------------------------------
    # Información (precio, duración, horario, ubicación)
    # ------------------------------------------------------------------------------------------
    def _answer_info(self, biz: Business, s: dict, u: Understanding, now: datetime, state: str) -> Reply:
        topics = set(u.info) or {"price"}
        service = biz.service(u.service)
        if service is None and state != "idle":
            service = biz.service(s.get("draft", {}).get("service"))
        parts: list[str] = []
        if topics & {"price", "duration"}:
            # Solo el servicio preguntado; el catálogo completo solo si no nombró ninguno (IF01)
            parts.append(msg.service_line(service) if service else "\n".join(f"• {msg.service_line(x)}" for x in biz.services))
        if "hours" in topics:
            parts.append(msg.today_hours(biz, now.date()) + "\nNuestro horario:\n" + msg.week_hours(biz))
        if "location" in topics:
            parts.append(biz.address or msg.LOCATION_UNKNOWN)
        if state == "idle":
            if service:
                s["suggest_service"] = service.id
                parts.append(msg.suggest_booking(service))
            else:
                parts.append(msg.ask_service(biz))
        else:
            parts.append(msg.RESUME_FLOW)
        return Reply("\n".join(parts))

    # ------------------------------------------------------------------------------------------
    # Mis citas / cancelar / reprogramar
    # ------------------------------------------------------------------------------------------
    def _describe(self, biz: Business, a: Appointment) -> str:
        return (
            f"{biz.service(a.service_id).name} con {biz.professional(a.professional_id).name}, "
            f"{msg.fmt_dt(a.start.astimezone(biz.tz))}"
        )

    def _list_appointments(self, biz, wa_id, now) -> Reply:
        appts = self.store.upcoming_appointments(biz.id, wa_id, now)
        if not appts:
            return Reply(msg.NO_APPOINTMENTS, ["Agendar"])
        return Reply(msg.YOUR_APPOINTMENTS + "\n" + "\n".join(f"• {self._describe(biz, a)}" for a in appts))

    def _start_cancel(self, biz, wa_id, s, now) -> Reply:
        appts = self.store.upcoming_appointments(biz.id, wa_id, now)
        s.clear()
        if not appts:
            return Reply(msg.NO_APPOINTMENTS, ["Agendar"])
        if len(appts) == 1:
            s.update(state="confirm_cancel", target=appts[0].id)
            return Reply(msg.confirm_cancel(self._describe(biz, appts[0])), ["Sí, cancelar", "No"])
        return self._ask_which(biz, s, appts, "cancel")

    def _start_reschedule(self, biz, wa_id, s, u: Understanding, now) -> Reply:
        appts = self.store.upcoming_appointments(biz.id, wa_id, now)
        s.clear()
        if not appts:
            return Reply(msg.NO_APPOINTMENTS, ["Agendar"])
        s.update(mode="reschedule", draft={}, offered=[], choices=[])
        # "mover mi cita de hoy a las 16:00": fecha y hora del mensaje se usan para buscar;
        # el servicio y el profesional salen de la cita
        self._patch(biz, s, Understanding(date=u.date, time=u.time))
        if len(appts) == 1:  # J04: con una sola cita no se pide elegir número
            return self._select_appointment(biz, wa_id, s, appts[0], now)
        return self._ask_which(biz, s, appts, "reschedule")

    def _ask_which(self, biz, s, appts, action) -> Reply:
        s.update(state="pick_appt", pending=action, choices=[a.id for a in appts])
        lines = [f"{i}. {self._describe(biz, a)}" for i, a in enumerate(appts, 1)]
        verb = "cancelar" if action == "cancel" else "cambiar"
        return Reply(f"Tienes varias citas. ¿Cuál quieres {verb}?\n" + "\n".join(lines))

    def _on_pick_appt(self, biz, wa_id, s, u: Understanding, now, text) -> Reply:
        choices = s.get("choices", [])
        if u.choice is None or not 1 <= u.choice <= len(choices):
            return Reply(msg.choose_range(len(choices)))
        appt = self.store.get_appointment(biz.id, wa_id, choices[u.choice - 1])  # filtra por dueño
        if appt is None:
            s.clear()
            return Reply(msg.APPT_GONE)
        if s.get("pending") == "cancel":
            s.update(state="confirm_cancel", target=appt.id)
            return Reply(msg.confirm_cancel(self._describe(biz, appt)), ["Sí, cancelar", "No"])
        return self._select_appointment(biz, wa_id, s, appt, now)

    def _select_appointment(self, biz, wa_id, s, appt: Appointment, now) -> Reply:
        s["target"], s["mode"] = appt.id, "reschedule"
        draft = s.setdefault("draft", {})
        draft["service"], draft["professional"] = appt.service_id, appt.professional_id
        # Sin fecha nueva se ofrecen otras horas del mismo día (J06); el cliente puede pedir otro día
        draft.setdefault("date", appt.start.astimezone(biz.tz).date().isoformat())
        if date.fromisoformat(draft["date"]) < now.date():
            draft["date"] = now.date().isoformat()
        return self._advance(biz, wa_id, s, now, prefix=msg.reschedule_intro(self._describe(biz, appt)) + "\n")

    def _on_confirm_cancel(self, biz, wa_id, s, u: Understanding, now, text) -> Reply:
        if u.intent == "yes":
            appt = self.store.get_appointment(biz.id, wa_id, s["target"])
            if appt is None:
                s.clear()
                return Reply(msg.APPT_GONE)
            pro = biz.professional(appt.professional_id)
            self.calendar.delete_event(pro.calendar_id, appt.event_id)
            self.store.cancel_appointment(biz.id, wa_id, appt.id)
            s.clear()
            return Reply(msg.CANCELLED, ["Agendar"])
        if u.intent == "no":
            s.clear()
            return Reply(msg.KEPT)
        return Reply(msg.CONFIRM_REPROMPT, ["Sí, cancelar", "No"])
