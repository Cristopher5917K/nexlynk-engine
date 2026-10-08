"""Los 17 casos de Drive: "Matriz_pruebas_Nexlynk" (Julián, 2026-10-06), uno por prueba.

Reloj: martes 2026-10-06 10:00 America/Guayaquil → miércoles 7, jueves 8, viernes 9 de octubre.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

from nexlynk.export import HEADERS, export_xlsx, safe_cell

from .conftest import book, say, session

TZ = ZoneInfo("America/Guayaquil")


def offered(engine, user):
    return [(datetime.fromisoformat(o["start"]).astimezone(TZ), o["pro"]) for o in session(engine, user)["offered"]]


# --- J01 Lenguaje y tono: errata "jueve" y cambio a viernes sin perder el servicio ---------------
def test_J01_errata_y_cambio_de_dia_conserva_servicio(engine):
    r = say(engine, "A", "tienes turnos disponibles para el jueve")
    assert "servicio" in r.text
    assert session(engine, "A")["draft"]["date"] == "2026-10-08"  # "jueve" se entendió como jueves

    r = say(engine, "A", "corte")
    assert "jueves 8 de octubre" in r.text

    r = say(engine, "A", "viernes?")
    draft = session(engine, "A")["draft"]
    assert draft["service"] == "corte" and draft["date"] == "2026-10-09"
    assert "viernes 9 de octubre" in r.text
    assert "reiniciar" not in r.text.lower()


# --- J02 Intenciones durante cada paso: nueva preferencia mientras elige horario ------------------
def test_J02_nueva_preferencia_en_pick_slot_reemplaza_sin_reiniciar(engine):
    say(engine, "A", "quiero un combo el viernes")
    assert session(engine, "A")["state"] == "pick_slot"

    r = say(engine, "A", "Quiero un corte mañana")
    s = session(engine, "A")
    assert s["state"] == "pick_slot"
    assert s["draft"]["service"] == "corte" and s["draft"]["date"] == "2026-10-07"
    assert "miércoles 7 de octubre" in r.text
    assert "reiniciar" not in r.text.lower()


# --- J03 Selección y búsqueda: "más opciones" no repite --------------------------------------------
def test_J03_mas_opciones_no_repite(engine):
    say(engine, "A", "corte el jueves")
    first = offered(engine, "A")
    say(engine, "A", "más opciones")
    second = offered(engine, "A")
    assert second and not set(first) & set(second)
    assert min(t for t, _ in second) > max(t for t, _ in first)


# --- J04 Gestión de citas: una sola cita, sin paso numérico redundante -----------------------------
def test_J04_cambiar_cita_unica_no_pide_numero_ni_modifica(engine, cal):
    book(engine, "A", "corte el jueves a las 10")
    (event_before,) = cal.events.values()
    start_before = event_before["start"]

    r = say(engine, "A", "Quiero cambiar mi cita")
    assert "varias citas" not in r.text
    assert "jueves 8 de octubre, 10:00" in r.text  # reconoce la cita
    assert "todavía no la he cambiado" in r.text.lower()
    assert session(engine, "A")["mode"] == "reschedule"
    (event_after,) = cal.events.values()
    assert event_after["start"] == start_before  # nada se modificó aún


# --- J05 Intenciones durante cada paso: "quiero cambiar mi cita" en la confirmación ---------------
def test_J05_cambiar_en_confirmacion_reabre_borrador(engine, cal):
    say(engine, "A", "corte el jueves a las 10")
    say(engine, "A", "Julian")
    assert session(engine, "A")["state"] == "confirm"

    r = say(engine, "A", "Quiero cambiar mi cita")
    assert "todavía no he agendado" in r.text.lower()
    assert not cal.events  # no se tomó como "sí"
    assert session(engine, "A")["draft"]["service"] == "corte"

    r = say(engine, "A", "mejor a las 11")
    assert session(engine, "A")["state"] == "confirm"
    assert "11:00" in r.text and "Corte" in r.text


# --- J06 Gestión de citas: al mover, no ofrecer la hora original -----------------------------------
def test_J06_mover_cita_excluye_hora_original(engine):
    book(engine, "A", "corte hoy a las 4")
    say(engine, "A", "Quiero mover mi cita de hoy a las 16:00 a otra hora")
    opts = offered(engine, "A")
    assert opts
    assert all(t.strftime("%H:%M") != "16:00" for t, _ in opts)
    assert all(t.date().isoformat() == "2026-10-06" for t, _ in opts)  # "otra hora" = mismo día


# --- J07 Selección y búsqueda: "la segunda" y ambigüedad ------------------------------------------
def test_J07_la_segunda_elige_la_opcion_2(engine):
    say(engine, "A", "corte el jueves")
    opts = offered(engine, "A")
    say(engine, "A", "la segunda")
    chosen = session(engine, "A")["chosen"]
    assert datetime.fromisoformat(chosen["start"]) == opts[1][0] and chosen["pro"] == opts[1][1]


def test_J07_la_de_alex_ambigua_pregunta_en_vez_de_adivinar(engine):
    say(engine, "A", "corte el jueves")
    assert all(p == "alex" for _, p in offered(engine, "A"))
    r = say(engine, "A", "la de Alex")
    s = session(engine, "A")
    assert s["state"] == "pick_slot" and s["chosen"] is None
    assert "opciones con Alex" in r.text

    say(engine, "A", "con Dani")  # nadie con Dani entre las opciones: busca con Dani
    assert all(p == "dani" for _, p in offered(engine, "A"))


# --- J08 Identidad y privacidad: dos clientes intercalados ----------------------------------------
def test_J08_clientes_intercalados_no_se_mezclan(engine):
    say(engine, "593111", "Hola, quiero un corte para hoy")
    r_b = say(engine, "593222", "tienen espacio mañana?")
    assert "servicio" in r_b.text  # B no hereda el "corte" de A
    say(engine, "593111", "a las 3")

    a, b = session(engine, "593111"), session(engine, "593222")
    assert a["draft"] == {"service": "corte", "date": "2026-10-06", "time": "15:00", "professional": "any"}
    assert b["draft"] == {"date": "2026-10-07"}

    say(engine, "593111", "Julian")
    say(engine, "593111", "sí")
    r = say(engine, "593222", "mis citas")
    assert "No tienes citas" in r.text  # B no ve la cita de A
    r = say(engine, "593222", "cancelar mi cita")
    assert "No tienes citas" in r.text


def test_J08_mismo_numero_en_dos_negocios_esta_aislado(engine):
    say(engine, "593111", "corte el jueves", business="barberia")
    r = say(engine, "593111", "a las 9", business="vet")
    assert session(engine, "593111", "vet").get("draft", {}).get("service") is None
    assert session(engine, "593111", "barberia")["draft"]["service"] == "corte"
    assert "Corte" not in r.text


# --- LT01 Lenguaje y tono: jerga ------------------------------------------------------------------
def test_LT01_jerga_responde_horario_de_hoy_y_pregunta_servicio(engine):
    r = say(engine, "A", "qtl bro a q hr atiendn oyyy")
    assert "Hoy martes atendemos de 09:00 a 13:00 y 14:00 a 18:00" in r.text
    assert "servicio" in r.text
    assert "ortograf" not in r.text.lower()


# --- FH01 Fechas y horas: "mañana" a las 23:59 -----------------------------------------------------
def test_FH01_manana_a_las_2359_es_el_dia_siguiente(engine, clock):
    clock.now = datetime(2026, 10, 6, 23, 59, tzinfo=TZ)
    say(engine, "A", "turno para mañana")
    assert session(engine, "A")["draft"]["date"] == "2026-10-07"
    say(engine, "A", "corte")
    assert {t.date().isoformat() for t, _ in offered(engine, "A")} == {"2026-10-07"}


# --- IF01 Información: precio y duración de un servicio --------------------------------------------
def test_IF01_precio_y_duracion_sin_recitar_catalogo(engine):
    r = say(engine, "A", "cuanto vale el corte y q tiempo demora")
    assert "$12" in r.text and "30 min" in r.text
    assert "agendar" in r.text.lower()
    assert "Barba" not in r.text and "combo" not in r.text.lower()

    r = say(engine, "A", "sí")  # acepta agendar: el servicio ya queda elegido
    assert session(engine, "A")["draft"]["service"] == "corte"
    assert "día" in r.text


# --- CX01 Contexto: todos los datos en un turno ---------------------------------------------------
def test_CX01_varios_datos_en_un_turno_van_directo_a_confirmar(engine):
    r = say(engine, "A", "corte con alex mañana a las 3 me llamo juan")
    assert session(engine, "A")["state"] == "confirm"
    for part in ("Corte", "Alex", "miércoles 7 de octubre, 15:00", "Juan"):
        assert part in r.text
    assert "a nombre de quién" not in r.text.lower()


# --- RS01 Reserva: "sí, pero sin barba" ------------------------------------------------------------
def test_RS01_si_pero_sin_barba_recalcula_y_reconfirma(engine, cal):
    say(engine, "A", "combo el jueves a las 10")
    r = say(engine, "A", "Julian")
    assert "$18" in r.text

    r = say(engine, "A", "sí, pero sin barba")
    s = session(engine, "A")
    assert s["state"] == "confirm" and s["draft"]["service"] == "corte"
    assert "Corte ($12, 30 min)" in r.text
    assert not cal.events  # no confirmó la cita original


# --- AG01 Agenda: hora de almuerzo -----------------------------------------------------------------
def test_AG01_almuerzo_no_se_agenda_y_sugiere_alternativas(engine):
    say(engine, "A", "corte el jueves")
    r = say(engine, "A", "a las 13:30")
    assert "13:30 no tengo espacio" in r.text
    s = session(engine, "A")
    assert s["state"] == "pick_slot"
    assert all(not ("13:00" <= t.strftime("%H:%M") < "14:00") for t, _ in offered(engine, "A"))


# --- CE01 Concurrencia e idempotencia: doble "sí" ------------------------------------------------
def test_CE01_doble_si_crea_una_sola_cita(engine, cal):
    say(engine, "A", "corte el jueves a las 10")
    say(engine, "A", "Julian")
    say(engine, "A", "sí")
    r = say(engine, "A", "sí")
    assert len(cal.events) == 1
    assert "ya quedó confirmada" in r.text
    assert len(engine.store.all_appointments("barberia")) == 1


def test_CE01_reintento_del_mismo_mensaje_de_whatsapp_se_ignora(engine, cal):
    say(engine, "A", "corte el jueves a las 10", message_id="m1")
    say(engine, "A", "Julian", message_id="m2")
    say(engine, "A", "sí", message_id="m3")
    r = say(engine, "A", "sí", message_id="m3")  # Meta reenvía el mismo evento
    assert r.text == ""
    assert len(cal.events) == 1


# --- HU01 Humano ---------------------------------------------------------------------------------
def test_HU01_pide_persona_pausa_el_bot_y_avisa(engine, handoffs):
    say(engine, "A", "corte el jueves")
    r = say(engine, "A", "estoy harto pasame con una persona")
    assert "persona del equipo" in r.text
    assert handoffs == [("barberia", "A", "estoy harto pasame con una persona")]
    assert say(engine, "A", "hola??").text == ""  # no sigue intentando agendar

    engine.resume("barberia", "A")
    assert "asistente virtual" in say(engine, "A", "hola").text


# --- EX01 Exportación ------------------------------------------------------------------------------
def test_EX01_exporta_sin_duplicar_y_sin_formulas(engine, tmp_path):
    book(engine, "+593111", "corte el jueves a las 10")
    book(engine, "593222", "barba el viernes a las 11", name="Ana")
    say(engine, "+593111", "reprogramar para el viernes a las 15:00")
    say(engine, "+593111", "sí")

    path = tmp_path / "citas.xlsx"
    assert export_xlsx(engine.store, engine.businesses["barberia"], path) == 2
    export_xlsx(engine.store, engine.businesses["barberia"], path)  # exportar otra vez no duplica
    rows = list(load_workbook(path).active.values)
    assert list(rows[0]) == HEADERS and len(rows) == 3
    assert rows[1][2] == "'+593111"  # un "+" inicial no se interpreta como fórmula
    assert rows[1][6] == "2026-10-09 15:00"  # refleja la reprogramación en la misma fila
    assert safe_cell("=HYPERLINK(\"x\")").startswith("'=")
    assert safe_cell("@SUM(A1)") == "'@SUM(A1)"
