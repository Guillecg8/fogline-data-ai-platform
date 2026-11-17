"""Domain types. Money is integer cents and time is integer milliseconds, everywhere.

`Product` carries untrusted text. `ProductFacts` carries none: every field is an integer or an
identifier restricted to a fixed pattern, so policy cannot be given product text (ADR 0003).
"""

import re
from dataclasses import dataclass

SKU_PATTERN = re.compile(r"FP-[0-9]{4}")
CATEGORY_PATTERN = re.compile(r"[a-z][a-z_]{0,31}")

UNKNOWN_CATEGORY = "unknown"
KNOWN_CATEGORIES = frozenset(
    {"dry_goods", "beverages", "dairy", "produce", "cleaning", "kitchen_supplies", "alcohol"}
)


def require_int(name: str, value: object, minimum: int = 0) -> int:
    """Return `value` if it is an int (not a bool) and at least `minimum`, else raise."""
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def require_sku(value: object) -> str:
    if type(value) is not str or not SKU_PATTERN.fullmatch(value):
        raise ValueError("sku must match FP-0000")
    return value


def require_category(value: object) -> str:
    if type(value) is not str or not CATEGORY_PATTERN.fullmatch(value):
        raise ValueError("policy_category must be a lowercase identifier")
    if value != UNKNOWN_CATEGORY and value not in KNOWN_CATEGORIES:
        raise ValueError("policy_category is not a known category")
    return value


@dataclass(frozen=True)
class PriceVersion:
    price_version: int
    unit_price_cents: int
    effective_from_ms: int

    def __post_init__(self) -> None:
        require_int("price_version", self.price_version, 1)
        require_int("unit_price_cents", self.unit_price_cents, 0)
        require_int("effective_from_ms", self.effective_from_ms, 0)


@dataclass(frozen=True)
class ProductFacts:
    """Everything policy may know about a product. No text fields."""

    sku: str
    policy_category: str
    unit_price_cents: int
    price_version: int
    stock: int

    def __post_init__(self) -> None:
        require_sku(self.sku)
        require_category(self.policy_category)
        require_int("unit_price_cents", self.unit_price_cents, 0)
        require_int("price_version", self.price_version, 1)
        require_int("stock", self.stock, 0)


@dataclass(frozen=True)
class Product:
    """A catalogue product. `name`, `description` and `ingredients` are untrusted text."""

    sku: str
    name: str
    description: str
    ingredients: str
    policy_category: str
    stock: int
    prices: tuple[PriceVersion, ...]

    def __post_init__(self) -> None:
        require_sku(self.sku)
        require_category(self.policy_category)
        require_int("stock", self.stock, 0)
        for field in (self.name, self.description, self.ingredients):
            if type(field) is not str:
                raise ValueError("product text fields must be strings")
        if not self.prices or self.prices[0].effective_from_ms != 0:
            raise ValueError("the first price version must be effective from 0")
        for index, price in enumerate(self.prices):
            if price.price_version != index + 1:
                raise ValueError("price versions must be 1, 2, 3, ...")
            if index and price.effective_from_ms <= self.prices[index - 1].effective_from_ms:
                raise ValueError("price versions must take effect in increasing time order")

    def price_at(self, now_ms: int) -> PriceVersion:
        """The latest price version in effect at `now_ms`."""
        return [p for p in self.prices if p.effective_from_ms <= now_ms][-1]

    def facts_at(self, now_ms: int) -> ProductFacts:
        price = self.price_at(now_ms)
        return ProductFacts(
            sku=self.sku,
            policy_category=self.policy_category,
            unit_price_cents=price.unit_price_cents,
            price_version=price.price_version,
            stock=self.stock,
        )


@dataclass(frozen=True)
class LockedLine:
    """A cart line or quote line: a quantity at a price locked until `lock_expires_at_ms`."""

    sku: str
    quantity: int
    unit_price_cents: int
    price_version: int
    lock_expires_at_ms: int

    def __post_init__(self) -> None:
        require_sku(self.sku)
        require_int("quantity", self.quantity, 1)
        require_int("unit_price_cents", self.unit_price_cents, 0)
        require_int("price_version", self.price_version, 1)
        require_int("lock_expires_at_ms", self.lock_expires_at_ms, 0)

    @property
    def line_total_cents(self) -> int:
        return self.quantity * self.unit_price_cents

    def as_list(self) -> list[object]:
        return [
            self.sku,
            self.quantity,
            self.unit_price_cents,
            self.price_version,
            self.lock_expires_at_ms,
        ]
