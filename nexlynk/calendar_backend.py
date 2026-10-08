"""Acceso al calendario. Reemplaza los webhooks de Make por llamadas directas a Google Calendar."""
from __future__ import annotations

import itertools
import os
from datetime import datetime
from typing import Protocol

Interval = tuple[datetime, datetime]


class CalendarError(Exception):
    """El calendario no respondió o rechazó la operación. El motor nunca confirma una cita si esto ocurre."""


class CalendarBackend(Protocol):
    def busy(self, calendar_id: str, start: datetime, end: datetime) -> list[Interval]: ...

    def create_event(
        self,
        calendar_id: str,
        start: datetime,
        end: datetime,
        summary: str,
        description: str,
        private: dict[str, str],
    ) -> str: ...

    def move_event(self, calendar_id: str, event_id: str, start: datetime, end: datetime) -> None: ...

    def delete_event(self, calendar_id: str, event_id: str) -> None: ...


class InMemoryCalendar:
    """Calendario falso para pruebas y para la demo sin credenciales."""

    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self._ids = itertools.count(1)
        self.fail = False  # las pruebas lo activan para simular caídas

    def _check(self) -> None:
        if self.fail:
            raise CalendarError("calendario no disponible (simulado)")

    def busy(self, calendar_id, start, end):
        self._check()
        return [
            (e["start"], e["end"])
            for e in self.events.values()
            if e["calendar_id"] == calendar_id and e["start"] < end and e["end"] > start
        ]

    def create_event(self, calendar_id, start, end, summary, description, private):
        self._check()
        event_id = f"evt{next(self._ids)}"
        self.events[event_id] = dict(
            calendar_id=calendar_id, start=start, end=end, summary=summary,
            description=description, private=dict(private),
        )
        return event_id

    def move_event(self, calendar_id, event_id, start, end):
        self._check()
        ev = self.events.get(event_id)
        if ev is None or ev["calendar_id"] != calendar_id:
            raise CalendarError(f"evento {event_id} no existe")
        ev["start"], ev["end"] = start, end

    def delete_event(self, calendar_id, event_id):
        self._check()
        ev = self.events.get(event_id)
        if ev is None or ev["calendar_id"] != calendar_id:
            raise CalendarError(f"evento {event_id} no existe")
        del self.events[event_id]


class GoogleCalendar:
    """Google Calendar API v3 con cuenta de servicio.

    Cada calendario (uno por profesional/sede) debe compartirse con el correo de la cuenta de
    servicio con permiso "Hacer cambios en eventos".
    """

    SCOPES = ["https://www.googleapis.com/auth/calendar"]

    def __init__(self, service) -> None:
        self._svc = service

    @classmethod
    def from_service_account_file(cls, path: str) -> "GoogleCalendar":
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(path, scopes=cls.SCOPES)
        return cls(build("calendar", "v3", credentials=creds, cache_discovery=False))

    @classmethod
    def from_env(cls) -> "GoogleCalendar":
        return cls.from_service_account_file(os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"])

    def _run(self, request):
        from googleapiclient.errors import HttpError

        try:
            return request.execute()
        except HttpError as exc:
            raise CalendarError(f"Google Calendar rechazó la operación: {exc}") from exc
        except OSError as exc:  # red, timeouts, SSL
            raise CalendarError(f"No se pudo contactar a Google Calendar: {exc}") from exc

    def busy(self, calendar_id, start, end):
        body = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "items": [{"id": calendar_id}]}
        data = self._run(self._svc.freebusy().query(body=body))
        cal = data.get("calendars", {}).get(calendar_id, {})
        if cal.get("errors"):
            # calendario no compartido con la cuenta de servicio, id equivocado, etc.
            raise CalendarError(f"freebusy falló para {calendar_id}: {cal['errors']}")
        return [
            (datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"]))
            for b in cal.get("busy", [])
        ]

    def create_event(self, calendar_id, start, end, summary, description, private):
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
            "extendedProperties": {"private": private},
        }
        event = self._run(self._svc.events().insert(calendarId=calendar_id, body=body))
        return event["id"]

    def move_event(self, calendar_id, event_id, start, end):
        body = {"start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}
        self._run(self._svc.events().patch(calendarId=calendar_id, eventId=event_id, body=body))

    def delete_event(self, calendar_id, event_id):
        self._run(self._svc.events().delete(calendarId=calendar_id, eventId=event_id))
