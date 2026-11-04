"""Tests never use the developer's local .env secrets or live settings."""

import os

# load_dotenv() does not override variables that already exist, so pin them before genesis is imported.
for _k in ("GENESIS_ADMIN_TOKEN", "MODEL_API_KEY", "WA_TOKEN", "MP_ACCESS_TOKEN", "WA_APP_SECRET"):
    os.environ[_k] = ""
os.environ.setdefault("GENESIS_MOCK", "1")
os.environ.setdefault("WA_DRY_RUN", "1")
