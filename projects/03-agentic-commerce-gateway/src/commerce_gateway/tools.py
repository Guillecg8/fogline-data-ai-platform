"""The MCP tool surface (ADR 0003) and validation of raw tool arguments (ADR 0004).

Arguments are validated here, in the gateway, not by the MCP SDK, so a malformed call reaches the
gateway and is counted and audited as `invalid_arguments`.
"""

from dataclasses import dataclass

from commerce_gateway.canonical import sha256_hex
from commerce_gateway.models import SKU_PATTERN

TOOL_NAMES = (
    "get_policy",
    "search_products",
    "get_product",
    "add_to_cart",
    "remove_from_cart",
    "view_cart",
    "request_checkout",
    "get_checkout_status",
)

# Limits on what one tool call can ask for. Longer or larger requests are rejected, not cut.
QUERY_MAX_CHARS = 200  # longest search_products query accepted
MAX_QUANTITY_PER_CALL = 99  # most units one add_to_cart or remove_from_cart call can move
SEARCH_MAX_RESULTS = 20  # most products one search_products call returns

_SKU = "sku"
_QUANTITY = "quantity"
_QUERY = "query"

TOOL_PARAMETERS: dict[str, tuple[str, ...]] = {
    "get_policy": (),
    "search_products": (_QUERY,),
    "get_product": (_SKU,),
    "add_to_cart": (_SKU, _QUANTITY),
    "remove_from_cart": (_SKU, _QUANTITY),
    "view_cart": (),
    "request_checkout": (),
    "get_checkout_status": (),
}


# Closed vocabulary for describing a rejected value in the audit trail. The raw value itself is
# never stored: only which field it was, what was expected, its JSON type, its length and a hash.
FIELD_TOOL = "tool"
FIELD_ARGUMENTS = "arguments"
FIELDS = (FIELD_TOOL, FIELD_ARGUMENTS, _SKU, _QUANTITY, _QUERY)
EXPECTED = (
    "tool_name",
    "object",
    "sku_pattern",
    "known_sku",
    "sku_in_cart",
    "integer_1_to_99",
    "quantity_in_cart",
    "non_empty_text",
    "text_within_limit",
)


def _json_type(value: object) -> str:
    kinds = {bool: "boolean", int: "integer", float: "float", str: "string", type(None): "null"}
    if type(value) in kinds:
        return kinds[type(value)]
    if type(value) in (list, tuple):
        return "array"
    return "object" if type(value) is dict else "other"


@dataclass(frozen=True)
class RejectedValue:
    """What the audit trail may say about a rejected value. Never the value itself."""

    field: str
    expected: str
    received: str
    length: int | None
    sha256: str | None

    def __post_init__(self) -> None:
        if self.field not in FIELDS or self.expected not in EXPECTED:
            raise ValueError("rejected value outside the closed vocabulary")

    @classmethod
    def of(cls, field: str, expected: str, value: object) -> "RejectedValue":
        length = len(value) if type(value) in (str, list, tuple, dict) else None
        digest = None
        if type(value) is str:
            digest = sha256_hex(value.encode("utf-8", errors="surrogatepass"))
        return cls(field, expected, _json_type(value), length, digest)

    def as_payload(self) -> dict[str, object]:
        return {
            "field": self.field,
            "expected": self.expected,
            "received": self.received,
            "length": self.length,
            "sha256": self.sha256,
        }


class InvalidArguments(Exception):
    """A malformed tool call. `reason` is fixed text; `rejected` describes the value safely."""

    def __init__(self, reason: str, rejected: RejectedValue) -> None:
        super().__init__(reason)
        self.reason = reason
        self.rejected = rejected

    @property
    def audit(self) -> dict[str, object]:
        return self.rejected.as_payload()


def _reject(reason: str, field: str, expected: str, value: object) -> InvalidArguments:
    return InvalidArguments(reason, RejectedValue.of(field, expected, value))


def _validate_query(value: object) -> str:
    if type(value) is not str:
        raise _reject("query must be a string", _QUERY, "non_empty_text", value)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _reject("query is not valid text", _QUERY, "non_empty_text", value) from exc
    if not value.strip():
        raise _reject("query must not be empty", _QUERY, "non_empty_text", value)
    if len(value) > QUERY_MAX_CHARS:
        raise _reject("query is too long", _QUERY, "text_within_limit", value)
    return value


def validate_arguments(tool: object, arguments: object) -> dict[str, object]:
    """Return validated arguments for `tool`, or raise `InvalidArguments` with a fixed reason."""
    if type(tool) is not str or tool not in TOOL_PARAMETERS:
        raise _reject("unknown tool", FIELD_TOOL, "tool_name", tool)
    if arguments is None:
        arguments = {}
    if type(arguments) is not dict:
        raise _reject("arguments must be an object", FIELD_ARGUMENTS, "object", arguments)
    expected = TOOL_PARAMETERS[tool]
    if set(arguments) != set(expected):
        raise _reject("unexpected or missing arguments", FIELD_ARGUMENTS, "object", arguments)
    clean: dict[str, object] = {}
    if _SKU in expected:
        sku = arguments[_SKU]
        if type(sku) is not str or not SKU_PATTERN.fullmatch(sku):
            raise _reject("sku must look like FP-0000", _SKU, "sku_pattern", sku)
        clean[_SKU] = sku
    if _QUANTITY in expected:
        quantity = arguments[_QUANTITY]
        if type(quantity) is not int or not 1 <= quantity <= MAX_QUANTITY_PER_CALL:
            reason = f"quantity must be an integer from 1 to {MAX_QUANTITY_PER_CALL}"
            raise _reject(reason, _QUANTITY, "integer_1_to_99", quantity)
        clean[_QUANTITY] = quantity
    if _QUERY in expected:
        clean[_QUERY] = _validate_query(arguments[_QUERY])
    return clean
