"""Runtime settings. Everything comes from the environment (.env is never committed).

GENESIS_MOCK=1 (default when MODEL_API_KEY is missing) runs every external call against
deterministic fixtures, so the whole pipeline can be exercised offline and in CI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _list(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    # Meta Model API (OpenAI-compatible): https://api.meta.ai/v1
    model_api_key: str = os.getenv("MODEL_API_KEY", "")
    model_base_url: str = os.getenv("MODEL_BASE_URL", "https://api.meta.ai/v1")
    model_reasoning: str = os.getenv("MODEL_REASONING", "low")  # minimal|low|medium|high
    model_spec: str = os.getenv("MODEL_SPEC", "muse-spark-1.3")  # structured output, Flow JSON, agent
    model_audio: str = os.getenv("MODEL_AUDIO", "muse-spark-1.2")  # docs: 1.2 is better for audio
    model_image: str = os.getenv("MODEL_IMAGE", "muse-image-1.0")
    model_sam: str = os.getenv("MODEL_SAM", "sam-3.1")
    model_transcribe: str = os.getenv("MODEL_TRANSCRIBE", "muse-voice-transcribe-1.0")

    # WhatsApp Cloud API
    graph_version: str = os.getenv("GRAPH_VERSION", "v24.0")
    wa_token: str = os.getenv("WA_TOKEN", "")
    waba_id: str = os.getenv("WABA_ID", "")
    intake_phone_id: str = os.getenv("WA_INTAKE_PHONE_ID", "")  # the "Génesis" number sellers write to
    cradle_phone_ids: list[str] = field(default_factory=lambda: _list("WA_CRADLE_PHONE_IDS"))  # numbers that become businesses
    cradle_display_numbers: list[str] = field(default_factory=lambda: _list("WA_CRADLE_DISPLAY_NUMBERS"))
    verify_token: str = os.getenv("WA_VERIFY_TOKEN", "genesis-verify")
    app_secret: str = os.getenv("WA_APP_SECRET", "")
    catalog_id: str = os.getenv("CATALOG_ID", "")
    test_customer_wa_id: str = os.getenv("TEST_CUSTOMER_WA_ID", "")

    # Payments (link opened in WhatsApp's in-app browser; never card data in chat)
    mp_access_token: str = os.getenv("MP_ACCESS_TOKEN", "")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")

    db_path: str = os.getenv("GENESIS_DB", "genesis.db")

    @property
    def mock(self) -> bool:
        flag = os.getenv("GENESIS_MOCK")
        if flag is not None:
            return flag not in ("0", "false", "no", "")
        return not self.model_api_key

    @property
    def wa_live(self) -> bool:
        """Real Graph API calls only with a token and WA_DRY_RUN unset."""
        return bool(self.wa_token) and os.getenv("WA_DRY_RUN", "0") in ("0", "false", "no", "")


settings = Settings()
