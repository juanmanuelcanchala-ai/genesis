"""Tests never use the developer's local .env secrets or live settings."""

import os

# load_dotenv() does not override variables that already exist, so pin them before genesis is imported.
for _k in ("GENESIS_ADMIN_TOKEN", "MODEL_API_KEY", "WA_TOKEN", "MP_ACCESS_TOKEN", "WA_APP_SECRET", "WA_APP_ID", "WABA_ID",
           "WA_INTAKE_PHONE_ID", "WA_CRADLE_PHONE_IDS", "WA_CRADLE_DISPLAY_NUMBERS", "PUBLIC_BASE_URL",
           "WA_SKIP_SIGNATURE", "DEMO_OWNER_WA_ID"):
    os.environ[_k] = ""
os.environ["GENESIS_MOCK"] = "1"
os.environ["WA_DRY_RUN"] = "1"
