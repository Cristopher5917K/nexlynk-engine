"""API HTTP del motor. Por ahora solo el endpoint de simulador (/chat); el webhook de WhatsApp se
conecta después, cuando se defina el número."""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .calendar_backend import GoogleCalendar, InMemoryCalendar
from .engine import Engine
from .models import load_businesses
from .nlu import ClaudeNLU, FallbackNLU, RuleNLU
from .store import Store

log = logging.getLogger("nexlynk")


class ChatIn(BaseModel):
    business_id: str
    wa_id: str = Field(min_length=1)
    text: str = Field(max_length=1000)
    message_id: str | None = None  # id de WhatsApp: evita procesar dos veces un reintento


class ChatOut(BaseModel):
    text: str
    buttons: list[str]


def build_engine() -> Engine:
    businesses = load_businesses(os.environ.get("NEXLYNK_BUSINESSES_DIR", "businesses"))
    store = Store(os.environ.get("NEXLYNK_DB", "nexlynk.db"))

    if os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE"):
        calendar = GoogleCalendar.from_env()
    else:
        log.warning("GOOGLE_SERVICE_ACCOUNT_FILE no definido: usando calendario EN MEMORIA (solo demo)")
        calendar = InMemoryCalendar()

    if os.environ.get("ANTHROPIC_API_KEY"):
        nlu = FallbackNLU(ClaudeNLU(model=os.environ.get("NEXLYNK_MODEL", "claude-haiku-4-5-20251001")), RuleNLU())
    else:
        log.warning("ANTHROPIC_API_KEY no definido: usando comprensión por reglas")
        nlu = RuleNLU()
    return Engine(businesses, store, calendar, nlu)


def create_app(engine: Engine | None = None) -> FastAPI:
    app = FastAPI(title="Nexlynk Engine")
    eng = engine or build_engine()

    @app.get("/health")
    def health():
        return {"ok": True, "businesses": sorted(eng.businesses)}

    @app.post("/chat", response_model=ChatOut)
    def chat(body: ChatIn):
        try:
            reply = eng.handle_message(body.business_id, body.wa_id, body.text, body.message_id)
        except KeyError:
            raise HTTPException(404, "negocio desconocido")
        return ChatOut(text=reply.text, buttons=reply.buttons)

    return app
