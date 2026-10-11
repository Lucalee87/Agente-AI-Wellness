# Agente AI Wellness 📞💪

Un agente vocale AI che **chiama in automatico i lead** arrivati da Meta Ads, Google Ads o dai link che mandi in DM, e **prenota la consulenza** nei tuoi centri fitness.

```
 Meta Lead Ads ─┐
 Google Ads ────┼──► Server (questo progetto) ──► coda chiamate ──► Vapi ──► 📱 telefono del lead
 Link nei DM ───┤          │                                         │
 ManyChat/Zapier┘          │◄──── strumenti: disponibilità, ─────────┘  (Claude conduce la conversazione)
                           │      prenotazione, esito
                           ▼
                  Database lead + appuntamenti ──► dashboard /admin, notifiche (Slack/Zapier...)
```

## Cosa fa

1. **Riceve i lead** da:
   - **Meta Lead Ads** (moduli Facebook/Instagram) → `POST /webhooks/meta`
   - **Google Ads** (modulo per i lead) → `POST /webhooks/google`
   - **Link per i DM / bio / sito** → pagina `GET /consulenza?centro=napoli-centro&src=ig_dm`
   - **Qualsiasi altro strumento** (ManyChat, Zapier, Make, CRM) → `POST /webhooks/lead`
2. **Chiama entro pochi secondi** (solo negli orari consentiti, es. lun–sab 9:00–20:30).
3. Durante la chiamata l'agente (Claude):
   - chiede l'obiettivo del cliente e spiega la consulenza;
   - controlla gli **orari liberi reali** del centro e propone 2–3 opzioni;
   - **prenota** lo slot (niente doppie prenotazioni) e comunica l'indirizzo;
   - gestisce “richiamami più tardi”, “non mi interessa”, “non chiamatemi più”, numero errato, segreteria.
4. Se il lead **non risponde**, ritenta automaticamente (default: subito, dopo 2 ore, dopo 1 giorno, dopo 2 giorni).
5. Tutto finisce nella **dashboard** `/admin` e, se vuoi, in una **notifica** (Slack, WhatsApp via Zapier, ecc.).

## Avvio rapido (in locale, senza telefonate)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # poi modifica i valori
python -m pytest -q             # verifica che tutto funzioni
uvicorn app.main:app --reload   # server su http://localhost:8000
```

Apri <http://localhost:8000/consulenza> per vedere la pagina da mandare nei DM.

### Provare la conversazione in chat (consigliato prima di andare live)

Con `ANTHROPIC_API_KEY` impostata nel `.env`:

```bash
python -m app.simulate --nome "Marco" --centro napoli-centro
```

Scrivi tu le risposte del cliente: l'agente risponde con lo stesso prompt e gli stessi strumenti (disponibilità, prenotazione) che userà al telefono. Ottimo per rifinire il tono prima di spendere in telefonate.

## Personalizzazione

Tutto quello che riguarda la tua attività sta in **`config/centri.yaml`**:

- nome del brand e dell'assistente, testo dell'offerta (es. “consulenza gratuita + allenamento di prova”);
- **centri**: nome, indirizzo, durata della consulenza, posti per slot, orari delle consulenze per giorno;
- **orari in cui l'agente può chiamare** e **schema dei tentativi**;
- `meta_form_ids` / `google_form_ids`: per associare ogni modulo pubblicitario al suo centro.

Il comportamento dell'agente (copione, tono, gestione obiezioni) è in `app/voice/prompt.py`.

## Messa in produzione

### 1. Server
Serve un server raggiungibile da internet con HTTPS (es. Railway, Render, Fly.io, una VPS). C'è un `Dockerfile` pronto. Imposta `PUBLIC_BASE_URL` con l'indirizzo pubblico.

### 2. Telefonia e voce: Vapi
1. Crea un account su [vapi.ai](https://vapi.ai) e copia la **API key** in `VAPI_API_KEY`.
2. Collega un **numero italiano** (es. importandolo da Twilio) e copia il suo id in `VAPI_PHONE_NUMBER_ID`.
3. Scegli una **voce italiana** (es. ElevenLabs multilingua) e mettine l'id in `VOICE_ID`.
4. Imposta `VAPI_SERVER_SECRET` con una stringa lunga a caso, poi `VOICE_PROVIDER=vapi`.

L'assistente viene configurato da questo server a ogni chiamata (prompt, strumenti, voce): non serve crearlo a mano su Vapi. Il modello Claude si sceglie con `LLM_MODEL`. Per una conversazione più reattiva al telefono puoi provare un modello più veloce (es. `claude-haiku-5-5` o `claude-sonnet-5-5`), verificando prima che sia nell'elenco dei modelli supportati da Vapi.

### 3. Meta Lead Ads
1. Su [developers.facebook.com](https://developers.facebook.com) crea un'app (tipo Business) e aggiungi il prodotto **Webhooks**.
2. Oggetto **Page**, campo **leadgen**, URL `https://TUO-DOMINIO/webhooks/meta`, token di verifica = `META_VERIFY_TOKEN`.
3. Genera un **Page Access Token** (permessi `leads_retrieval`, `pages_manage_metadata`, `pages_show_list`) → `META_PAGE_ACCESS_TOKEN`; l'App Secret → `META_APP_SECRET`.
4. Iscrivi la Pagina all'app (`POST /{page-id}/subscribed_apps?subscribed_fields=leadgen`) e prova con il [Lead Ads Testing Tool](https://developers.facebook.com/tools/lead-ads-testing).
5. Nel modulo includi il **numero di telefono** (campo obbligatorio) e, se hai più centri, una domanda “Quale centro?” oppure un modulo per centro (con `meta_form_ids`).

