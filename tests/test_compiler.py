import copy
import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

from genesis import compiler
from genesis.spec import BusinessSpec

FIX = Path(__file__).resolve().parent.parent / "fixtures"
ROSA_SPEC = json.loads((FIX / "rosa_spec.json").read_text())
ROSA_TR = json.loads((FIX / "rosa_transcript.json").read_text())


@dataclass
class Transcript:
    text: str
    segments: list
    model: str


class FakeMuse(types.ModuleType):
    def __init__(self, responses):
        super().__init__("genesis.muse")
        self.Transcript = Transcript
        self.responses = list(responses)
        self.calls = []
        self.transcribed = 0

    def transcribe(self, audio, mime="audio/ogg"):
        self.transcribed += 1
        return Transcript(ROSA_TR["text"], ROSA_TR["segments"], ROSA_TR["model"])

    def structured(self, prompt, schema, name, *, system="", images=None, model=None, reasoning=None):
        self.calls.append({"prompt": prompt, "schema": schema, "name": name, "system": system, "images": images})
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return copy.deepcopy(r)


@pytest.fixture
def fake(monkeypatch):
    def install(*responses):
        m = FakeMuse(responses)
        monkeypatch.setitem(sys.modules, "genesis.muse", m)
        return m
    return install


def test_compile_rosa(fake):
    m = fake(ROSA_SPEC)
    d = compiler.compile_spec(b"ogg", "audio/ogg", [b"p0", b"p1"])
    assert isinstance(d.spec, BusinessSpec)
    assert d.question == "" and d.blocked is None
    assert d.spec.business_name == "Arepas Rosa"
    assert [p.price_cop for p in d.spec.products] == [5000, 5000]
    assert d.spec.products[0].evidence.source_s == 4.2
    assert d.spec.hours.evidence.source_s == 9.8
    assert d.spec.evidence["business_name"].source_s == 22.4
    call = m.calls[0]
    assert call["images"] == [b"p0", b"p1"]
    assert "properties" in call["schema"] and "products" in call["schema"]["properties"]
    assert "[4.2-9.8]" in call["prompt"] and "cinco mil" in call["prompt"]


def test_messy_model_output_is_normalized(fake):
    raw = {
        "owner_name": "Rosa",
        "category": "arepas",
        "products": [
            {"name": "Arepa de choclo con queso", "price_cop": "cinco mil", "photo": 7,
             "evidence": {"quote": "arepa de choclo con queso"}},
            {"name": "Arepa de carne desmechada", "price_cop": "a 5"},
        ],
        "hours": {"days": ["lunes", "martes", "sábado"], "open": "6 de la mañana", "close": "11 de la mañana"},
        "fulfillment": "pickup",
        "payment_methods": ["efectivo", "Nequi", "bitcoin"],
        "about": "x" * 300,
        "blocked_category": "nonsense",
    }
    fake(raw)
    d = compiler.compile_spec(b"ogg", "audio/ogg", [b"p0"])
    s = d.spec
    assert s.business_name == "Arepas Rosa"
    assert [p.price_cop for p in s.products] == [5000, 5000]
    assert s.products[0].photo is None
    assert s.products[0].evidence.source_s == 4.2
    assert s.products[1].evidence.source_s == 4.2
    assert (s.hours.open, s.hours.close, s.hours.days) == ("06:00", "11:00", ["mon", "tue", "sat"])
    assert [m.value for m in s.payment_methods] == ["cash", "transfer"]
    assert len(s.about) == 139
    assert s.blocked_category is None
    assert s.evidence["owner_name"].source_s == 0.0


def test_retry_once_with_validation_error(fake):
    bad = {"business_name": "Arepas Rosa", "category": "arepas", "products": []}
    m = fake(bad, ROSA_SPEC)
    d = compiler.compile_spec(b"ogg", "audio/ogg", [])
    assert len(m.calls) == 2
    assert "no pasó la validación" in m.calls[1]["prompt"]
    assert d.spec.business_name == "Arepas Rosa"


def test_fails_after_second_invalid(fake):
    bad = {"business_name": "X", "category": "x", "products": []}
    m = fake(bad, bad)
    with pytest.raises(compiler.CompileError):
        compiler.compile_spec(b"ogg", "audio/ogg", [])
    assert len(m.calls) == 2


def test_one_question_when_price_missing_then_merge(fake):
    raw = copy.deepcopy(ROSA_SPEC)
    raw["products"][1]["price_cop"] = 0
    raw["missing"] = ["price:arepa-de-carne-desmechada"]
    m = fake(raw)
    d = compiler.compile_spec(b"ogg", "audio/ogg", [])
    assert d.question == "¿A cómo vendes Arepa de carne desmechada?"
    assert d.spec.clarifying_question == d.question

    merged = copy.deepcopy(ROSA_SPEC)
    merged["products"][1]["price_cop"] = "a 6"
    m.responses = [merged]
    d2 = compiler.compile_spec(b"", "audio/ogg", [], answer="la de carne a 6", previous=d)
    assert m.transcribed == 1  # reuses the previous transcript
    assert d2.question == ""
    assert d2.spec.products[1].price_cop == 6000
    p = m.calls[-1]["prompt"]
    assert "la de carne a 6" in p and "Arepa de carne desmechada" in p and d.question in p


