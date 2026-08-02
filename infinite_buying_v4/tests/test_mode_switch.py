"""
test_mode_switch.py
====================
normal_mode.py의 매수/매도 주문 생성 로직과, 일반모드<->리버스모드 전환 경계값을
검증합니다 (설계도 5, 6번 + 3번 경계값).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from infinite_buying_v4.formulas import is_reverse_trigger
from infinite_buying_v4.normal_mode import (
    NormalModeError,
    generate_aux_ladder_buy_orders,
    generate_first_buy_orders,
    generate_first_half_buy_orders,
    generate_second_half_buy_orders,
    generate_sell_orders,
    place_decoy_order,
)
from infinite_buying_v4.trade_history import (
    BUY_TYPE_FIRST,
    BUY_TYPE_FULL_STAR,
    BUY_TYPE_HALF_AVG,
    BUY_TYPE_HALF_STAR,
    SELL_TYPE_LIMIT_15PCT,
    SELL_TYPE_QUARTER,
)


def test_place_decoy_order_is_12pct_above_prev_close_and_marked_decoy() -> None:
    order = place_decoy_order(Decimal("50.00"))
    assert order.is_decoy is True
    assert order.price == Decimal("56.00")  # 50 * 1.12
    assert order.qty == 1


def test_generate_first_buy_orders_includes_decoy_and_ladder() -> None:
    orders = generate_first_buy_orders(Decimal("50.00"), Decimal("10000"), split_count=40, ladder_steps=4)
    assert orders[0].is_decoy is True
    ladder_orders = orders[1:]
    assert len(ladder_orders) == 4
    # 사다리 가격은 아래로 갈수록 낮아져야 합니다.
    prices = [o.price for o in ladder_orders]
    assert prices == sorted(prices, reverse=True)
    for o in ladder_orders:
        assert o.side == "BUY"
        assert o.purpose == BUY_TYPE_FIRST
        assert o.is_decoy is False


def test_generate_first_half_buy_orders_splits_budget_half_half() -> None:
    avg_price = Decimal("50.00")
    t = Decimal("5")
    split_count = 40
    orders = generate_first_half_buy_orders(avg_price, Decimal("10000"), t, split_count)

    purposes = {o.purpose for o in orders}
    assert purposes == {BUY_TYPE_HALF_STAR, BUY_TYPE_HALF_AVG}

    avg_order = next(o for o in orders if o.purpose == BUY_TYPE_HALF_AVG)
    assert avg_order.price == avg_price

    star_order = next(o for o in orders if o.purpose == BUY_TYPE_HALF_STAR)
    assert star_order.price > avg_price  # 전반전: 별지점이 평단보다 위


def test_generate_first_half_buy_orders_rejects_second_half_t() -> None:
    with pytest.raises(NormalModeError):
        generate_first_half_buy_orders(Decimal("50.00"), Decimal("10000"), Decimal("30"), 40)


def test_generate_second_half_buy_orders_uses_full_budget_at_star_price() -> None:
    avg_price = Decimal("50.00")
    t = Decimal("25")  # 40분할 후반전(20 초과), 리버스 트리거(39) 미만
    split_count = 40
    orders = generate_second_half_buy_orders(avg_price, Decimal("10000"), t, split_count)

    assert len(orders) == 1
    assert orders[0].purpose == BUY_TYPE_FULL_STAR
    assert orders[0].price < avg_price  # 후반전: 별지점이 평단보다 아래


def test_generate_second_half_buy_orders_rejects_first_half_t() -> None:
    with pytest.raises(NormalModeError):
        generate_second_half_buy_orders(Decimal("50.00"), Decimal("10000"), Decimal("5"), 40)


def test_generate_second_half_buy_orders_rejects_reverse_trigger_t() -> None:
    """T > 분할수-1이면 이미 리버스모드 전환 조건이므로 일반모드 매수를 거부해야 합니다 (설계도 5-3번)."""
    split_count = 40
    t = Decimal(split_count) - Decimal(1) + Decimal("0.01")
    assert is_reverse_trigger(t, split_count) is True
    with pytest.raises(NormalModeError):
        generate_second_half_buy_orders(Decimal("50.00"), Decimal("10000"), t, split_count)


def test_generate_second_half_buy_orders_at_exact_boundary_still_allowed() -> None:
    """T == 분할수-1 (경계값)은 아직 리버스 트리거가 아니므로 정상적으로 매수 주문이 생성돼야 합니다."""
    split_count = 40
    t = Decimal(split_count) - Decimal(1)
    assert is_reverse_trigger(t, split_count) is False
    orders = generate_second_half_buy_orders(Decimal("50.00"), Decimal("10000"), t, split_count)
    assert len(orders) == 1


def test_generate_sell_orders_quarter_and_limit_15pct() -> None:
    avg_price = Decimal("50.00")
    holding_qty = 100
    t = Decimal("10")
    split_count = 40
    orders = generate_sell_orders(avg_price, holding_qty, t, split_count)

    quarter = next(o for o in orders if o.purpose == SELL_TYPE_QUARTER)
    limit15 = next(o for o in orders if o.purpose == SELL_TYPE_LIMIT_15PCT)

    assert quarter.qty == 25
    assert limit15.qty == 75
    assert limit15.price == Decimal("57.50")  # 50 * 1.15
    assert quarter.order_kind == "LOC"
    assert limit15.order_kind == "LIMIT"


def test_generate_sell_orders_small_holding_skips_quarter_sell() -> None:
    """보유수량이 4주 미만이면 쿼터매도(1/4)가 0주가 되므로 지정가매도만 생성돼야 합니다."""
    orders = generate_sell_orders(Decimal("50.00"), 3, Decimal("10"), 40)
    purposes = {o.purpose for o in orders}
    assert purposes == {SELL_TYPE_LIMIT_15PCT}
    assert orders[0].qty == 3


def test_generate_sell_orders_rejects_zero_holding() -> None:
    with pytest.raises(NormalModeError):
        generate_sell_orders(Decimal("50.00"), 0, Decimal("10"), 40)


def test_generate_aux_ladder_buy_orders_distributes_budget_evenly() -> None:
    orders = generate_aux_ladder_buy_orders(
        Decimal("50.00"), Decimal("1000"), ladder_steps=5, ladder_step_pct=Decimal("0.05")
    )
    assert len(orders) == 5
    prices = [o.price for o in orders]
    assert prices == sorted(prices, reverse=True)


def test_generate_aux_ladder_buy_orders_rejects_non_positive_steps() -> None:
    with pytest.raises(NormalModeError):
        generate_aux_ladder_buy_orders(Decimal("50.00"), Decimal("1000"), ladder_steps=0, ladder_step_pct=Decimal("0.05"))