### 4. Google Ads
Nel modulo per i lead → *Integrazione dei lead* → **Webhook**: URL `https://TUO-DOMINIO/webhooks/google`, chiave = `GOOGLE_LEAD_KEY`. Premi *Invia dati di test* per verificare.

### 5. Link nei DM
Manda nei DM (o metti in bio / risposte rapide) link del tipo:

```
https://TUO-DOMINIO/consulenza?centro=napoli-centro&src=ig_dm
https://TUO-DOMINIO/consulenza?centro=salerno&src=wa
```

`src` ti dice da dove arrivano i lead. Se usi **ManyChat** per raccogliere il numero direttamente in chat, fai un *External Request* verso `POST /webhooks/lead` con header `X-Webhook-Token` e un JSON come:

```json
{"nome": "Marco", "telefono": "+393331234567", "centro": "napoli-centro", "source": "manychat", "obiettivo": "perdere peso"}
```

### 6. Google Calendar
Ogni consulenza prenotata diventa un evento nel Google Calendar del centro (nome, telefono, obiettivo, note). E viceversa: **gli impegni che lo staff inserisce a mano bloccano gli slot**, così l'agente non propone mai orari occupati.

1. Vai su [console.cloud.google.com](https://console.cloud.google.com), crea un progetto e abilita **Google Calendar API**.
2. *IAM e amministrazione → Account di servizio* → crea un account di servizio → *Chiavi* → *Aggiungi chiave → JSON*. Salva il file sul server e indica il percorso in `GOOGLE_SERVICE_ACCOUNT_FILE` (oppure incolla il contenuto in `GOOGLE_SERVICE_ACCOUNT_JSON`).
3. In Google Calendar, apri le impostazioni del calendario di ogni centro → *Condividi con persone specifiche* → aggiungi l'email dell'account di servizio (`...@...iam.gserviceaccount.com`) con il permesso **“Apportare modifiche agli eventi”**.
4. Copia l'**ID calendario** (Impostazioni → *Integra calendario*) nel campo `google_calendar_id` del centro in `config/centri.yaml`.

Regole utili:
- un evento segnato **“Occupato”** blocca gli slot che copre; uno **“Disponibile”** no;
- un evento di **giornata intera “Occupato”** (es. “Chiuso per ferie”) blocca tutto il giorno;
- se lo staff **cancella** l'evento di una consulenza, lo slot torna libero;
- se Google non risponde, la prenotazione viene comunque salvata e lo slot resta bloccato: l'evento viene scritto appena Google torna raggiungibile.

Il calendario è collegato tramite un'interfaccia (`app/calendars/`) pensata per aggiungere in seguito il **gestionale della palestra** con le stesse regole.

### 7. Conferme e promemoria al cliente (SMS ed email)
Quando Vittoria prenota, il cliente riceve:
- una **conferma subito**;
- un **promemoria il giorno prima alle 10:00**, solo se l'appuntamento è almeno 2 giorni dopo la prenotazione;
- un **promemoria 2 ore prima** (mai prima delle 8:00).

I testi cambiano in base al tipo di valutazione (bioimpedenza, posturale o entrambe) e si modificano in `config/centri.yaml`, sezione `messaggi`. Se l'appuntamento viene spostato, i promemoria vecchi vengono annullati.

Configurazione: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_SMS_FROM` per gli SMS; `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM` per le email (es. Brevo); poi `MESSAGGI_ATTIVI=1`. Finché non è attivo, i messaggi vengono solo scritti nei log.

### 8. Avvisi al responsabile (email)
A fine chiamata il responsabile della sede riceve **un'email** con l'esito e la **trascrizione della conversazione** quando:
- il cliente non ha trovato un orario e va **richiamato** (con la fascia oraria preferita);
- è stata **prenotata** una valutazione;
- una prenotazione è stata **annullata**;
- il cliente **non è interessato** (o numero errato, o non vuole essere ricontattato);
- il lead **non risponde** dopo tutti i tentativi.

Destinatari ed eventi: `responsabile` di ogni centro e sezione `notifiche_staff` in `config/centri.yaml`. Per inviare da Gmail: `SMTP_HOST=smtp.gmail.com`, `SMTP_USER` = indirizzo Gmail, `SMTP_PASSWORD` = una "password per le app" di Google.

### 9. Notifiche via webhook (facoltativo)
Imposta `NOTIFY_WEBHOOK_URL` (es. un webhook Zapier/Make/Slack): riceverai un evento a ogni consulenza prenotata, rifiuto o lead irraggiungibile.

## Aspetti legali (Italia/UE) — da verificare con il tuo consulente

- **Consenso**: chiama solo persone che hanno chiesto di essere ricontattate. La pagina `/consulenza` raccoglie un consenso esplicito; nei moduli Meta/Google inserisci un'informativa privacy e una casella di consenso chiara che citi il ricontatto telefonico.
- **Trasparenza**: l'agente si presenta come *assistente virtuale* e ammette di essere un'AI se glielo chiedono (AI Act).
- **Opposizione**: chi dice “non chiamatemi più” viene marcato `do_not_call` e non viene più chiamato, anche se compila un nuovo modulo.
- **Registrazioni**: se attivi la registrazione delle chiamate su Vapi, indicalo nell'informativa.
- Rispetta le fasce orarie (configurabili in `centri.yaml`).

## Struttura del progetto

```
app/
  main.py           endpoint web (webhook, pagina DM, dashboard)
  ingest/meta.py    lettura lead Meta (Graph API + verifica firma)
  ingest/google.py  lettura lead Google Ads
  leads.py          creazione lead, deduplica, tentativi, notifiche
  dialer.py         coda chiamate e gestione esito di fine chiamata
  centers.py        centri, orari, slot liberi
  calendars/        calendari esterni (Google Calendar; in futuro il gestionale)
  voice/prompt.py   copione dell'agente
  voice/tools.py    strumenti: trova_disponibilita, prenota_consulenza, registra_esito
  voice/vapi.py     integrazione Vapi
  simulate.py       simulatore testuale con Claude
config/centri.yaml  configurazione dei centri
templates/          pagina /consulenza
tests/              test automatici
```

## Prossimi passi possibili
- Collegare il gestionale della palestra (stessa interfaccia di Google Calendar).
- SMS/WhatsApp di conferma e promemoria il giorno prima.
- Report settimanale: lead → risposte → prenotazioni → presentati, per canale e campagna.
