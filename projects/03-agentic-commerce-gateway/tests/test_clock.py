import re
from pathlib import Path

import pytest

import commerce_gateway
from commerce_gateway.clock import FixedClock, SystemClock


def test_fixed_clock_moves_only_when_told():
    clock = FixedClock(1_000)
    assert clock.now_ms() == 1_000
    clock.advance(500)
    assert clock.now_ms() == 1_500
    clock.set(200)
    assert clock.now_ms() == 200
    with pytest.raises(ValueError):
        clock.advance(-1)


def test_system_clock_returns_integer_milliseconds():
    now = SystemClock().now_ms()
    assert type(now) is int and now > 1_700_000_000_000


_TIME_READS = re.compile(r"datetime\.now|datetime\.utcnow|time\.time|time\.monotonic|date\.today")


def test_only_system_clock_reads_the_real_time():
    package = Path(commerce_gateway.__file__).parent
    offenders = [
        path.name
        for path in package.rglob("*.py")
        if path.name != "clock.py" and _TIME_READS.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
