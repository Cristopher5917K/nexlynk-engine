"""Persistencia (SQLite). Fix de J08: toda lectura/escritura exige (business_id, wa_id).

No existe ningún método que lea una sesión o una cita sin ambas claves, así que una conversación
no puede ver el estado de otra aunque el código del motor tenga un bug.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    business_id TEXT NOT NULL,
    wa_id       TEXT NOT NULL,
    data        TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (business_id, wa_id)
);
CREATE TABLE IF NOT EXISTS appointments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id  TEXT NOT NULL,
    wa_id        TEXT NOT NULL,
    professional_id TEXT NOT NULL,
    service_id   TEXT NOT NULL,
    start        TEXT NOT NULL,
    end          TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'confirmed'
);
CREATE INDEX IF NOT EXISTS idx_appt_owner ON appointments (business_id, wa_id, status, start);
CREATE TABLE IF NOT EXISTS customers (
    business_id TEXT NOT NULL,
    wa_id       TEXT NOT NULL,
    name        TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (business_id, wa_id)
);
CREATE TABLE IF NOT EXISTS processed_messages (
    business_id TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (business_id, message_id)
);
"""


@dataclass(frozen=True)
class Appointment:
    id: int
    business_id: str
    wa_id: str
    professional_id: str
    service_id: str
    start: datetime
    end: datetime
    event_id: str
    status: str


def _row_to_appt(r: sqlite3.Row) -> Appointment:
    return Appointment(
        id=r["id"], business_id=r["business_id"], wa_id=r["wa_id"],
        professional_id=r["professional_id"], service_id=r["service_id"],
        start=datetime.fromisoformat(r["start"]), end=datetime.fromisoformat(r["end"]),
        event_id=r["event_id"], status=r["status"],
    )


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    # --- sesiones -----------------------------------------------------------------------------
    def get_session(self, business_id: str, wa_id: str) -> dict:
        with self._lock:
            row = self._db.execute(
                "SELECT data FROM sessions WHERE business_id = ? AND wa_id = ?", (business_id, wa_id)
            ).fetchone()
        return json.loads(row["data"]) if row else {}

    def save_session(self, business_id: str, wa_id: str, data: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO sessions (business_id, wa_id, data, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (business_id, wa_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
                (business_id, wa_id, json.dumps(data), datetime.now().isoformat()),
            )
            self._db.commit()

    # --- clientes -----------------------------------------------------------------------------
    def ensure_customer(self, business_id: str, wa_id: str) -> bool:
        """Registra al cliente si es nuevo. Devuelve True la primera vez (para el aviso de privacidad)."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO customers (business_id, wa_id, created_at) VALUES (?, ?, ?)",
                (business_id, wa_id, datetime.now().isoformat()),
            )
            self._db.commit()
            return cur.rowcount == 1

    def customer_name(self, business_id: str, wa_id: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT name FROM customers WHERE business_id = ? AND wa_id = ?", (business_id, wa_id)
            ).fetchone()
        return row["name"] if row else None

    def set_customer_name(self, business_id: str, wa_id: str, name: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO customers (business_id, wa_id, name, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (business_id, wa_id) DO UPDATE SET name = excluded.name",
                (business_id, wa_id, name, datetime.now().isoformat()),
            )
            self._db.commit()

    # --- idempotencia ---------------------------------------------------------------------------
    def mark_message(self, business_id: str, message_id: str) -> bool:
        """True si el mensaje es nuevo; False si ya se procesó (reintento de WhatsApp)."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO processed_messages (business_id, message_id, received_at) VALUES (?, ?, ?)",
                (business_id, message_id, datetime.now().isoformat()),
            )
            self._db.commit()
            return cur.rowcount == 1

    # --- citas --------------------------------------------------------------------------------
    def add_appointment(self, business_id, wa_id, professional_id, service_id, start, end, event_id) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO appointments (business_id, wa_id, professional_id, service_id, start, end, event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (business_id, wa_id, professional_id, service_id, start.isoformat(), end.isoformat(), event_id),
            )
            self._db.commit()
            return cur.lastrowid

    def upcoming_appointments(self, business_id: str, wa_id: str, now: datetime) -> list[Appointment]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM appointments WHERE business_id = ? AND wa_id = ? AND status = 'confirmed' ORDER BY start",
                (business_id, wa_id),
            ).fetchall()
        # se compara en Python: los ISO con distinto offset no ordenan bien como texto
        return [a for a in map(_row_to_appt, rows) if a.end > now]

    def get_appointment(self, business_id: str, wa_id: str, appt_id: int) -> Appointment | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM appointments WHERE id = ? AND business_id = ? AND wa_id = ? AND status = 'confirmed'",
                (appt_id, business_id, wa_id),
            ).fetchone()
        return _row_to_appt(row) if row else None

    def all_appointments(self, business_id: str) -> list[Appointment]:
        """Todas las citas del negocio (también canceladas), para el panel o la exportación."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM appointments WHERE business_id = ? ORDER BY id", (business_id,)
            ).fetchall()
        return [_row_to_appt(r) for r in rows]

    def update_appointment(self, business_id, wa_id, appt_id, professional_id, service_id, start, end, event_id) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE appointments SET professional_id = ?, service_id = ?, start = ?, end = ?, event_id = ? "
                "WHERE id = ? AND business_id = ? AND wa_id = ?",
                (professional_id, service_id, start.isoformat(), end.isoformat(), event_id, appt_id, business_id, wa_id),
            )
            self._db.commit()

    def cancel_appointment(self, business_id: str, wa_id: str, appt_id: int) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE appointments SET status = 'cancelled' WHERE id = ? AND business_id = ? AND wa_id = ?",
                (appt_id, business_id, wa_id),
            )
            self._db.commit()
