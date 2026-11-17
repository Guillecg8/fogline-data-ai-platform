import hashlib
from collections import OrderedDict, namedtuple
from decimal import Decimal

import pytest

from commerce_gateway.canonical import CanonicalError, canonical, canonical_hash


def test_sorted_keys_no_whitespace_utf8():
    value = {"b": 1, "a": [1, "x", None, True, False], "é": "ñ"}
    assert canonical(value) == '{"a":[1,"x",null,true,false],"b":1,"é":"ñ"}'.encode()


def test_integers_stay_integers():
    assert canonical(1250) == b"1250"
    assert canonical(10**30) == b"1" + b"0" * 30


def test_tuple_is_encoded_exactly_as_a_list():
    assert canonical((1, ("a", None))) == canonical([1, ["a", None]]) == b'[1,["a",null]]'


@pytest.mark.parametrize("value", [1.0, 0.5, float("nan"), {"cents": 12.5}, [1, 2.0]])
def test_floats_are_rejected_anywhere(value):
    with pytest.raises(CanonicalError, match="float"):
        canonical(value)


Pair = namedtuple("Pair", "a b")


@pytest.mark.parametrize(
    "value",
    [
        {1, 2},
        frozenset({1}),
        b"bytes",
        Decimal("1.5"),
        Pair(1, 2),
        OrderedDict(a=1),
        range(3),
        {1: "non-string key"},
        object(),
    ],
)
def test_other_types_are_rejected(value):
    with pytest.raises(CanonicalError):
        canonical(value)


def test_lone_surrogate_is_rejected():
    with pytest.raises(CanonicalError, match="UTF-8"):
        canonical("\ud800")


def test_hash_is_sha256_of_canonical_bytes():
    assert canonical_hash({"a": [1]}) == hashlib.sha256(b'{"a":[1]}').hexdigest()
