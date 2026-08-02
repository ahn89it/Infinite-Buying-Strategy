"""
test_formulas.py
=================
formulas.py의 순수함수들을 검증하는 단위 테스트입니다.

설계도 3번은 "20분할/40분할 각각 T=1~40 전 구간 검증"을 명시적으로 요구하므로,
star_percent()/is_first_half()/is_reverse_trigger()는 전 구간 반복 테스트로,
그 외 함수들은 대표 케이스 + 경계값 위주로 검증합니다.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from infinite_buying_v4.formulas import (
    FormulaError,
    buy_trigger_price,
    is_first_half,
    is_reverse_trigger,
    new_average_price,
    profit_amount,
    return_pct,
    sell_trigger_price,
    single_buy_amount,
    star_percent,
    star_price,
)


def _explicit_star_percent(t: Decimal, split_count: int) -> Decimal:
    """설계도 3번에 적힌 원래 공식을 "그대로" 코드로 옮긴 버전.

    formulas.py의 star_percent()는 계수를 일반화한 구현이므로, 이 함수와
    나란히 비교해 두 식이 정말 동일한 결과를 내는지 전 구간에서 검증합니다.
    """
    if split_count == 40:
        return Decimal(15) - Decimal("0.75") * t
    if split_count == 20:
        return Decimal(15) - Decimal("1.5") * t
    raise ValueError("이 헬퍼는 20/40분할만 지원합니다.")


@pytest.mark.parametrize("split_count", [20, 40])
def test_star_percent_matches_explicit_formula_full_range(split_count: int) -> None:
    """T=0~40(0.5 스텝 포함)까지 설계도 원문 공식과 정확히 일치하는지 확인합니다."""
    t = Decimal(0)
    step = Decimal("0.5")
    while t <= Decimal(40):
        expected = _explicit_star_percent(t, split_count)
        actual = star_percent(t, split_count)
        assert actual == expected, f"split={split_count}, T={t}: {actual} != {expected}"
        t += step


@pytest.mark.parametrize("split_count", [20, 40])
def test_is_first_half_full_range_boundary(split_count: int) -> None:
    """T가 split_count/2 이하이면 전반전, 초과하면 후반전이어야 합니다 (경계값 포함 전 구간)."""
    half = Decimal(split_count) / Decimal(2)
    t = Decimal(0)
    step = Decimal("0.5")
    while t <= Decimal(split_count):
        expected_first_half = t <= half
        assert is_first_half(t, split_count) == expected_first_half, f"T={t}"
        t += step


@pytest.mark.parametrize("split_count", [20, 40])
def test_is_reverse_trigger_boundary(split_count: int) -> None:
    """T > split_count - 1 에서만 리버스모드 트리거가 True여야 합니다 (설계도 3, 7-1번)."""
    boundary = Decimal(split_count) - Decimal(1)
    assert is_reverse_trigger(boundary, split_count) is False  # 경계값 자체는 아직 트리거 아님
    assert is_reverse_trigger(boundary + Decimal("0.0001"), split_count) is True
    assert is_reverse_trigger(boundary - Decimal("0.0001"), split_count) is False


def test_star_price_and_trigger_prices_first_half() -> None:
    """전반전(T <= split/2)에서는 별%가 양수라 별지점이 평단보다 높아야 합니다."""
    avg_price = Decimal("50.00")
    t = Decimal(5)
    split_count = 40
    price = star_price(avg_price, t, split_count)
    assert price > avg_price  # 전반전: 목표가가 평단보다 위

    # 매수점 = 별지점 - 0.01, 매도점 = 별지점 (설계도 3번)
    assert buy_trigger_price(avg_price, t, split_count) == price - Decimal("0.01")
    assert sell_trigger_price(avg_price, t, split_count) == price


def test_star_price_second_half_goes_below_average() -> None:
    """후반전(T > split/2)에서는 별%가 음수로 전환되어 목표가가 평단보다 낮아야 합니다."""
    avg_price = Decimal("50.00")
    t = Decimal(30)  # 40분할 기준 후반전 (20 초과)
    split_count = 40
    price = star_price(avg_price, t, split_count)
    assert price < avg_price


def test_single_buy_amount_normal_case() -> None:
    remaining_cash = Decimal("10000")
    t = Decimal(5)
    split_count = 40
    amount = single_buy_amount(remaining_cash, t, split_count)
    assert amount == remaining_cash / (Decimal(split_count) - t)


@pytest.mark.parametrize("split_count", [20, 40])
def test_single_buy_amount_zero_or_negative_denominator_raises(split_count: int) -> None:
    """T가 분할수 이상이면 분모가 0 이하가 되어 예외가 발생해야 합니다 (설계도 4번 방어 코드)."""
    remaining_cash = Decimal("10000")
    with pytest.raises(FormulaError):
        single_buy_amount(remaining_cash, Decimal(split_count), split_count)
    with pytest.raises(FormulaError):
        single_buy_amount(remaining_cash, Decimal(split_count) + Decimal(1), split_count)


def test_new_average_price_first_buy_uses_buy_price_directly() -> None:
    """보유수량이 0인 첫 매수는 가중평균 없이 체결가가 그대로 평단가가 됩니다."""
    result = new_average_price(Decimal(0), 0, Decimal("55.5555"), 10)
    assert result == Decimal("55.56")  # 센트 단위 반올림(ROUND_HALF_UP)


def test_new_average_price_weighted_average() -> None:
    """기존 10주 평단 50 + 신규 10주 체결가 60 => 평단 55."""
    result = new_average_price(Decimal("50.00"), 10, Decimal("60.00"), 10)
    assert result == Decimal("55.00")


def test_new_average_price_rejects_non_positive_qty() -> None:
    with pytest.raises(FormulaError):
        new_average_price(Decimal("50.00"), 10, Decimal("60.00"), 0)


def test_return_pct_and_profit_amount() -> None:
    sell_price = Decimal("60.00")
    avg_price_at_sell = Decimal("50.00")
    sell_qty = 10

    pct = return_pct(sell_price, avg_price_at_sell)
    assert pct == Decimal("20")  # (60-50)/50*100 = 20%

    profit = profit_amount(sell_price, avg_price_at_sell, sell_qty)
    assert profit == Decimal("100.00")  # (60-50)*10 = 100


def test_return_pct_rejects_zero_avg_price() -> None:
    with pytest.raises(FormulaError):
        return_pct(Decimal("60.00"), Decimal("0"))
