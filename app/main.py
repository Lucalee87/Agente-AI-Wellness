"""Server web: riceve i lead, risponde agli eventi delle chiamate, dashboard."""

import asyncio
import hmac
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from html import escape

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import centers, db, dialer
from .config import ROOT, settings
from .ingest import google, meta
from .leads import LeadIn, create_lead
from .voice import tools, vapi

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

templates = Jinja2Templates(directory=str(ROOT / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.ensure_connected()
    task = None
    if os.environ.get("DISABLE_DIALER") != "1":
        task = asyncio.create_task(dialer.run_forever())
    yield
    if task:
        task.cancel()


app = FastAPI(title="Agente AI Wellness", lifespan=lifespan)


@app.get("/", response_class=PlainTextResponse)
def health():
    return "ok"


# --------------------------------------------------------------------------- #
# Pagina per i link nei DM / bio / sito
# --------------------------------------------------------------------------- #

def _landing_ctx(request: Request, **extra) -> dict:
    cfg = centers.load_config()
    return {
        "request": request,
        "brand": cfg.brand,
        "centri": list(cfg.centri.values()),
        "form": {},
        **extra,
    }


@app.get("/consulenza", response_class=HTMLResponse)
def consulenza_form(request: Request, centro: str = "", src: str = "dm"):
    return templates.TemplateResponse(request, "consulenza.html", _landing_ctx(request, centro=centro, src=src))


@app.post("/consulenza", response_class=HTMLResponse)
async def consulenza_submit(request: Request):
    form = dict(await request.form())
    src = (form.get("src") or "dm")[:40]
    if form.get("consenso") != "1":
        ctx = _landing_ctx(request, centro=form.get("centro", ""), src=src, form=form,
                           error="Per richiamarti ci serve il tuo consenso.")
        return templates.TemplateResponse(request, "consulenza.html", ctx, status_code=400)
    result = create_lead(LeadIn(
        source="dm" if src.startswith(("dm", "ig", "fb", "wa", "tiktok")) else "web",
        telefono=form.get("telefono"),
        nome=form.get("nome"),
        email=(form.get("email") or "").strip() or None,
        centro_id=form.get("centro"),
        obiettivo=form.get("obiettivo") or None,
        extra={"src": src, "consenso": True},
    ))
    if result.lead_id is None:
        ctx = _landing_ctx(request, centro=form.get("centro", ""), src=src, form=form,
                           error="Il numero di telefono non sembra valido, puoi controllarlo?")
        return templates.TemplateResponse(request, "consulenza.html", ctx, status_code=400)
    ctx = _landing_ctx(request, done=True, nome=form.get("nome"), calling_now=centers.is_calling_time())
    return templates.TemplateResponse(request, "consulenza.html", ctx)


# --------------------------------------------------------------------------- #
# Meta Lead Ads
# --------------------------------------------------------------------------- #

@app.get("/webhooks/meta", response_class=PlainTextResponse)
def meta_verify(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
):
    if mode == "subscribe" and settings.meta_verify_token and hmac.compare_digest(token, settings.meta_verify_token):
        return challenge
    raise HTTPException(403, "verify token non valido")


def _process_meta_events(events: list[dict]) -> None:
    for ev in events:
        try:
            data = meta.fetch_lead(str(ev["leadgen_id"]))
            result = create_lead(meta.to_lead(ev, data))
            log.info("Lead Meta %s -> %s", ev.get("leadgen_id"), result)
        except Exception:
            log.exception("Impossibile importare il lead Meta %s", ev.get("leadgen_id"))


@app.post("/webhooks/meta")
async def meta_webhook(request: Request, background: BackgroundTasks,
                       x_hub_signature_256: str | None = Header(None)):
    raw = await request.body()
    if not meta.verify_signature(raw, x_hub_signature_256):
        raise HTTPException(403, "firma non valida")
    events = meta.leadgen_events(json.loads(raw or b"{}"))
    if events:
        background.add_task(_process_meta_events, events)
    return {"ok": True, "eventi": len(events)}


# --------------------------------------------------------------------------- #
# Google Ads lead form
# --------------------------------------------------------------------------- #

@app.post("/webhooks/google")
async def google_webhook(request: Request):
    payload = await request.json()
    if not google.verify_key(payload):
        raise HTTPException(403, "google_key non valida")
    result = create_lead(google.to_lead(payload))
    log.info("Lead Google %s -> %s", payload.get("lead_id"), result)
    return {}


# --------------------------------------------------------------------------- #
# Webhook generico (ManyChat, Zapier, Make, sito web, CRM...)
# --------------------------------------------------------------------------- #

@app.post("/webhooks/lead")
async def generic_webhook(request: Request, x_webhook_token: str | None = Header(None), token: str = ""):
    supplied = x_webhook_token or token
    if not settings.generic_webhook_token or not hmac.compare_digest(supplied, settings.generic_webhook_token):
        raise HTTPException(403, "token non valido")
    p = await request.json()
    result = create_lead(LeadIn(
        source=str(p.get("source") or "generic")[:20],
        source_ref=str(p["id"]) if p.get("id") else None,
        telefono=p.get("telefono") or p.get("phone"),
        nome=p.get("nome") or p.get("name"),
        email=p.get("email"),
        centro_id=p.get("centro") or p.get("centro_id"),
        obiettivo=p.get("obiettivo"),
        note=p.get("note"),
        extra={k: v for k, v in p.items() if k not in {"telefono", "phone", "nome", "name", "email"}},
    ))
    if result.lead_id is None:
        raise HTTPException(422, result.reason)
    return {"lead_id": result.lead_id, "creato": result.created, "motivo": result.reason}


# --------------------------------------------------------------------------- #
# Eventi delle chiamate (Vapi)
# --------------------------------------------------------------------------- #

@app.post("/webhooks/vapi")
async def vapi_webhook(request: Request, x_vapi_secret: str | None = Header(None)):
    if not settings.vapi_server_secret or not hmac.compare_digest(x_vapi_secret or "", settings.vapi_server_secret):
        raise HTTPException(403, "secret non valido")
    body = await request.json()
    message = body.get("message") or {}
    mtype = message.get("type")
    lead_id = vapi.lead_id_for_call(message)

    if mtype == "tool-calls":
        results = []
        for tc in message.get("toolCallList") or message.get("toolCalls") or []:
            fn = tc.get("function") or {}
            if lead_id is None:
                out = "Errore: chiamata non associata a nessun lead."
            else:
                out = await asyncio.to_thread(
                    tools.execute_tool, lead_id, fn.get("name", ""), vapi.parse_tool_arguments(fn.get("arguments"))
                )
            results.append({"toolCallId": tc.get("id"), "result": out})
        return {"results": results}

    if mtype == "end-of-call-report" and lead_id is not None:
        await asyncio.to_thread(dialer.handle_end_of_call, lead_id, message)

    return {}


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #

security = HTTPBasic()


def require_admin(creds: HTTPBasicCredentials = Depends(security)) -> None:
    ok = settings.admin_password and secrets.compare_digest(creds.password, settings.admin_password) \
        and creds.username == "admin"
    if not ok:
        raise HTTPException(401, "accesso negato", headers={"WWW-Authenticate": "Basic"})


def _leads_rows(limit: int = 300) -> list[dict]:
    with db.tx() as c:
        rows = c.execute(
            """SELECT l.*, a.start_at AS appuntamento
               FROM leads l
               LEFT JOIN appointments a ON a.lead_id = l.id AND a.status = 'confermato'
               ORDER BY l.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/admin/leads.json", dependencies=[Depends(require_admin)])
def admin_leads_json():
    return JSONResponse(_leads_rows())


@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_dashboard():
    rows = _leads_rows()
    with db.tx() as c:
        stats = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) AS n FROM leads GROUP BY status")}
    cols = ["id", "created_at", "source", "nome", "telefono", "centro_id", "status", "attempts", "appuntamento", "summary"]
    head = "".join(f"<th>{c}</th>" for c in cols)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(str(r.get(c) or ''))}</td>" for c in cols) + "</tr>" for r in rows
    )
    stat_html = " · ".join(f"<b>{escape(k)}</b>: {v}" for k, v in sorted(stats.items())) or "nessun lead"
    return f"""<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Lead · Agente AI</title>
<style>body{{font-family:system-ui,sans-serif;margin:16px;}}table{{border-collapse:collapse;font-size:13px;width:100%}}
td,th{{border:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}}th{{background:#f3f3f3}}
.wrap{{overflow-x:auto}}</style></head><body><h1>Lead</h1><p>{stat_html}</p>
<div class="wrap"><table><tr>{head}</tr>{body}</table></div></body></html>"""
