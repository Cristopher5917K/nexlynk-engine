"""Exportación de citas a Excel (EX01). Es una foto completa: se regenera entera, nunca duplica filas."""
from __future__ import annotations

from pathlib import Path

from .models import Business
from .store import Store

HEADERS = ["cita_id", "negocio", "wa_id", "nombre", "servicio", "profesional", "inicio", "fin", "zona", "estado"]


def safe_cell(value) -> str:
    """Evita inyección de fórmulas: Excel ejecuta celdas que empiezan con = + - @ (o tab/CR)."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def appointment_rows(store: Store, biz: Business) -> list[list[str]]:
    rows = []
    for a in store.all_appointments(biz.id):
        service = biz.service(a.service_id)
        pro = biz.professional(a.professional_id)
        rows.append([safe_cell(v) for v in (
            a.id, biz.id, a.wa_id, store.customer_name(biz.id, a.wa_id),
            service.name if service else a.service_id, pro.name if pro else a.professional_id,
            a.start.astimezone(biz.tz).strftime("%Y-%m-%d %H:%M"),
            a.end.astimezone(biz.tz).strftime("%Y-%m-%d %H:%M"),
            biz.timezone, a.status,
        )])
    return rows


def export_xlsx(store: Store, biz: Business, path: str | Path) -> int:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Citas"
    ws.append(HEADERS)
    rows = appointment_rows(store, biz)
    for row in rows:
        ws.append(row)  # todo como texto: conserva ceros iniciales y el "+" de los teléfonos
    wb.save(path)
    return len(rows)
