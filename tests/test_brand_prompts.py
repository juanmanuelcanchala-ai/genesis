"""SAM 3.1 only grounds short nouns: the brand lane must try "arepa" before long product names."""

from genesis.lanes.brand import _prompts
from genesis.spec import BusinessSpec


def _spec(category: str, *names: str) -> BusinessSpec:
    return BusinessSpec.model_validate({
        "business_name": "Arepas Rosa", "category": category, "city": "Cali",
        "products": [{"id": f"p{i}", "name": n, "price_cop": 5000} for i, n in enumerate(names)],
    })


def test_singular_head_noun_first():
    p = _prompts(_spec("arepas", "Arepa de choclo con queso", "Arepa de carne desmechada"))
    assert p[0] == "arepa"
    assert "Arepa de choclo con queso" in p and p[-1] == "food"
    assert len(p) == len({x.lower() for x in p})


def test_other_categories():
    assert _prompts(_spec("empanadas", "Empanada de pipián"))[0] == "empanada"
    assert _prompts(_spec("jugos", "Jugo de mora"))[0] == "jugo"
