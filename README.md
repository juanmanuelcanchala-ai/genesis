# Génesis

From a WhatsApp voice note to a live, tested business on Meta in three minutes.

Built during the Meta Global AI Developer Hackathon (Oct 26 – Nov 4, 2026). Work in progress.

## Run it

```bash
make test    # full suite, offline (GENESIS_MOCK=1, WA_DRY_RUN=1)
make demo    # whole stage demo offline through real webhook payloads, with a ms timeline
make serve   # server + stage panel on http://localhost:8000 (press D on the panel for the demo)
make stage   # same, with per-node pacing for the projector (GENESIS_STAGE_DELAY_S=0.6)
make smoke   # start uvicorn, check the panel, run the demo over SSE until run.live, stop
```

Module boundaries: `docs/CONTRACTS.md`. Real keys go in `.env` (see `.env.example`).

## Live checklist (real WhatsApp keys)

- `GENESIS_ADMIN_TOKEN`: the stage API (`/api/*`) is closed without it when WhatsApp is live. Open the
  panel as `https://<tunnel>/#token=<GENESIS_ADMIN_TOKEN>`; `make smoke` sends it as `X-Genesis-Token`.
- `WA_APP_SECRET`: live, `/webhook` rejects every unsigned request.
- `WA_INTAKE_PHONE_ID`: without it the seller gets no "tu negocio está vivo" nor order alerts.
- 24 h window: messages to the seller are free text from the intake number. Within the 24 h before the
  demo, the seller's phone (`DEMO_OWNER_WA_ID`) must write anything to the intake number; otherwise Meta
  answers 200 and later drops them (status `failed`, 131047, logged as a warning). On a test number the
  seller and test customers must also be in the allowed recipients list (131030).
- Flows: check `GET /{cradle_phone_id}/whatsapp_business_encryption` on every cradle number. If Meta refuses
  to publish the order form, the business still launches and takes orders in chat.
- `PUBLIC_BASE_URL` must be the public https tunnel: Mercado Pago only gets `notification_url` then.
- Inspector: one soft miss (price, hours, delivery, order wording) is a warning; `GENESIS_INSPECTOR_STRICT=1`
  blocks on any.
