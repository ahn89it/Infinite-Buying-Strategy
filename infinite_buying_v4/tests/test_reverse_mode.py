"""
test_reverse_mode.py
=====================
reverse_mode.py의 매도수량 계산, 별지점(5거래일 평균), D1/D2+ 주문 생성, 종료조건
판정을 검증합니다 (설계도 7번).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from infinite_buying_v4.reverse_mode import (
    ReverseModeError,
    generate_day1_moc_sell_order,
    generate_daily_loc_sell_order,
    is_reverse_exit_condition,
    reverse_divisor,
    reverse_sell_qty,
    reverse_star_price,
)
from infinite_buying_v4.trade_history import SELL_TYPE_REVERSE_LOC, SELL_TYPE_REVERSE_MOC


@pytest.mark.parametrize("split_count,expected", [(20, 10), (40, 20)])
def test_reverse_divisor_matches_design_doc(split_count: int, expected: int) -> None:
    assert reverse_divisor(split_count) == expected


def test_reverse_divisor_rejects_invalid_split_count() -> None:
    with pytest.raises(ReverseModeError):
        reverse_divisor(30)


def test_reverse_sell_qty_normal_case() -> None:
    # 40분할 -> 20등분. 보유 200주 / 20 = 10주.
    assert reverse_sell_qty(200, 40) == 10


def test_reverse_sell_qty_floors_down() -> None:
    # 40분할 -> 20등분. 209 // 20 = 10 (내림).
    assert reverse_sell_qty(209, 40) == 10


def test_reverse_sell_qty_minimum_one_share_when_below_divisor() -> None:
    """보유수량이 등분수보다 작아 0이 계산되면 최소 1주로 보정해야 합니다 (설계도 11번)."""
    # 40분할 -> 20등분인데 보유가 5주뿐이면 5//20=0 -> 1로 보정.
    assert reverse_sell_qty(5, 40) == 1


def test_reverse_sell_qty_rejects_zero_holding() -> None:
    with pytest.raises(ReverseModeError):
        reverse_sell_qty(0, 40)


def test_reverse_star_price_is_average_of_5_closes() -> None:
    closes = [Decimal("50.00"), Decimal("51.00"), Decimal("49.00"), Decimal("52.00"), Decimal("48.00")]
    assert reverse_star_price(closes) == Decimal("50.00")


def test_reverse_star_price_rejects_wrong_length() -> None:
    with pytest.raises(ReverseModeError):
        reverse_star_price([Decimal("50.00")] * 4)


def test_generate_day1_moc_sell_order_has_no_price() -> None:
    order = generate_day1_moc_sell_order(200, 40)
    assert order.order_kind == "MOC"
    assert order.price is None
    assert order.qty == 10
    assert order.purpose == SELL_TYPE_REVERSE_MOC


def test_generate_daily_loc_sell_order_uses_5day_average_price() -> None:
    closes = [Decimal("50.00"), Decimal("51.00"), Decimal("49.00"), Decimal("52.00"), Decimal("48.00")]
    order = generate_daily_loc_sell_order(200, 40, closes)
    assert order.order_kind == "LOC"
    assert order.price == Decimal("50.00")
    assert order.qty == 10
    assert order.purpose == SELL_TYPE_REVERSE_LOC


def test_is_reverse_exit_condition_boundary() -> None:
    avg_price = Decimal("100.00")
    threshold = avg_price * Decimal("0.85")  # 85.00
    assert is_reverse_exit_condition(threshold, avg_price) is True  # 경계값 포함
    assert is_reverse_exit_condition(threshold - Decimal("0.01"), avg_price) is False
    assert is_reverse_exit_condition(threshold + Decimal("0.01"), avg_price) is True
