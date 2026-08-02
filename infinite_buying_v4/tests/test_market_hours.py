"""
test_market_hours.py
=====================
market_hours.py의 서머타임 자동판별과 장 시간대(프리장/본장/애프터/주간거래) 판별,
주문 가능 여부 판정을 검증합니다 (설계도 6, 11번).
"""

from __future__ import annotations

from datetime import datetime, timezone

from infinite_buying_v4.market_hours import (
    can_place_limit_sell_order,
    can_place_loc_or_moc_order,
    get_session,
    is_day_market_session,
    is_dst_now,
    is_weekday,
)


def _utc(y, m, d, h, mi=0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def test_is_dst_now_true_in_july() -> None:
    # 2026-07-15 12:00 UTC는 미국 동부 서머타임(EDT, UTC-4) 적용 기간입니다.
    assert is_dst_now(_utc(2026, 7, 15, 12, 0)) is True


def test_is_dst_now_false_in_january() -> None:
    # 2026-01-15는 서머타임이 아닌 기간(EST, UTC-5)입니다.
    assert is_dst_now(_utc(2026, 1, 15, 12, 0)) is False


def test_get_session_regular_hours_edt() -> None:
    # 서머타임 기간: 14:00 UTC = 10:00 EDT -> 본장(REGULAR, 09:30~16:00)
    assert get_session(_utc(2026, 7, 15, 14, 0)) == "REGULAR"


def test_get_session_premarket_edt() -> None:
    # 서머타임 기간: 09:00 UTC = 05:00 EDT -> 프리장(04:00~09:30)
    assert get_session(_utc(2026, 7, 15, 9, 0)) == "PREMARKET"


def test_get_session_afterhours_edt() -> None:
    # 서머타임 기간: 21:00 UTC = 17:00 EDT -> 애프터(16:00~20:00)
    assert get_session(_utc(2026, 7, 15, 21, 0)) == "AFTERHOURS"


def test_get_session_day_market_overnight() -> None:
    # 서머타임 기간: 02:00 UTC = 전날 22:00 EDT -> 주간거래(20:00~04:00)
    assert get_session(_utc(2026, 7, 16, 2, 0)) == "DAY_MARKET"


def test_get_session_regular_hours_est_winter() -> None:
    # 비서머타임 기간(EST, UTC-5): 15:00 UTC = 10:00 EST -> 본장
    assert get_session(_utc(2026, 1, 15, 15, 0)) == "REGULAR"


def test_can_place_loc_or_moc_order_only_during_regular_session() -> None:
    regular = _utc(2026, 7, 15, 14, 0)  # 10:00 EDT, 수요일
    day_market = _utc(2026, 7, 16, 2, 0)  # 22:00 EDT, 수요일 밤(목요일 새벽 UTC)
    assert can_place_loc_or_moc_order(regular) is True
    assert can_place_loc_or_moc_order(day_market) is False


def test_can_place_limit_sell_order_covers_pre_regular_after() -> None:
    premarket = _utc(2026, 7, 15, 9, 0)
    regular = _utc(2026, 7, 15, 14, 0)
    afterhours = _utc(2026, 7, 15, 21, 0)
    day_market = _utc(2026, 7, 16, 2, 0)
    assert can_place_limit_sell_order(premarket) is True
    assert can_place_limit_sell_order(regular) is True
    assert can_place_limit_sell_order(afterhours) is True
    assert can_place_limit_sell_order(day_market) is False


def test_day_market_orders_always_blocked() -> None:
    """설계도 6번 "주간거래 시간대 주문 절대 금지"를 두 주문 함수 모두에서 검증합니다."""
    day_market = _utc(2026, 7, 16, 2, 0)
    assert is_day_market_session(day_market) is True
    assert can_place_loc_or_moc_order(day_market) is False
    assert can_place_limit_sell_order(day_market) is False


def test_is_weekday_detects_weekend() -> None:
    # 2026-08-01은 토요일입니다.
    saturday = _utc(2026, 8, 1, 14, 0)
    assert is_weekday(saturday) is False
    # 2026-08-03은 월요일입니다.
    monday = _utc(2026, 8, 3, 14, 0)
    assert is_weekday(monday) is True


def test_weekend_blocks_order_placement_even_during_regular_hour_range() -> None:
    """시각만 본장 시간대와 같아도 주말이면 주문이 금지되어야 합니다."""
    saturday_regular_hour = _utc(2026, 8, 1, 14, 0)
    assert can_place_loc_or_moc_order(saturday_regular_hour) is False
