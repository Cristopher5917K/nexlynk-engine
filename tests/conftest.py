import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from nexlynk.calendar_backend import InMemoryCalendar
from nexlynk.engine import Engine
from nexlynk.models import business_from_dict
from nexlynk.nlu import RuleNLU
from nexlynk.store import Store

TZ = ZoneInfo("America/Guayaquil")
ROOT = Path(__file__).resolve().parent.parent

# Configuración de la demo de Julián (Drive: "Requerimientos para Claude", 2026-10-06), sin aviso
# de privacidad para que las aserciones lean la respuesta del bot sin prefijo.
BARBERIA = json.loads((ROOT / "businesses" / "barberia_demo.json").read_text(encoding="utf-8"))
BARBERIA["id"] = "barberia"
BARBERIA["privacy_notice"] = None
for p in BARBERIA["professionals"]:
    p["calendar_id"] = f"cal-{p['id']}"

# Otro rubro con la misma plataforma: una sola profesional y un solo servicio
VET = {
    "id": "vet",
    "name": "Vet Test",
    "timezone": "America/Guayaquil",
    "hours": {d: ["08:00", "12:00"] for d in ("mon", "tue", "wed", "thu", "fri")} | {"sat": None, "sun": None},
    "services": [{"id": "consulta", "name": "Consulta", "minutes": 30, "price": 25, "aliases": ["consulta", "revision"]}],
    "professionals": [{"id": "dra", "name": "Dra. Paz", "calendar_id": "cal-vet"}],
}


class Clock:
    """Reloj congelado de la matriz: martes 2026-10-06 10:00, America/Guayaquil (UTC-5)."""

    def __init__(self):
        self.now = datetime(2026, 10, 6, 10, 0, tzinfo=TZ)

    def __call__(self):
        return self.now


@pytest.fixture
def cal():
    return InMemoryCalendar()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def handoffs():
    return []


@pytest.fixture
def engine(cal, clock, handoffs):
    return Engine(
        {b["id"]: business_from_dict(b) for b in (BARBERIA, VET)},
        Store(":memory:"), cal, RuleNLU(), clock=clock,
        on_handoff=lambda b, w, t: handoffs.append((b, w, t)),
    )


def say(engine, user, text, business="barberia", message_id=None):
    return engine.handle_message(business, user, text, message_id)


def session(engine, user, business="barberia"):
    return engine.store.get_session(business, user)


def book(engine, user, text, name="Julian", business="barberia"):
    """Agenda de punta a punta con un mensaje que trae servicio, día y hora exactos."""
    r = say(engine, user, text, business)
    if session(engine, user, business).get("state") == "name":
        r = say(engine, user, name, business)
    assert session(engine, user, business).get("state") == "confirm", r.text
    return say(engine, user, "sí", business)
