"""BusinessSpec: the typed intermediate language between a voice note and Meta resources.

Every field that came from the seller carries `source_s`, the second of the voice note it was
heard at, so the panel can light the field next to the audio and a judge can audit it.
"""

from __future__ import annotations

import re
import unicodedata
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Fulfillment(str, Enum):
    pickup = "pickup"
    delivery = "delivery"
    both = "both"


class PaymentMethod(str, Enum):
    cash = "cash"
    link = "link"  # Mercado Pago link opened in WhatsApp's in-app browser
    transfer = "transfer"  # Nequi / Bancolombia transfer


# Categories Génesis refuses to build (Meta Business Agent excludes them; WhatsApp commerce policy).
BLOCKED_CATEGORIES = {
    "health": "salud, medicamentos o suplementos",
    "alcohol": "bebidas alcohólicas",
    "tobacco": "tabaco o vapeadores",
    "gambling": "apuestas o rifas",
    "finance": "préstamos, créditos o inversiones",
    "weapons": "armas",
    "adult": "contenido para adultos",
}


class Evidence(BaseModel):
    """Where a value came from: second of the voice note and/or index of a photo."""

    source_s: float | None = Field(default=None, description="Second of the voice note where it was said")
    photo: int | None = Field(default=None, description="0-based index of the photo that shows it")
    quote: str = Field(default="", description="Exact words the seller used")


class Product(BaseModel):
    id: str = Field(description="slug, lowercase, a-z0-9 and dashes")
    name: str
    description: str = ""
    price_cop: int = Field(ge=0, description="Price in Colombian pesos, integer")
    photo: int | None = Field(default=None, description="index of the photo that shows this product")
    evidence: Evidence = Field(default_factory=Evidence)

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        return slugify(v)


class Hours(BaseModel):
    days: list[str] = Field(description="e.g. ['mon','tue','wed','thu','fri','sat']")
    open: str = Field(description="HH:MM 24h")
    close: str = Field(description="HH:MM 24h")
    evidence: Evidence = Field(default_factory=Evidence)


class BusinessSpec(BaseModel):
    business_name: str
    owner_name: str = ""
    category: str = Field(description="short category, e.g. 'arepas', 'postres', 'ropa'")
    blocked_category: str | None = Field(default=None, description="key of BLOCKED_CATEGORIES if it applies, else null")
    city: str = "Cali"
    neighborhood: str = ""
    address_hint: str = ""
    products: list[Product] = Field(min_length=1)
    hours: Hours | None = None
    fulfillment: Fulfillment = Fulfillment.pickup
    delivery_zone: str = ""
    payment_methods: list[PaymentMethod] = Field(default_factory=lambda: [PaymentMethod.cash, PaymentMethod.link])
    tone: str = Field(default="cercano, colombiano, breve", description="how the agent should talk")
    about: str = Field(default="", max_length=139, description="WhatsApp profile 'about', <=139 chars")
    description: str = Field(default="", max_length=512)
    missing: list[str] = Field(default_factory=list, description="fields that could not be inferred")
    clarifying_question: str = Field(default="", description="ONE question to ask the seller if something essential is missing")
    evidence: dict[str, Evidence] = Field(default_factory=dict, description="evidence for top-level fields")

    @property
    def slug(self) -> str:
        return slugify(self.business_name)

    def product(self, pid: str) -> Product | None:
        return next((p for p in self.products if p.id == pid), None)


def slugify(text: str) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:48] or "item"


def json_schema() -> dict:
    """JSON Schema handed to Muse Spark structured output."""
    return BusinessSpec.model_json_schema()
