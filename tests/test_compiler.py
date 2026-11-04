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


# ---------------------------------------------------------------- regressions from the first real-model run (4-nov)
# fixtures/rosa_real_run.json: the real Muse Voice Transcribe output (2 coarse turns) and the two spec.ready
# payloads of media/rehearsal/run.json, replayed as model output. No network.

REAL = json.loads((FIX / "rosa_real_run.json").read_text())


class RealTranscriptMuse(FakeMuse):
    def transcribe(self, audio, mime="audio/ogg"):
        self.transcribed += 1
        tr = REAL["transcript"]
        return Transcript(tr["text"], tr["segments"], tr["model"])


@pytest.fixture
def real(monkeypatch):
    def install(*responses):
        m = RealTranscriptMuse(responses)
        monkeypatch.setitem(sys.modules, "genesis.muse", m)
        return m
    return install


def test_real_answer_cannot_corrupt_previous_spec(real):
    m = real(REAL["first_raw"])
    d = compiler.compile_spec(b"ogg", "audio/ogg", [b"p0", b"p1"])
    assert d.question == REAL["question"]
    m.responses = [REAL["second_raw"]]  # 3x "Arepa de choclo con queso", no carne, no owner, no neighborhood
    d2 = compiler.compile_spec(b"", "audio/ogg", [b"p0", b"p1"], answer=REAL["answer"], previous=d)
    s = d2.spec
    assert [p.name for p in s.products] == ["Arepa de choclo con queso", "Arepa de carne desmechada"]
    assert [p.id for p in s.products] == ["arepa-choclo-con-queso", "arepa-carne-desmechada"]
    assert [p.price_cop for p in s.products] == [5000, 5000]
    assert (s.owner_name, s.neighborhood, s.city) == ("Rosa", "San Fernando", "Cali")
    assert s.fulfillment.value == "pickup" and "fulfillment" not in s.missing
    assert d2.question == "" and s.clarifying_question == ""
    assert "desmechada" in s.description and s.description.count("choclo") == 1


def test_duplicate_products_are_merged_by_normalized_name(real):
    raw = copy.deepcopy(REAL["second_raw"])
    raw["products"][1]["name"] = "arepa de  choclo con queso."
    raw["products"][0]["price_cop"] = 0
    real(raw)
    s = compiler.compile_spec(b"ogg", "audio/ogg", []).spec
    choclo = [p for p in s.products if "choclo" in p.name]
    assert len(choclo) == 1 and choclo[0].price_cop == 5000 and choclo[0].id == "arepa-choclo-con-queso"


def test_answer_patches_only_what_it_names(real):
    m = real(REAL["first_raw"])
    d = compiler.compile_spec(b"ogg", "audio/ogg", [])
    new = copy.deepcopy(REAL["first_raw"])
    new["business_name"] = "Arepas Doña Rosa"  # not in the answer: must not change
    new["products"].append({"id": "jugo-de-mango", "name": "Jugo de mango", "price_cop": 3000})
    new["products"][1]["price_cop"] = 9000  # the answer does not mention carne: keep 5000
    new["fulfillment"] = "both"
    m.responses = [new]
    d2 = compiler.compile_spec(b"", "", [], answer="Hago domicilios también, y vendo jugo de mango a 3 mil", previous=d)
    s = d2.spec
    assert s.business_name == "Arepas Rosa"
    assert [(p.name, p.price_cop) for p in s.products] == [
        ("Arepa de choclo con queso", 5000), ("Arepa de carne desmechada", 5000), ("Jugo de mango", 3000)]
    assert s.fulfillment.value == "both"


def test_answer_when_model_output_is_invalid_still_applies(real):
    m = real(REAL["first_raw"])
    d = compiler.compile_spec(b"ogg", "audio/ogg", [])
    m.responses = [{"business_name": "X", "category": "x", "products": []}]
    d2 = compiler.compile_spec(b"", "", [], answer="no hago domicilios, solo para recoger", previous=d)
    assert len(d2.spec.products) == 2 and d2.spec.fulfillment.value == "pickup" and d2.question == ""


