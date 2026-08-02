"""
test_event_log.py
==================
event_log.py의 append/read와 T값 재생(replay) 규칙(설계도 2번)을 검증합니다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from infinite_buying_v4.event_log import (
    EVENT_FULL_BUY,
    EVENT_HALF_BUY,
    EVENT_LIMIT_SELL_THEN_LOC_BUY_FULL,
    EVENT_LIMIT_SELL_THEN_LOC_BUY_HALF,
    EVENT_QUARTER_SELL,
    EventLogError,
    apply_event,
    append_event,
    make_event,
    read_all_events,
    read_events_on_date,
    replay,
)


def test_apply_event_full_buy_increments_by_one() -> None:
    assert apply_event(Decimal("3"), EVENT_FULL_BUY) == Decimal("4")


def test_apply_event_half_buy_increments_by_half() -> None:
    assert apply_event(Decimal("3"), EVENT_HALF_BUY) == Decimal("3.5")


def test_apply_event_quarter_sell_multiplies_by_0_75() -> None:
    assert apply_event(Decimal("4"), EVENT_QUARTER_SELL) == Decimal("3.00")


def test_apply_event_limit_sell_then_loc_buy_full() -> None:
    # T = T*0.25 + 1
    assert apply_event(Decimal("8"), EVENT_LIMIT_SELL_THEN_LOC_BUY_FULL) == Decimal("3.00")


def test_apply_event_limit_sell_then_loc_buy_half() -> None:
    # T = T*0.25 + 0.5
    assert apply_event(Decimal("8"), EVENT_LIMIT_SELL_THEN_LOC_BUY_HALF) == Decimal("2.50")


def test_apply_event_unknown_type_raises() -> None:
    with pytest.raises(EventLogError):
        apply_event(Decimal("1"), "NOT_A_REAL_EVENT")


def test_make_event_rejects_unknown_event_type() -> None:
    with pytest.raises(EventLogError):
        make_event(event_date=date(2026, 8, 3), event_type="BOGUS")


def test_read_all_events_returns_empty_list_when_file_missing(tmp_path: Path) -> None:
    assert read_all_events(tmp_path / "does_not_exist.jsonl") == []


def test_append_and_read_all_events_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "event_log.jsonl"
    e1 = make_event(event_date=date(2026, 8, 3), event_type=EVENT_FULL_BUY, price=Decimal("50.12"), qty=10)
    e2 = make_event(event_date=date(2026, 8, 4), event_type=EVENT_HALF_BUY, price=Decimal("48.00"), qty=5)
    append_event(path, e1)
    append_event(path, e2)

    events = read_all_events(path)
    assert len(events) == 2
    assert events[0].event_type == EVENT_FULL_BUY
    assert events[0].price == "50.12"
    assert events[1].event_type == EVENT_HALF_BUY


def test_read_events_on_date_filters_correctly(tmp_path: Path) -> None:
    path = tmp_path / "event_log.jsonl"
    append_event(path, make_event(event_date=date(2026, 8, 3), event_type=EVENT_FULL_BUY))
    append_event(path, make_event(event_date=date(2026, 8, 4), event_type=EVENT_HALF_BUY))
    append_event(path, make_event(event_date=date(2026, 8, 4), event_type=EVENT_QUARTER_SELL))

    events_on_8_4 = read_events_on_date(path, date(2026, 8, 4))
    assert [e.event_type for e in events_on_8_4] == [EVENT_HALF_BUY, EVENT_QUARTER_SELL]


def test_read_all_events_raises_on_corrupted_line(tmp_path: Path) -> None:
    path = tmp_path / "event_log.jsonl"
    path.write_text("{not valid json\n", encoding="utf-8")
    with pytest.raises(EventLogError):
        read_all_events(path)


def test_replay_applies_events_in_order() -> None:
    """T=0에서 시작해 첫매수(FULL_BUY) 후 절반매수(HALF_BUY)가 이어지면 T=1.5가 되어야 합니다."""
    events = [
        make_event(event_date=date(2026, 8, 3), event_type=EVENT_FULL_BUY),
        make_event(event_date=date(2026, 8, 4), event_type=EVENT_HALF_BUY),
    ]
    result = replay(Decimal(0), events)
    assert result == Decimal("1.5")


def test_replay_empty_events_returns_t0_unchanged() -> None:
    assert replay(Decimal("5.5"), []) == Decimal("5.5")
