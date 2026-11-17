"""The versioned fixture catalogue: fictional products, some with injection text on purpose."""

import json
from dataclasses import dataclass
from pathlib import Path

from commerce_gateway.canonical import canonical_hash
from commerce_gateway.models import PriceVersion, Product

CATALOGUE_FILE = Path(__file__).parent / "fixtures" / "catalogue_v1.json"

_DOCUMENT_KEYS = {"catalogue_version", "note", "products"}
_PRODUCT_KEYS = {"sku", "policy_category", "name", "description", "ingredients", "stock", "prices"}
_PRICE_KEYS = {"price_version", "unit_price_cents", "effective_from_ms"}


@dataclass(frozen=True)
class Catalogue:
    """`sha256` is over the canonical form of the parsed document, not the file bytes, so line
    endings, indentation and key order in the file do not change it."""

    version: int
    sha256: str
    products: tuple[Product, ...]


def _reject_float(text: str) -> object:
    raise ValueError(f"floats are not allowed in the catalogue: {text}")


def _exact_keys(obj: object, keys: set[str], what: str) -> dict:
    if not isinstance(obj, dict) or set(obj) != keys:
        raise ValueError(f"{what} must have exactly the keys {sorted(keys)}")
    return obj


def parse_catalogue(data: bytes) -> Catalogue:
    """Parse and validate catalogue bytes. Floats, unknown keys and duplicate SKUs are rejected."""
    doc = _exact_keys(
        json.loads(data.decode("utf-8"), parse_float=_reject_float, parse_constant=_reject_float),
        _DOCUMENT_KEYS,
        "catalogue",
    )
    if doc["catalogue_version"] != 1:
        raise ValueError("unsupported catalogue_version")
    products = []
    for raw in doc["products"]:
        item = _exact_keys(raw, _PRODUCT_KEYS, "product")
        prices = tuple(PriceVersion(**_exact_keys(p, _PRICE_KEYS, "price")) for p in item["prices"])
        products.append(Product(**{**item, "prices": prices}))
    skus = [p.sku for p in products]
    if len(set(skus)) != len(skus):
        raise ValueError("duplicate sku in catalogue")
    return Catalogue(version=1, sha256=canonical_hash(doc), products=tuple(products))


def load_catalogue(path: Path = CATALOGUE_FILE) -> Catalogue:
    return parse_catalogue(path.read_bytes())