def test_hours_fallback_from_transcript_when_model_leaves_them_null(real):
    m = real(REAL["first_raw"])  # hours: null, as the real model returned
    d = compiler.compile_spec(b"ogg", "audio/ogg", [])
    h = d.spec.hours
    assert h is not None
    assert (h.days, h.open, h.close) == (["mon", "tue", "wed", "thu", "fri", "sat"], "06:00", "11:00")
    assert h.evidence.quote == "abro de lunes a sábado de seis a once de la mañana"
    assert 9.0 <= h.evidence.source_s <= 10.5
    assert "hours" not in d.spec.missing
    assert "Lun a sáb, 6 a. m. a 11 a. m." in compiler.summary(d.spec)
    m.responses = [REAL["second_raw"]]
    d2 = compiler.compile_spec(b"", "", [], answer=REAL["answer"], previous=d)
    assert (d2.spec.hours.open, d2.spec.hours.close) == ("06:00", "11:00")


@pytest.mark.parametrize("text,expected", [
    ("abro de lunes a sábado de seis a once de la mañana", (["mon", "tue", "wed", "thu", "fri", "sat"], "06:00", "11:00")),
    ("atiendo de lunes a viernes de 2 a 8 de la noche", (["mon", "tue", "wed", "thu", "fri"], "14:00", "20:00")),
    ("todos los días de 7 a 2 de la tarde", (["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "07:00", "14:00")),
    ("los sábados y domingos desde las 8 hasta las 12", (["sat", "sun"], "08:00", "12:00")),
    ("de martes a domingo de 5 a 10 de la noche", (["tue", "wed", "thu", "fri", "sat", "sun"], "17:00", "22:00")),
])
def test_extract_hours(text, expected):
    h = compiler.extract_hours(text)
    assert (h["days"], h["open"], h["close"]) == expected


@pytest.mark.parametrize("text", ["cada una a cinco mil pesos", "no tengo horario fijo", ""])
def test_extract_hours_none(text):
    assert compiler.extract_hours(text) is None


def test_evidence_seconds_interpolated_inside_coarse_turns(real):
    real(REAL["first_raw"])  # quotes empty, source_s 0.0 everywhere, as the real model returned
    s = compiler.compile_spec(b"ogg", "audio/ogg", []).spec
    ev = {k: v.source_s for k, v in s.evidence.items()}
    assert 1.5 <= ev["neighborhood"] <= 2.5  # "San Fernando" ~2 s into the 0-6.86 s turn
    assert 0.0 <= ev["owner_name"] <= 1.0  # "soy Rosa"
    assert 17.0 <= ev["business_name"] <= 19.26  # "Arepas Rosa" at the end of the 7.42-19.26 s turn
    choclo, carne = (p.evidence.source_s for p in s.products)
    assert 3.0 <= choclo < carne <= 6.86
    assert 9.0 <= s.hours.evidence.source_s <= 10.5
    assert ev["payment_methods"] > 10.0


def test_locate_interpolates_only_coarse_segments():
    segs = REAL["transcript"]["segments"]
    assert 7.9 <= compiler._locate(segs, "cinco mil pesos") <= 9.0
    assert compiler._locate(ROSA_TR["segments"], "cinco mil pesos") == 4.2  # fine segments: segment start
    assert compiler._snap(segs, 12.3) == 12.3 and compiler._snap(ROSA_TR["segments"], 12.3) == 9.8


def test_thin_first_compile_is_backfilled_from_transcript(monkeypatch):
    """Second real run: the model returned products [choclo, 'x' at $0], no owner, no neighborhood, no hours."""
    tr = REAL["thin_transcript"]
    m = FakeMuse([REAL["thin_raw"]])
    m.transcribe = lambda audio, mime="audio/ogg": Transcript(tr["text"], tr["segments"], tr["model"])
    monkeypatch.setitem(sys.modules, "genesis.muse", m)
    d = compiler.compile_spec(b"ogg", "audio/ogg", [b"p0", b"p1"])
    s = d.spec
    assert [(p.name, p.price_cop) for p in s.products] == [
        ("Arepa de choclo con queso", 5000), ("Arepa de carne desmechada", 5000)]
    assert (s.owner_name, s.neighborhood) == ("Rosa", "San Fernando")
    assert (s.hours.open, s.hours.close) == ("06:00", "11:00")
    assert not any(x.startswith("price:") for x in s.missing)
    assert "desmechada" in s.description and ", x." not in s.description
    assert d.question == "¿Tus clientes recogen el pedido en el puesto, o también haces domicilios?"