def test_after_answer_never_asks_again(fake):
    raw = copy.deepcopy(ROSA_SPEC)
    raw["products"][1]["price_cop"] = 0
    m = fake(raw)
    d = compiler.compile_spec(b"ogg", "audio/ogg", [])
    d2 = compiler.compile_spec(b"", "", [], answer="no sé", previous=d)
    assert d2.question == ""
    assert [p.id for p in d2.spec.products] == ["arepa-de-choclo-con-queso"]  # unpriced dropped


def test_fulfillment_question(fake):
    raw = copy.deepcopy(ROSA_SPEC)
    raw.pop("fulfillment")
    fake(raw)
    d = compiler.compile_spec(b"ogg", "audio/ogg", [])
    assert "domicilios" in d.question


def test_blocked_by_model_and_by_keyword(fake):
    raw = copy.deepcopy(ROSA_SPEC)
    raw["blocked_category"] = "alcohol"
    fake(raw)
    d = compiler.compile_spec(b"ogg", "audio/ogg", [])
    assert d.blocked == "alcohol" and d.question == ""

    raw2 = copy.deepcopy(ROSA_SPEC)
    raw2["products"][0]["name"] = "Cerveza Club Colombia"
    raw2["products"][0]["id"] = "cerveza"
    fake(raw2)
    d2 = compiler.compile_spec(b"ogg", "audio/ogg", [])
    assert d2.blocked == "alcohol" and d2.spec.blocked_category == "alcohol"
    assert "bebidas alcohólicas" in compiler.blocked_message("alcohol")


def test_no_false_block_for_food(fake):
    raw = copy.deepcopy(ROSA_SPEC)
    raw["products"][0]["name"] = "Arma tu bowl de frutas"
    fake(raw)
    assert compiler.compile_spec(b"ogg", "audio/ogg", []).blocked is None


def test_compile_with_real_mock_module_if_present(monkeypatch):
    try:
        import genesis.muse  # noqa: F401
    except ImportError:
        pytest.skip("genesis.muse not written yet")
    monkeypatch.setenv("GENESIS_MOCK", "1")
    d = compiler.compile_spec(b"fake-ogg", "audio/ogg", [b"x"])
    assert d.spec.products and d.blocked is None


@pytest.mark.parametrize("value,expected", [
    ("cinco mil", 5000), ("a 5", 5000), ("5", 5000), ("$5.000", 5000), ("5.000", 5000), ("5000", 5000),
    ("dos mil quinientos", 2500), ("2.500", 2500), ("2.5", 2500), ("5 lucas", 5000), ("5k", 5000),
    ("mil quinientos", 1500), ("cinco mil y medio", 5500), ("veinte mil pesos", 20000), (3500, 3500),
    (None, 0), ("", 0), ("cada una a cinco mil pesos", 5000), ("quince mil", 15000), ("500", 500),
])
def test_parse_cop(value, expected):
    assert compiler.parse_cop(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("6", "06:00"), ("6:30", "06:30"), ("6am", "06:00"), ("6:30 pm", "18:30"), ("18:00", "18:00"),
    ("seis de la mañana", "06:00"), ("once de la mañana", "11:00"), ("8 de la noche", "20:00"),
    ("2 de la tarde", "14:00"), ("12 m", "12:00"), ("nada", None),
])
def test_parse_hhmm(value, expected):
    assert compiler.parse_hhmm(value) == expected


def test_summary_rosa():
    spec = BusinessSpec.model_validate(ROSA_SPEC)
    text = compiler.summary(spec)
    assert "*Arepas Rosa*" in text
    assert "Arepa de choclo con queso: $5.000" in text
    assert "San Fernando, Cali" in text
    assert "Lun a sáb, 6 a. m. a 11 a. m." in text
    assert "efectivo o link de pago" in text
    assert text.rstrip().endswith("o dime qué le cambio.")
    assert "*sí*" in text


@pytest.mark.parametrize("text", ["sí", "Si", "SIIII", "dale", "Listo!", "de una", "ok", "👍", "sí, gracias", "Dale pues", "sisas", "hágale"])
def test_is_yes(text):
    assert compiler.is_yes(text)


@pytest.mark.parametrize("text", ["no", "", "sí pero cambia el precio", "espera", "no, falta la de queso", "cuánto cuesta", "👍 pero no"])
def test_is_not_yes(text):
    assert not compiler.is_yes(text)
