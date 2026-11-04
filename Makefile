# Génesis: common tasks. Everything runs offline in mock + dry-run unless you export real keys.
MOCK ?= 1
DRY ?= 1
PORT ?= 8000
ENV = GENESIS_MOCK=$(MOCK) WA_DRY_RUN=$(DRY)

.PHONY: test demo serve stage smoke smoke-muse rehearse

test:  ## full test suite, offline
	$(ENV) uv run pytest

demo:  ## offline end-to-end demo with a millisecond timeline
	$(ENV) uv run python scripts/demo_offline.py

serve:  ## server + panel on http://localhost:$(PORT)
	$(ENV) uv run uvicorn genesis.server:app --host 0.0.0.0 --port $(PORT)

stage:  ## like serve, with per-node pacing so the panel animations are visible in mock
	$(ENV) GENESIS_STAGE_DELAY_S=$${GENESIS_STAGE_DELAY_S:-0.6} uv run uvicorn genesis.server:app --host 0.0.0.0 --port $(PORT)

smoke:  ## start the real server, check the panel, run the demo over SSE until run.live, stop
	$(ENV) uv run python scripts/smoke_server.py --port 8765

smoke-muse:  ## one call to every Meta Model API endpoint (needs MODEL_API_KEY)
	uv run python scripts/smoke_muse.py

rehearse:  ## whole story with the real Meta models, WhatsApp in dry-run (needs MODEL_API_KEY)
	uv run python scripts/rehearse_real.py
