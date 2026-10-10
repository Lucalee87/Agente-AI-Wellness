import hashlib
import hmac
import json

from app import db
from app.ingest import meta


def _leads():
    with db.tx() as c:
        return [dict(r) for r in c.execute("SELECT * FROM leads ORDER BY id")]


GOOGLE_PAYLOAD = {
    "lead_id": "abc123",
    "api_version": "1.0",
    "form_id": 999,
    "campaign_id": 1,
    "google_key": "google-key",
    "is_test": True,
    "user_column_data": [
        {"column_name": "Full Name", "string_value": "Anna Rossi", "column_id": "FULL_NAME"},
        {"column_name": "User Phone", "string_value": "+39 340 111 2233", "column_id": "PHONE_NUMBER"},
        {"column_name": "User Email", "string_value": "anna@example.com", "column_id": "EMAIL"},
    ],
}


def test_google_webhook_creates_lead_once(client):
    assert client.post("/webhooks/google", json=GOOGLE_PAYLOAD).status_code == 200
    assert client.post("/webhooks/google", json=GOOGLE_PAYLOAD).status_code == 200
    rows = _leads()
    assert len(rows) == 1
    assert rows[0]["nome"] == "Anna Rossi"
    assert rows[0]["telefono"] == "+393401112233"
    assert rows[0]["status"] == db.NEW


def test_google_webhook_rejects_wrong_key(client):
    bad = {**GOOGLE_PAYLOAD, "google_key": "nope"}
    assert client.post("/webhooks/google", json=bad).status_code == 403
    assert _leads() == []


def test_meta_verification(client):
    ok = client.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "meta-verify",
                                              "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    bad = client.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "x",
                                               "hub.challenge": "1"})
    assert bad.status_code == 403


def test_meta_webhook_fetches_and_saves_lead(client, monkeypatch):
    monkeypatch.setattr(meta, "fetch_lead", lambda lid: {
        "id": lid,
        "field_data": [
            {"name": "full_name", "values": ["Luca Bianchi"]},
            {"name": "phone_number", "values": ["+393471234567"]},
            {"name": "centro", "values": ["Salerno"]},
            {"name": "obiettivo", "values": ["tonificare"]},
        ],
    })
    body = json.dumps({"object": "page", "entry": [{"id": "1", "changes": [
        {"field": "leadgen", "value": {"leadgen_id": "L1", "form_id": "F1", "page_id": "1"}}]}]}).encode()
    sig = "sha256=" + hmac.new(b"meta-secret", body, hashlib.sha256).hexdigest()

    assert client.post("/webhooks/meta", content=body, headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 403
    r = client.post("/webhooks/meta", content=body, headers={"X-Hub-Signature-256": sig,
                                                             "Content-Type": "application/json"})
    assert r.status_code == 200
    [lead] = _leads()
    assert lead["nome"] == "Luca Bianchi"
    assert lead["centro_id"] == "salerno"
    assert lead["obiettivo"] == "tonificare"
    assert lead["source_ref"] == "L1"


def test_landing_page(client):
    assert "Prenota la tua consulenza" in client.get("/consulenza?centro=salerno&src=ig_dm").text
    no_consent = client.post("/consulenza", data={"nome": "Sara", "telefono": "3331112222", "centro": "salerno"})
    assert no_consent.status_code == 400
    r = client.post("/consulenza", data={"nome": "Sara", "telefono": "3331112222", "centro": "salerno",
                                         "consenso": "1", "src": "ig_dm"})
    assert r.status_code == 200 and "Grazie" in r.text
    [lead] = _leads()
    assert lead["source"] == "dm" and lead["centro_id"] == "salerno"


def test_generic_webhook_and_dedupe_by_phone(client):
    h = {"X-Webhook-Token": "generic-token"}
    assert client.post("/webhooks/lead", json={"telefono": "3330000000"}).status_code == 403
    r1 = client.post("/webhooks/lead", json={"name": "Paolo", "phone": "3330000000", "source": "manychat"}, headers=h)
    r2 = client.post("/webhooks/lead", json={"name": "Paolo", "phone": "+39 333 0000000"}, headers=h)
    assert r1.json()["creato"] is True
    assert r2.json()["creato"] is False
    assert len(_leads()) == 1
    assert client.post("/webhooks/lead", json={"phone": "12"}, headers=h).status_code == 422
