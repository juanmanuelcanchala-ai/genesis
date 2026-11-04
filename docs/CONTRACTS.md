# Génesis: module contracts

Single source of truth for module boundaries. Python 3.12+, sync code (FastAPI runs sync handlers in a
threadpool). Run everything with `uv run`. All modules must work with `GENESIS_MOCK=1` and no network.
Already written (do not change signatures without updating this file): `genesis/config.py`,
`genesis/spec.py`, `genesis/events.py`, `genesis/plan.py`.

## Product in one paragraph

A street vendor (Rosa, arepas in Cali) sends the **Génesis intake number** on WhatsApp a voice note and up
to 3 photos. Génesis transcribes (Muse Voice Transcribe / Spark 1.2), compiles a typed `BusinessSpec`
(Spark 1.3 structured output, every field with the second of audio it came from), asks at most ONE
clarifying question, reads back a summary and waits for "sí". Then a deterministic graph (`plan.py`)
turns a pre-registered **cradle number** (Cloud API number from FunnelChat's pre-verified pool) into her
business in parallel lanes: profile (about, description, address, photo), brand (SAM 3.1 crops the product
from her photo, Muse Image makes a logo and a clean product shot), menu (catalog products + a WhatsApp
Flow order form generated from the spec), agent (Spark 1.3 agent config with her menu, hours, tone),
payments (Mercado Pago link factory). An **Inspector** asks the agent test questions and places a test
order end to end before the business is switched **live**. Rosa gets "Tu negocio está vivo" + a wa.me link
/ QR. Customers who write to the cradle number talk to her agent, order through the Flow, pay with a
Mercado Pago link (in-app browser, never card data in chat) and Rosa is notified on her own WhatsApp.

## Module map (owner = workflow agent)

| File                                          | Owner | Purpose                                                                            |
| --------------------------------------------- | ----- | ---------------------------------------------------------------------------------- |
| `genesis/muse.py`                             | A     | Meta Model API client (+ mock)                                                     |
| `scripts/smoke_muse.py`                       | A     | one-shot check of every Muse endpoint when MODEL_API_KEY exists                    |
| `genesis/whatsapp.py`                         | B     | WhatsApp Cloud API client (+ dry-run recorder)                                     |
| `genesis/compiler.py`                         | C     | voice note + photos → BusinessSpec, clarification, confirmation text               |
| `genesis/flowgen.py`                          | C     | BusinessSpec → WhatsApp Flow JSON (order form) + lint                              |
| `genesis/blueprint.py` + `genesis/lanes/*.py` | D     | BusinessSpec → list[plan.Node]                                                     |
| `genesis/store.py`                            | E     | SQLite: businesses (cradle → spec, agent config, live flag), orders, conversations |
| `genesis/agent.py`                            | E     | customer-facing agent runtime for a live business                                  |
| `genesis/payments.py`                         | E     | Mercado Pago preference + webhook handling (+ mock)                                |
| `genesis/inspector.py`                        | E     | test questions + end-to-end test order, returns a report                           |
| `genesis/onboarding.py`                       | F     | intake conversation state machine; starts runs                                     |
| `genesis/server.py`                           | F     | FastAPI app: webhooks, demo API, SSE, panel                                        |
| `panel/index.html`                            | F     | stage panel (lanes lighting up, timer, final QR)                                   |

## A. `genesis/muse.py`

OpenAI-compatible base URL `settings.model_base_url`, key `settings.model_api_key`
(`Authorization: Bearer`). Model ids in `settings`. Every function has a deterministic mock branch
(`settings.mock`) that returns fixture data for the Rosa example (see `fixtures/`).

```python
@dataclass
class Transcript:
    text: str
    segments: list[dict]  # [{"start": float, "end": float, "text": str}], may be [] if API gives none
    model: str

def transcribe(audio: bytes, mime: str = "audio/ogg") -> Transcript
    # WhatsApp voice notes are OGG/Opus: convert with ffmpeg to WAV mono 16 kHz s16le.
    # Primary: POST /v1/asr/transcribe (multipart: model=muse-voice-transcribe-1.0, file=wav).
    # Fallback: chat completion with settings.model_audio and an input_audio part (wav, base64).
def structured(prompt: str, schema: dict, name: str, *, system: str = "", images: list[bytes] | None = None,
               model: str | None = None, reasoning: str | None = None) -> dict
    # chat.completions with response_format={"type":"json_schema","json_schema":{"name":name,"schema":schema,"strict":False}}
    # images as data: URLs (image_url parts). Parses and returns the JSON object. Retries once on invalid JSON.
def chat(messages: list[dict], tools: list[dict] | None = None, *, model: str | None = None,
         reasoning: str | None = None) -> dict
    # returns {"content": str|None, "tool_calls": [{"id","name","arguments": dict}]}
def segment(image: bytes, prompt: str) -> list[dict]
    # SAM 3.1 via POST /v1/responses (model sam-3.1, input_text prompt + input_image). Returns
    # [{"box": [x0,y0,x1,y1] in pixels, "score": float}]. If the response shape is unexpected, fall back to
    # Spark grounding: structured() asking for boxes. Never raise for "no match": return [].
def crop(image: bytes, box: list[float], pad: float = 0.06) -> bytes   # PNG/JPEG bytes, pure local (Pillow optional; if Pillow missing return original)
def image_generate(prompt: str, size: str = "1:1") -> bytes            # /v1/images/generations, model muse-image-1.0, b64
def image_edit(image: bytes, prompt: str) -> bytes                     # /v1/images/edits (keep the product pixels, change background)
```

Mock fixtures: `fixtures/rosa_transcript.json`, `fixtures/rosa_spec.json` (valid BusinessSpec for
"Arepas Rosa", arepas de choclo con queso 5000, arepa de carne desmechada 5000, horario 6-11 lun-sáb,
San Fernando Cali, recogida, efectivo + link), tiny PNGs generated in code for images. Mock `chat()`
must be a small deterministic rule-based responder good enough for inspector tests (answers price,
hours, refuses alcohol, calls `crear_pedido` when asked to order).

## B. `genesis/whatsapp.py`

Graph base `https://graph.facebook.com/{settings.graph_version}`, bearer `settings.wa_token`.
When `not settings.wa_live` every call is **recorded** in `whatsapp.SENT: list[dict]` and returns a fake
id, so tests can assert on it.

```python
def send_text(phone_id: str, to: str, body: str) -> str
def send_buttons(phone_id, to, body, buttons: list[tuple[str, str]]) -> str          # (id, title<=20)
def send_flow(phone_id, to, body, flow_id: str, cta: str, screen: str, data: dict | None = None,
              flow_token: str = "") -> str                                            # interactive type=flow, navigate action
def send_cta_url(phone_id, to, body, button_text: str, url: str) -> str
def send_image(phone_id, to, image: bytes, caption: str = "") -> str                  # uploads media then sends
def send_template(phone_id, to, name: str, lang: str, params: list[str]) -> str
def mark_read(phone_id, message_id) -> None
def download_media(media_id: str) -> tuple[bytes, str]                                # (bytes, mime)
def upload_media(phone_id, data: bytes, mime: str) -> str
def get_business_profile(phone_id) -> dict
def update_business_profile(phone_id, *, about: str | None = None, description: str | None = None,
                            address: str | None = None, vertical: str | None = None,
                            profile_picture_handle: str | None = None, websites: list[str] | None = None) -> None
def upload_profile_picture(app_id: str, data: bytes, mime: str) -> str               # Resumable Upload API → handle
def create_flow(waba_id, name: str, categories: list[str]) -> str                    # returns flow_id
def update_flow_json(flow_id, flow_json: dict) -> list[dict]                          # returns validation_errors
def publish_flow(flow_id) -> None
def deprecate_or_delete_flow(flow_id) -> None                                         # DRAFT → delete, PUBLISHED → deprecate
def create_product(catalog_id, retailer_id, name, description, price_cop: int, image_url: str, url: str) -> str
def delete_product(catalog_id, retailer_id) -> None
def verify_signature(raw_body: bytes, header: str) -> bool                           # X-Hub-Signature-256 with app_secret; True if no secret configured
def parse_webhook(payload: dict) -> list[Inbound]
@dataclass
class Inbound:
    phone_id: str          # metadata.phone_number_id (which of OUR numbers received it)
    wa_id: str             # sender
    name: str
    message_id: str
    type: str              # text|audio|image|interactive|button|...
    text: str = ""
    media_id: str = ""
    mime: str = ""
    button_id: str = ""
    flow_response: dict | None = None   # parsed nfm_reply.response_json
    timestamp: int = 0
def wa_me_link(display_number: str, text: str = "") -> str
```

## C. `genesis/compiler.py` and `genesis/flowgen.py`

```python
@dataclass
class Draft:
    spec: BusinessSpec
    transcript: muse.Transcript
    question: str          # "" if nothing essential missing; else ONE question in Colombian Spanish
    blocked: str | None    # BLOCKED_CATEGORIES key if refused
def compile_spec(audio: bytes, mime: str, photos: list[bytes], answer: str = "", previous: Draft | None = None) -> Draft
    # transcript → muse.structured(prompt, spec.json_schema()) with photos; evidence source_s from segments.
    # Essential: business_name (invent "<Producto> <owner>" if absent), >=1 product with price, fulfillment.
    # `answer` is the seller's reply to the clarifying question: merge it (second structured call, previous spec in prompt).
def summary(spec: BusinessSpec) -> str        # WhatsApp-ready read-back, ends asking to confirm with "sí"
def is_yes(text: str) -> bool                 # sí/si/dale/listo/de una/ok/👍 ...
```

`flowgen.py`:

```python
def order_flow(spec: BusinessSpec) -> dict    # Flow JSON version "7.0": screen ORDER (product checkboxes or
    # per-product quantity dropdowns 0-10, pickup time dropdown derived from hours, fulfillment radio if both,
    # name, optional note) → screen CONFIRM → complete action with payload. No data_exchange endpoint (navigate only).
def lint(flow_json: dict) -> list[str]        # local checks: ids unique, screen ids uppercase, terminal screen, footer, label lengths
def parse_order(response_json: dict, spec: BusinessSpec) -> dict
    # → {"items":[{"product_id","name","qty","unit_cop"}], "total_cop", "pickup_time", "fulfillment", "customer_name", "note"}
```

## D. `genesis/blueprint.py` and `genesis/lanes/`

```python
def build(ctx: plan.Ctx) -> list[plan.Node]
```

Lanes and node ids (deps in brackets). Each node: apply/verify/revert using `whatsapp`, `muse`, `store`.

- `number.profile` — update_business_profile(about, description, address, vertical) on ctx.cradle_phone_id. revert: restore previous profile (read it first).
- `brand.crops` — SAM 3.1 segment each photo with the product word (spec.category) → crops. non-critical.
- `brand.logo` [brand.crops] — muse.image_generate(logo prompt from spec); `brand.hero` — image_edit of best crop (clean background, keep product). non-critical.
- `number.photo` [brand.logo] — upload_profile_picture + update_business_profile(profile_picture_handle). non-critical.
- `menu.catalog` [brand.hero] — create_product per product (skip with output {"skipped": "no catalog"} if no CATALOG_ID). non-critical. revert deletes.
- `menu.flow` — flowgen.order_flow → lint → create_flow → update_flow_json (validation_errors must be []) → publish. revert deprecate/delete.
- `agent.config` [menu.flow] — store.save_business(cradle, spec, flow_id, system_prompt, knowledge) with live=False.
- `payments.ready` — payments.check() (token valid or mock). output {"provider": "mercadopago"|"mock"}.
- `inspector.run` [agent.config, payments.ready, number.profile] — inspector.inspect(cradle) must pass, else raise.
- `launch.live` [inspector.run] — store.set_live(cradle, True); send Rosa "Tu negocio está vivo" + wa_me_link. revert set_live False.
  Each node emits extra panel detail through `ctx.emit("node.detail", lane, node, message, **data)`
  (e.g. the Flow JSON size, the crops' boxes, the logo as a URL path `/media/<run>/<name>.png`).
  Media saved under `media/<run_id>/` (gitignored); server serves `/media`.

## E. `genesis/store.py`, `genesis/agent.py`, `genesis/payments.py`, `genesis/inspector.py`

```python
# store.py (SQLite at settings.db_path, thread-safe)
def save_business(cradle_phone_id, owner_wa_id, spec: BusinessSpec, flow_id: str, system_prompt: str, run_id: str) -> None
def get_business(cradle_phone_id) -> dict | None   # {"spec": BusinessSpec, "flow_id", "system_prompt", "owner_wa_id", "live", "run_id"}
def set_live(cradle_phone_id, live: bool) -> None
def create_order(cradle_phone_id, customer_wa_id, order: dict, test: bool = False) -> dict  # adds "id" (#1, #2...), status "pending"
def set_order_status(order_id: str, status: str, payment_id: str = "") -> dict
def list_orders(cradle_phone_id) -> list[dict]
def history(cradle_phone_id, wa_id, limit=20) -> list[dict]; def append(cradle_phone_id, wa_id, role, content) -> None

# agent.py
def system_prompt(spec: BusinessSpec) -> str   # business-specific, declares it is an AI assistant of the business,
    # never general-purpose (WhatsApp AI policy), hands off to the owner on anything outside the menu.
TOOLS: list[dict]  # consultar_menu, crear_pedido(items, pickup_time, customer_name), link_de_pago(order_id), estado_pedido(order_id), pasar_a_humano(reason)
def handle(cradle_phone_id: str, inbound: whatsapp.Inbound) -> list[str]   # replies sent (message ids)
    # text/audio (transcribe) → Spark with tools; "Hacer pedido" → send_flow; nfm_reply → flowgen.parse_order →
    # create_order → send_cta_url(payments.link(order)) → notify owner. Not live → polite "aún no abrimos".
def reply_text(cradle_phone_id: str, wa_id: str, text: str) -> dict   # pure function used by inspector: {"text", "tool_calls"}

# payments.py
def check() -> dict
def link(order: dict, spec: BusinessSpec) -> str   # MP preference init_point (sandbox_init_point if test); mock → f"{public_base_url}/pay/mock/{order_id}"
def handle_webhook(payload: dict, query: dict) -> dict | None  # fetch payment, if approved → store.set_order_status(paid) and return order
def mock_pay(order_id) -> dict                       # marks paid (demo/tests)

# inspector.py
def inspect(cradle_phone_id: str, ctx_emit=None) -> dict
    # 5 questions: price of first product, hours, pickup/delivery, "¿venden cerveza?" (must refuse),
    # "quiero 2 de <product>" (must call crear_pedido or offer the Flow). Then a test order through
    # flowgen.parse_order on a synthetic response_json → store.create_order(test=True) → payments.link →
    # payments.mock_pay in mock / check link format live. Returns {"passed": bool, "checks":[{"name","ok","detail"}]}.
```

## F. `genesis/onboarding.py`, `genesis/server.py`, `panel/index.html`

Onboarding state per seller wa_id on the intake number: `collecting` (accept 1 voice note + up to 3
photos; "listo" or 20 s after the last photo → compile) → `asking` (one clarifying question) →
`confirming` (summary, wait is_yes) → `building` (thread running plan.apply_plan with a free cradle) →
`live`. Cradle assignment: first `settings.cradle_phone_ids` entry not used by a business; in mock use
"cradle-1".

```python
def start_run(owner_wa_id: str, audio: bytes, mime: str, photos: list[bytes], answer: str = "") -> str  # run_id; used by demo API too
def handle_intake(inbound: whatsapp.Inbound) -> None
```

`server.py` routes: `GET /webhook` (hub.challenge), `POST /webhook` (verify signature, parse, route by
phone_id: intake → onboarding, cradle → agent.handle; ack 200 fast, work in background thread),
`POST /mp/webhook`, `GET /pay/mock/{order_id}` (mock checkout page that marks paid), `POST /api/runs`
(multipart audio + photos → start_run, returns run_id; `?auto_confirm=1` skips the read-back for the
stage demo), `GET /api/runs/{run_id}/events` (SSE from events.bus, with backlog), `GET /api/runs/{id}`,
`GET /api/businesses`, `GET /media/...`, `GET /` → panel.

Panel (`panel/index.html`, no build step, vanilla JS + CSS, works on a projector): big timer, transcript
with fields lighting up next to their audio second, 6 lane columns (Número, Marca, Menú, Agente, Pagos,
Inspector) with node cards (pending/running/done/failed/reverted, ms), the Inspector checklist, and at
the end a large QR (generate client-side with a tiny inline QR encoder or `https://api.qrserver.com`-free
approach: inline JS library code is fine) for the wa.me link plus "Tu negocio está vivo".
