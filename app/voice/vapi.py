"""Integrazione con Vapi (https://vapi.ai): telefonia, voce e trascrizione.

Vapi gestisce la chiamata (numero Twilio/Vapi, riconoscimento vocale, sintesi vocale)
e usa Claude come "cervello" dell'agente. Quando Claude usa uno strumento, Vapi
chiama il nostro endpoint /webhooks/vapi e noi rispondiamo con il risultato.
"""

import json
import logging

import httpx

from .. import db
from ..config import settings
from . import prompt
from .tools import TOOL_DEFINITIONS

log = logging.getLogger(__name__)

VAPI_URL = "https://api.vapi.ai"

# endedReason di Vapi che indicano che il lead non ha risposto (da richiamare)
NO_ANSWER_HINTS = ("did-not-answer", "busy", "voicemail", "failed-to-connect", "no-answer", "silence-timed-out")


class CallStartError(Exception):
    pass


def build_assistant(lead: dict) -> dict:
    """Configurazione completa dell'assistente per questa chiamata."""
    voice = {"provider": settings.voice_provider_name}
    if settings.voice_id:
        voice["voiceId"] = settings.voice_id
    return {
        "name": "Agente consulenze",
        "firstMessage": prompt.first_message(lead),
        "model": {
            "provider": "anthropic",
            "model": settings.llm_model,
            "messages": [{"role": "system", "content": prompt.system_prompt(lead)}],
            "tools": [
                {"type": "function", "function": t} for t in TOOL_DEFINITIONS
            ] + [{"type": "endCall"}],
        },
        "voice": voice,
        "transcriber": {"provider": "deepgram", "model": "nova-2", "language": settings.transcriber_language},
        "server": {
            "url": f"{settings.public_base_url}/webhooks/vapi",
            "headers": {"x-vapi-secret": settings.vapi_server_secret},
        },
        "serverMessages": ["tool-calls", "end-of-call-report", "status-update"],
        "voicemailDetection": {"provider": "vapi"},
        "maxDurationSeconds": 600,
        "silenceTimeoutSeconds": 30,
        # Evita che Vittoria si interrompa per rumori, colpi di tosse o "sì/ok" di sottofondo:
        # smette di parlare solo se la persona dice almeno 2 parole
        "stopSpeakingPlan": {"numWords": 2, "voiceSeconds": 0.3, "backoffSeconds": 1},
        "backgroundDenoisingEnabled": True,
        "metadata": {"lead_id": lead["id"]},
    }


def start_call(lead: dict) -> str:
    """Avvia la chiamata in uscita e restituisce l'id della chiamata."""
    if settings.voice_provider == "dry_run":
        call_id = f"dry-run-{lead['id']}-{db.now_utc()}"
        log.info("[dry_run] chiamerei %s (%s): %s", lead["telefono"], lead.get("nome"), prompt.first_message(lead))
        return call_id
    if not (settings.vapi_api_key and settings.vapi_phone_number_id):
        raise CallStartError("VAPI_API_KEY o VAPI_PHONE_NUMBER_ID non configurati")

    customer = {"number": lead["telefono"]}
    if lead.get("nome"):
        customer["name"] = lead["nome"][:40]
    payload = {
        "phoneNumberId": settings.vapi_phone_number_id,
        "customer": customer,
        "assistant": build_assistant(lead),
        "metadata": {"lead_id": lead["id"]},
    }
    try:
        r = httpx.post(
            f"{VAPI_URL}/call",
            json=payload,
            headers={"Authorization": f"Bearer {settings.vapi_api_key}"},
            timeout=30,
        )
    except httpx.HTTPError as e:
        raise CallStartError(f"Errore di rete verso Vapi: {e}") from e
    if r.status_code >= 300:
        raise CallStartError(f"Vapi ha risposto {r.status_code}: {r.text[:500]}")
    return r.json()["id"]


def parse_tool_arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def lead_id_for_call(message: dict) -> int | None:
    """Trova il lead associato a un evento Vapi (via id chiamata o metadata)."""
    call = message.get("call") or {}
    call_id = call.get("id")
    if call_id:
        with db.tx() as c:
            row = c.execute("SELECT lead_id FROM calls WHERE provider_call_id = ?", (call_id,)).fetchone()
        if row:
            return row["lead_id"]
    for meta in (call.get("metadata"), (call.get("assistant") or {}).get("metadata"), message.get("metadata")):
        if isinstance(meta, dict) and meta.get("lead_id") is not None:
            try:
                return int(meta["lead_id"])
            except (TypeError, ValueError):
                pass
    return None


def is_no_answer(ended_reason: str | None) -> bool:
    reason = (ended_reason or "").lower()
    return any(h in reason for h in NO_ANSWER_HINTS)
