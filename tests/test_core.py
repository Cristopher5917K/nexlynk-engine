"""Pruebas fuera de la matriz: doble reserva, fallos de calendario, cancelar/reprogramar,
cálculo de horarios, Google Calendar (con un servicio falso) y la API."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from nexlynk.app import create_app
from nexlynk.availability import free_slots
from nexlynk.calendar_backend import CalendarError, GoogleCalendar
from nexlynk.engine import Engine
from nexlynk.models import business_from_dict
from nexlynk.nlu import RuleNLU, clean_name
from nexlynk.store import Store

from .conftest import BARBERIA, Clock, book, say, session

TZ = ZoneInfo("America/Guayaquil")


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 10, day, hh, mm, tzinfo=TZ)


# --- reservas concurrentes -------------------------------------------------------------------------
def test_dos_clientes_mismo_hueco_solo_uno_lo_obtiene(engine, cal):
    for user, name in (("A", "Ana"), ("B", "Beto")):
        say(engine, user, "corte con alex el jueves a las 10")
        say(engine, user, name)
        assert session(engine, user)["state"] == "confirm"

    assert "quedó confirmada" in say(engine, "A", "sí").text
    r = say(engine, "B", "sí")
    assert "se acaba de ocupar" in r.text
    alex_10 = [e for e in cal.events.values() if e["calendar_id"] == "cal-alex" and e["start"] == at(8, 10)]
    assert len(alex_10) == 1
    assert session(engine, "B")["state"] == "pick_slot"  # le ofrece alternativas


def test_si_el_calendario_falla_no_confirma_ni_pierde_el_estado(engine, cal):
    say(engine, "A", "corte el jueves a las 10")
    say(engine, "A", "Julian")
    cal.fail = True
    r = say(engine, "A", "sí")
    assert "problema para consultar la agenda" in r.text
    assert session(engine, "A")["state"] == "confirm"
    assert not engine.store.all_appointments("barberia")

    cal.fail = False
    assert "quedó confirmada" in say(engine, "A", "sí").text


# --- cancelar y reprogramar ----------------------------------------------------------------------
def test_cancelar_borra_el_evento_y_marca_la_cita(engine, cal):
    book(engine, "A", "corte el jueves a las 10")
    r = say(engine, "A", "cancelar mi cita")
    assert "¿Cancelo esta cita?" in r.text
    assert "cancelada" in say(engine, "A", "sí").text
    assert not cal.events
    (appt,) = engine.store.all_appointments("barberia")
    assert appt.status == "cancelled"


def test_no_quiero_cancelar_conserva_la_cita(engine, cal):
    book(engine, "A", "corte el jueves a las 10")
    say(engine, "A", "cancelar mi cita")
    assert "sigue en pie" in say(engine, "A", "no quiero cancelar").text
    assert len(cal.events) == 1


def test_cancelar_en_medio_de_una_reserva_solo_abandona_el_borrador(engine, cal):
    book(engine, "A", "corte el jueves a las 10")
    say(engine, "A", "barba el viernes")
    r = say(engine, "A", "cancelar")
    assert "dejé sin efecto" in r.text
    assert len(cal.events) == 1  # la cita existente no se tocó


def test_reprogramar_mueve_el_mismo_evento(engine, cal):
    book(engine, "A", "corte el jueves a las 10")
    (event_id,) = cal.events
    r = say(engine, "A", "reprogramar para el viernes a las 11")
    assert "Voy a mover tu cita del jueves 8 de octubre, 10:00" in r.text
    assert "reprogramada" in say(engine, "A", "sí").text
    assert list(cal.events) == [event_id]
    assert cal.events[event_id]["start"] == at(9, 11)


def test_reprogramar_con_varias_citas_pide_elegir(engine):
    book(engine, "A", "corte el jueves a las 10")
    book(engine, "A", "barba el viernes a las 11")
    r = say(engine, "A", "quiero cambiar mi cita")
    assert "Tienes varias citas" in r.text
    say(engine, "A", "2")
    assert session(engine, "A")["draft"]["service"] == "barba"


def test_cliente_recurrente_no_vuelve_a_dar_su_nombre(engine):
    book(engine, "A", "corte el jueves a las 10", name="Julian")
    r = say(engine, "A", "barba el viernes a las 11")
    assert session(engine, "A")["state"] == "confirm"
    assert "Julian" in r.text


def test_nombre_igual_a_un_profesional_no_cambia_de_barbero(engine):
    say(engine, "A", "corte con alex el jueves a las 10")
    say(engine, "A", "Dani")  # el cliente se llama Dani
    s = session(engine, "A")
    assert s["state"] == "confirm" and s["chosen"]["pro"] == "alex"
    assert engine.store.customer_name("barberia", "A") == "Dani"


# --- otro rubro con la misma plataforma -----------------------------------------------------------
def test_veterinaria_con_una_sola_profesional(engine, cal):
    r = book(engine, "A", "consulta mañana a las 9", name="Paula", business="vet")
    assert "Dra. Paz" in r.text
    assert [e["calendar_id"] for e in cal.events.values()] == ["cal-vet"]


# --- cálculo de horarios -----------------------------------------------------------------------------
@pytest.fixture
def biz():
    return business_from_dict(BARBERIA)


def test_buffer_de_10_minutos_entre_citas(biz, cal):
    alex = biz.professional("alex")
    cal.create_event("cal-alex", at(8, 10), at(8, 10, 30), "x", "", {})
    starts = [s.start for s in free_slots(biz, cal, [alex], biz.service("corte"), now=at(6, 10),
                                          only_date=date(2026, 10, 8), limit=50)]
    assert at(8, 10, 30) not in starts  # pegado a la cita anterior: sin los 10 min de margen
    assert at(8, 9, 30) not in starts  # terminaría 10:00, sin margen antes
    assert at(8, 11) in starts and at(8, 9) in starts


def test_almuerzo_y_cierre(biz, cal):
    alex = biz.professional("alex")
    combos = [s.start.time() for s in free_slots(biz, cal, [alex], biz.service("combo"), now=at(6, 10),
                                                 only_date=date(2026, 10, 8), limit=50)]
    assert time(12, 0) in combos  # 12:00-12:50
    assert time(12, 30) not in combos  # terminaría 13:20, dentro del almuerzo
    assert time(17, 30) not in combos  # terminaría después de las 18:00


def test_domingo_cerrado_y_anticipacion_minima(biz, cal):
    alex = biz.professional("alex")
    corte = biz.service("corte")
    assert free_slots(biz, cal, [alex], corte, now=at(6, 10), only_date=date(2026, 10, 11)) == []
    today = free_slots(biz, cal, [alex], corte, now=at(6, 10), only_date=date(2026, 10, 6))
    assert today[0].start == at(6, 11)  # 60 min de anticipación


def test_nombres_validos_e_invalidos():
    assert clean_name("julian perez") == "Julian Perez"
    assert clean_name("José") == "José"
    assert clean_name("=HYPERLINK(1)") is None
    assert clean_name("x" * 41) is None


# --- Google Calendar con un servicio falso (sin tocar Google) --------------------------------------
class _Req:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def execute(self):
        if self.error:
            raise self.error
        return self.result


class _FakeGoogle:
    def __init__(self, freebusy_result):
        self.calls = []
        self._fb = freebusy_result

    def freebusy(self):
        outer = self

        class FB:
            def query(self, body):
                outer.calls.append(("freebusy", body))
                return _Req(outer._fb)

        return FB()

    def events(self):
        outer = self

        class Ev:
            def insert(self, calendarId, body):
                outer.calls.append(("insert", calendarId, body))
                return _Req({"id": "gevt1"})

            def patch(self, calendarId, eventId, body):
                outer.calls.append(("patch", calendarId, eventId, body))
                return _Req({})

            def delete(self, calendarId, eventId):
                outer.calls.append(("delete", calendarId, eventId))
                return _Req({})

        return Ev()


def test_google_busy_lee_freebusy():
    svc = _FakeGoogle({"calendars": {"cal-alex": {"busy": [
        {"start": "2026-10-08T15:00:00Z", "end": "2026-10-08T15:30:00Z"}]}}})
    busy = GoogleCalendar(svc).busy("cal-alex", at(8, 0), at(9, 0))
    assert busy == [(at(8, 10), at(8, 10, 30))]  # 15:00Z = 10:00 en Quito


def test_google_error_de_calendario_no_se_toma_como_libre():
    svc = _FakeGoogle({"calendars": {"cal-alex": {"errors": [{"reason": "notFound"}]}}})
    with pytest.raises(CalendarError):
        GoogleCalendar(svc).busy("cal-alex", at(8, 0), at(9, 0))


def test_google_crear_mover_borrar():
    svc = _FakeGoogle({})
    g = GoogleCalendar(svc)
    assert g.create_event("cal-alex", at(8, 10), at(8, 10, 30), "Corte - Ana", "d", {"wa_id": "A"}) == "gevt1"
    g.move_event("cal-alex", "gevt1", at(9, 11), at(9, 11, 30))
    g.delete_event("cal-alex", "gevt1")
    insert = svc.calls[0]
    assert insert[2]["start"]["dateTime"] == "2026-10-08T10:00:00-05:00"
    assert insert[2]["extendedProperties"]["private"] == {"wa_id": "A"}
    assert [c[0] for c in svc.calls] == ["insert", "patch", "delete"]


# --- API y validaciones -------------------------------------------------------------------------
def test_api_chat_y_negocio_desconocido(engine):
    client = TestClient(create_app(engine))
    r = client.post("/chat", json={"business_id": "barberia", "wa_id": "A", "text": "corte el jueves"})
    assert r.status_code == 200 and "jueves 8 de octubre" in r.json()["text"]
    assert client.post("/chat", json={"business_id": "nope", "wa_id": "A", "text": "hola"}).status_code == 404
    assert client.post("/chat", json={"business_id": "barberia", "wa_id": "", "text": "hola"}).status_code == 422


def test_aviso_de_privacidad_solo_en_el_primer_mensaje(cal):
    data = dict(BARBERIA, privacy_notice="Uso tus datos solo para tus citas.")
    eng = Engine({"barberia": business_from_dict(data)}, Store(), cal, RuleNLU(), clock=Clock())
    assert eng.handle_message("barberia", "A", "hola").text.startswith("Uso tus datos")
    assert not eng.handle_message("barberia", "A", "hola").text.startswith("Uso tus datos")


def test_remitente_vacio_se_rechaza(engine):
    with pytest.raises(ValueError):
        engine.handle_message("barberia", "  ", "hola")
