"""
formulas.py
===========
무한매수법 V4.0의 수학 공식을 담은 "순수함수" 모듈입니다.

왜 순수함수로 분리하는가?
- 이 모듈의 함수들은 전부 입력값만으로 출력값이 결정되고(같은 입력 -> 항상 같은 출력),
  DB나 네트워크 같은 부수효과가 전혀 없습니다. 그래서 단위 테스트로 20분할/40분할
  T=1~40 전 구간을 손쉽게 검증할 수 있습니다(설계도 3번 "단위 테스트로... 전 구간 검증").
- state.py/normal_mode.py/reverse_mode.py 등 "언제, 얼마에" 주문을 낼지 결정하는
  로직들은 전부 이 모듈의 함수를 호출해서 숫자를 얻어오기만 하고, 계산식 자체를
  중복 구현하지 않습니다.
- 모든 계산은 Decimal로 수행합니다(설계도 2번). float으로 계산하면 0.1 + 0.2 != 0.3류의
  누적 오차가 생겨, 장기간 반복되는 자동매매에서 원금/수량 계산이 미세하게 어긋날 수
  있기 때문입니다.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# 무한매수법은 20분할 또는 40분할만 지원합니다(설계도 1, 3, 4번).
VALID_SPLIT_COUNTS = (20, 40)

# 주문가/체결가는 미국 주식 관행상 소수점 둘째 자리(센트)까지 사용합니다.
CENT = Decimal("0.01")


class FormulaError(Exception):
    """공식 계산이 불가능한 입력(예: 잘못된 분할수, 매수 불가 구간의 T값)이 들어왔을 때 발생시키는 예외."""


def _validate_split_count(split_count: int) -> None:
    if split_count not in VALID_SPLIT_COUNTS:
        raise FormulaError(
            f"split_count는 {VALID_SPLIT_COUNTS} 중 하나여야 합니다 (입력값: {split_count})."
        )


def star_percent(t: Decimal, split_count: int) -> Decimal:
    """별% (목표가 산출을 위한 평단가 대비 퍼센트)을 계산합니다 (설계도 3번).

    설계도에 명시된 두 공식:
        40분할: 별% = (15 - 0.75 * T) %
        20분할: 별% = (15 - 1.5  * T) %

    두 공식은 계수만 다를 뿐 "15 - (30 / split_count) * T" 형태로 일반화됩니다
    (40분할: 30/40=0.75, 20분할: 30/20=1.5로 정확히 일치). 분할수가 20/40 두
    가지뿐이므로 아래처럼 하나의 식으로 구현해도 설계도의 두 공식과 동일한
    결과를 냅니다(단위테스트에서 두 값 모두 전 구간 검증).

    T가 split_count/2를 넘어서면(후반전) 별%가 음수로 전환되어, 목표가가
    평단보다 낮아지는 설계도 3번의 특성이 자연스럽게 재현됩니다.
    """
    _validate_split_count(split_count)
    coefficient = Decimal(30) / Decimal(split_count)
    return Decimal(15) - coefficient * t


def star_price(avg_price: Decimal, t: Decimal, split_count: int) -> Decimal:
    """별지점(목표가)을 계산합니다: 평단가 * (1 + 별% / 100) (설계도 3번).

    센트 단위로 반올림합니다(미국 주식 최소 호가 단위 및 증권사 주문 단가 형식에 맞춤).
    """
    percent = star_percent(t, split_count)
    raw_price = avg_price * (Decimal(1) + percent / Decimal(100))
    return raw_price.quantize(CENT, rounding=ROUND_HALF_UP)


def buy_trigger_price(avg_price: Decimal, t: Decimal, split_count: int) -> Decimal:
    """매수점 = 별지점 - 0.01달러 (매도점과 가격이 겹치는 것을 방지, 설계도 3번)."""
    return star_price(avg_price, t, split_count) - CENT


def sell_trigger_price(avg_price: Decimal, t: Decimal, split_count: int) -> Decimal:
    """매도점 = 별지점 (설계도 3번). star_price()의 별칭이지만, 매수/매도 호출부에서
    "지금 이 가격이 매수점인지 매도점인지"를 코드만 보고 명확히 구분하기 위해
    별도 함수로 노출합니다."""
    return star_price(avg_price, t, split_count)


def is_first_half(t: Decimal, split_count: int) -> bool:
    """T가 split_count/2 이하이면 전반전(True), 초과하면 후반전(False) (설계도 3번)."""
    _validate_split_count(split_count)
    return t <= Decimal(split_count) / Decimal(2)


def is_reverse_trigger(t: Decimal, split_count: int) -> bool:
    """T > split_count - 1 이면 리버스모드 진입 트리거 (설계도 3번, 7-1번)."""
    _validate_split_count(split_count)
    return t > Decimal(split_count) - Decimal(1)


def single_buy_amount(remaining_cash: Decimal, t: Decimal, split_count: int) -> Decimal:
    """1회매수금을 계산합니다 (설계도 4번).

        20분할: 1회매수금 = remaining_cash / (20 - T)
        40분할: 1회매수금 = remaining_cash / (40 - T)

    T가 분할수에 근접/도달하면 분모가 0 또는 음수가 되어 1회매수금이 발산하거나
    음수가 됩니다. 설계도 4번은 "리버스모드 전환 조건과 연동해서 발생 전에
    처리되어야 정상"이라고 명시하므로, 여기서는 분모가 0 이하인 경우 조용히
    넘어가지 않고 FormulaError를 던져 호출부(normal_mode.py)가 반드시
    리버스모드 전환 여부를 먼저 확인하도록 강제합니다.
    """
    _validate_split_count(split_count)
    denominator = Decimal(split_count) - t
    if denominator <= 0:
        raise FormulaError(
            f"1회매수금 계산 불가: 분모(split_count - T)가 0 이하입니다 "
            f"(split_count={split_count}, T={t}). 리버스모드 전환 여부를 먼저 확인하세요."
        )
    return remaining_cash / denominator


def new_average_price(
    old_avg_price: Decimal,
    old_qty: int,
    buy_price: Decimal,
    buy_qty: int,
) -> Decimal:
    """매수 체결 후 새 평단가를 계산합니다: 가중평균 (기존수량*기존평단 + 신규수량*체결가) / 총수량.

    old_qty == 0 (첫 매수)인 경우 가중평균 계산 없이 체결가를 그대로 평단가로 씁니다.
    """
    if buy_qty <= 0:
        raise FormulaError(f"buy_qty는 1 이상이어야 합니다 (입력값: {buy_qty}).")
    if old_qty == 0:
        return buy_price.quantize(CENT, rounding=ROUND_HALF_UP)
    total_qty = old_qty + buy_qty
    weighted_total = old_avg_price * Decimal(old_qty) + buy_price * Decimal(buy_qty)
    return (weighted_total / Decimal(total_qty)).quantize(CENT, rounding=ROUND_HALF_UP)


def return_pct(sell_price: Decimal, avg_price_at_sell: Decimal) -> Decimal:
    """매도 건별 수익률(%) = (sell_price - avg_price_at_sell) / avg_price_at_sell * 100 (설계도 9-2번)."""
    if avg_price_at_sell == 0:
        raise FormulaError("avg_price_at_sell이 0이면 수익률을 계산할 수 없습니다.")
    return (sell_price - avg_price_at_sell) / avg_price_at_sell * Decimal(100)


def profit_amount(sell_price: Decimal, avg_price_at_sell: Decimal, sell_qty: int) -> Decimal:
    """매도 건별 실현 수익금 = (sell_price - avg_price_at_sell) * sell_qty (설계도 9-2번)."""
    return (sell_price - avg_price_at_sell) * Decimal(sell_qty)
