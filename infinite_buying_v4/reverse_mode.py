"""
reverse_mode.py
================
리버스모드(REVERSE, 일명 "소진모드")에서의 매도 "주문 의도(OrderIntent)"를 생성하는 모듈입니다
(설계도 7번).

리버스모드는 일반모드에서 T가 너무 커져(T > 분할수-1) 더 이상 정상적인 분할매수를
지속할 수 없을 때 진입하는, "보유 물량을 계획적으로 줄여나가는" 모드입니다. 매수 로직이
없고(이미 원금을 다 쓴 상태) 오직 매도 로직만 존재합니다.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from infinite_buying_v4.orders import OrderIntent
from infinite_buying_v4.trade_history import SELL_TYPE_REVERSE_LOC, SELL_TYPE_REVERSE_MOC

CENT = Decimal("0.01")

# TQQQ 리버스모드 종료(일반모드 복귀) 조건: 종가가 평단가 대비 -15% 선 위로 올라오면
# (설계도 7-3번: 종가 >= 평단가 * (1 - 0.15)).
EXIT_THRESHOLD_PCT = Decimal("0.15")


class ReverseModeError(Exception):
    """리버스모드 계산에 필요한 입력이 잘못됐을 때(종가 5거래일 미달, 보유수량 0 등) 발생시키는 예외."""


def reverse_divisor(split_count: int) -> int:
    """리버스모드 매도수량을 등분할 때 쓰는 등분수 (설계도 7-2번: 20분할 -> 10등분, 40분할 -> 20등분).

    두 경우 모두 "split_count / 2"와 정확히 일치합니다.
    """
    if split_count not in (20, 40):
        raise ReverseModeError(f"split_count는 20 또는 40이어야 합니다 (입력값: {split_count}).")
    return split_count // 2


def reverse_sell_qty(holding_qty: int, split_count: int) -> int:
    """오늘 매도할 수량을 계산합니다 (설계도 7-2번: 직전 보유수량을 등분수만큼 나눠서 내림).

    보유수량이 등분수보다 작아 계산 결과가 0이 되면, 설계도 11번 방어 코드에 따라
    최소 1주로 보정합니다("리버스모드 매도수량이 0으로 계산되는 경우... 최소 1주 매도로 보정").
    """
    if holding_qty <= 0:
        raise ReverseModeError(f"보유수량이 0 이하이면 리버스모드 매도수량을 계산할 수 없습니다 (입력값: {holding_qty}).")
    divisor = reverse_divisor(split_count)
    qty = holding_qty // divisor
    return qty if qty > 0 else 1  # 설계도 11번: 최소 1주 보정


def reverse_star_price(recent_5_closes: list[Decimal]) -> Decimal:
    """리버스모드의 별지점 = 직전 5거래일 종가 평균 (설계도 7-2번)."""
    if len(recent_5_closes) != 5:
        raise ReverseModeError(
            f"직전 5거래일 종가가 정확히 5개 필요합니다 (입력 개수: {len(recent_5_closes)})."
        )
    average = sum(recent_5_closes, Decimal(0)) / Decimal(5)
    return average.quantize(CENT, rounding=ROUND_HALF_UP)


def generate_day1_moc_sell_order(holding_qty: int, split_count: int) -> OrderIntent:
    """리버스모드 첫날(D1) 매도 주문을 생성합니다 (설계도 7-2번: MOC 무조건 매도).

    가격 지정이 없는 시장가 종가주문(MOC)이므로 OrderIntent.price는 None입니다.
    """
    qty = reverse_sell_qty(holding_qty, split_count)
    return OrderIntent(side="SELL", order_kind="MOC", price=None, qty=qty, purpose=SELL_TYPE_REVERSE_MOC)


def generate_daily_loc_sell_order(
    holding_qty: int, split_count: int, recent_5_closes: list[Decimal]
) -> OrderIntent:
    """리버스모드 D2 이후 매도 주문을 생성합니다 (설계도 7-2번: 직전 5거래일 종가 평균 위쪽 LOC 매도).

    매일 남은 보유수량 기준으로 등분을 다시 계산하므로, 자연히 매도수량이 점점
    줄어드는 구조가 됩니다(설계도 7-2번 "자연히 매도수량이 점점 줄어드는 구조").
    """
    qty = reverse_sell_qty(holding_qty, split_count)
    price = reverse_star_price(recent_5_closes)
    return OrderIntent(side="SELL", order_kind="LOC", price=price, qty=qty, purpose=SELL_TYPE_REVERSE_LOC)


def is_reverse_exit_condition(close_price: Decimal, avg_price: Decimal) -> bool:
    """리버스모드 종료(일반모드 복귀) 조건을 판정합니다 (설계도 7-3번).

    종가 >= 평단가 * (1 - 0.15) 이면 True. 이 함수가 True를 반환한 "다음 거래일부터"
    일반모드로 복귀해야 하며(당일 즉시 전환이 아님), 그 시점 전환 타이밍은 scheduler.py가
    담당합니다.
    """
    threshold = avg_price * (Decimal(1) - EXIT_THRESHOLD_PCT)
    return close_price >= threshold
