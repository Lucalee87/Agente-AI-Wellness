"""Google Ads: moduli per i lead (lead form asset) con consegna via webhook.

Nel modulo Google Ads si imposta l'URL del webhook e una "chiave": Google la
rimanda nel campo `google_key` di ogni lead, e noi la confrontiamo con GOOGLE_LEAD_KEY.
"""

import hmac

from .. import centers
from ..config import settings
from ..leads import LeadIn
from .meta import _match_centro


def verify_key(payload: dict) -> bool:
    key = str(payload.get("google_key") or "")
    return bool(settings.google_lead_key) and hmac.compare_digest(key, settings.google_lead_key)


def to_lead(payload: dict) -> LeadIn:
    cols: dict[str, str] = {}
    for col in payload.get("user_column_data", []):
        key = (col.get("column_id") or col.get("column_name") or "").upper()
        cols[key] = col.get("string_value") or ""

    nome = cols.get("FULL_NAME") or " ".join(
        x for x in (cols.get("FIRST_NAME"), cols.get("LAST_NAME")) if x
    ) or None
    form_id = str(payload.get("form_id") or "")
    centro_id = centers.centro_for_form("google", form_id) or _match_centro(
        cols.get("CENTRO") or cols.get("SEDE") or cols.get("CITY")
    )
    standard = {"FULL_NAME", "FIRST_NAME", "LAST_NAME", "PHONE_NUMBER", "EMAIL"}
    extra = {k: v for k, v in cols.items() if k not in standard}
    extra.update({k: payload[k] for k in ("form_id", "campaign_id", "adgroup_id", "creative_id", "gcl_id") if payload.get(k)})
    if payload.get("is_test"):
        extra["is_test"] = True

    return LeadIn(
        source="google",
        source_ref=str(payload["lead_id"]) if payload.get("lead_id") else None,
        nome=nome,
        telefono=cols.get("PHONE_NUMBER"),
        email=cols.get("EMAIL"),
        centro_id=centro_id,
        extra=extra,
    )
