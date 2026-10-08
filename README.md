# Nexlynk Engine: motor de agendamiento por WhatsApp

Motor conversacional para agendar, reprogramar y cancelar citas. Un mismo despliegue atiende a
varios negocios; cada negocio se describe en un JSON dentro de `businesses/`.

Estado: **piloto local**. Sin WhatsApp real, sin calendario real conectado y sin IA real probada.

## Ejecutar

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python -m pytest -q          # pruebas (40)
.venv/bin/python -m nexlynk.cli        # simulador de conversación en la consola
.venv/bin/uvicorn "nexlynk.app:create_app" --factory   # API: POST /chat
```

Sin variables de entorno usa un calendario **en memoria** y comprensión **por reglas** (gratis y sin red).

| Variable | Efecto |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Usa Google Calendar real (cuenta de servicio; compartir cada calendario con su correo) |
| `ANTHROPIC_API_KEY` | Usa Claude Haiku para entender mensajes; si falla, cae a reglas |
| `NEXLYNK_DB` | Archivo SQLite (por defecto `nexlynk.db`) |
| `NEXLYNK_BUSINESSES_DIR` | Carpeta de configuraciones (por defecto `businesses/`) |

## Cómo está organizado

| Archivo | Qué hace |
|---|---|
| `nexlynk/models.py` | Configuración por negocio: servicios, combos, profesionales, franjas (almuerzo), buffer |
| `nexlynk/store.py` | SQLite. **Todo exige (negocio, teléfono)**: una conversación no puede leer otra (J08) |
| `nexlynk/calendar_backend.py` | Google Calendar directo (reemplaza a Make) y un calendario en memoria para pruebas |
| `nexlynk/availability.py` | Horarios libres: franjas del negocio menos lo ocupado, con buffer y anticipación |
| `nexlynk/nlu.py` | Interpreta el mensaje (reglas o Claude). Solo interpreta: todo se valida contra el catálogo |
| `nexlynk/engine.py` | Conversación: primero la intención, después el estado; cambios parciales del borrador |
| `nexlynk/messages.py` | Todos los textos del bot (para ajustar el tono sin tocar la lógica) |
| `nexlynk/export.py` | Exportación a Excel sin duplicar filas y sin inyección de fórmulas |
| `tests/test_matriz.py` | Los 17 casos de "Matriz_pruebas_Nexlynk" (Drive), uno por prueba |
| `tests/test_core.py` | Doble reserva, caída del calendario, cancelar/reprogramar, horarios, Google, API |

## Garantías que cubren las pruebas

- Dos clientes intercalados no comparten datos; el mismo número en dos negocios tampoco.
- Un cliente no puede ver, cancelar ni mover citas de otro.
- Dos clientes no pueden quedarse con el mismo hueco (se revisa en vivo justo antes de escribir).
- Si el calendario falla, no se confirma nada y la conversación no pierde su estado.
- Un "sí" repetido o un reintento del mismo mensaje de WhatsApp no crea dos citas.
- Ningún horario ofrecido cae en el almuerzo, fuera de horario o sin los 10 min de margen.

## Pendiente

- Webhook de WhatsApp Cloud API (deliberadamente fuera de esta etapa).
- Probar `ClaudeNLU` con una API key real: hoy solo están probadas las reglas.
- Probar `GoogleCalendar` contra Google real: hoy se prueba con un servicio falso.
- Recordatorios, consentimiento de avisos, propuestas de cambio cuando el negocio se retrasa, panel.
- Bloqueo de reservas entre varios servidores (hoy protege dentro de un solo proceso).
- Módulos Catálogo/Cotizador y General separados del de Reservas (ver la comparación de motores).
- Las reglas no cubren todo el español informal; esa es la tarea del modo con Claude.
