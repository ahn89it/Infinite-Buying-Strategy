"""
normal_mode.py
===============
일반모드(NORMAL)에서의 매수/매도 "주문 의도(OrderIntent)"를 생성하는 모듈입니다
(설계도 5번 매수 로직, 6번 매도 로직).

이 모듈은 실제로 증권사에 주문을 넣지 않습니다. "지금 상태라면 이런 주문들을 내야 한다"는
계산 결과(OrderIntent 리스트)만 만들어서 반환하고, 실제 제출은 kiwoom_adapter.py가,
언제 실행할지는 scheduler.py가 결정합니다. 이렇게 분리해두면 이 모듈의 로직은 증권사 API
없이도(네트워크 없이도) 단위 테스트로 100% 검증할 수 있습니다.

설계도가 정확한 규칙을 준 부분과, 실무 판단에 맡긴 부분을 구분해서 구현했습니다:
- 정확한 규칙(그대로 구현): 첫매수 큰수 미끼주문, 전반전 별지점/평단 절반매수, 후반전
  별지점 전액매수, 쿼터매도, 지정가매도(+15%).
- 실무 판단에 맡긴 부분("단계별", "보조" 주문의 정확한 가격/수량 간격을 설계도가
  명시하지 않음): generate_first_buy_orders()의 실체결 노림 사다리 주문은 1회매수금
  범위 안에서 균등 분할하도록 구현했고, generate_aux_ladder_buy_orders()는 기본값으로
  호출되지 않는 "옵션" 함수로 분리해 두었습니다 — 추가 하락 대응 주문에 얼마의 예비
  현금을 쓸지는 개인 리스크 성향에 따라 달라지는 정책적 판단이라, 이 함수를 실제로
  스케줄러에서 사용할지/얼마를 배정할지는 사용자가 config로 명시적으로 켜야 합니다.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

from infinite_buying_v4.formulas import (
    buy_trigger_price,
    is_first_half,
    is_reverse_trigger,
    sell_trigger_price,
    single_buy_amount,
)
from infinite_buying_v4.orders import OrderIntent
from infinite_buying_v4.trade_history import (
    BUY_TYPE_FIRST,
    BUY_TYPE_FULL_STAR,
    BUY_TYPE_HALF_AVG,
    BUY_TYPE_HALF_STAR,
    SELL_TYPE_LIMIT_15PCT,
    SELL_TYPE_QUARTER,
)

# 첫매수 "큰수" 미끼주문 가격 프리미엄. 설계도 5-1번은 "전일 종가 대비 10~15% 위"라고만
# 명시했으므로, 그 범위의 중간값인 12%로 고정합니다. 실제 체결을 노리는 가격이 아니라
# 증권사 ±20% 주문가 제한을 우회하기 위한 장치이므로 정밀할 필요가 없습니다.
DECOY_PREMIUM_PCT = Decimal("0.12")
DECOY_QTY = 1  # 체결 목적이 아니므로 최소 수량(1주)만 사용합니다.

# 첫매수 시, 미끼주문 아래로 실제 체결을 노리는 단계별 LOC 매수 주문의 기본 설정.
# 설계도 5-1번은 "그 아래로 실제 체결을 노리는 단계별 LOC 매수 주문 추가"라고만 명시하고
# 정확한 단수/간격은 정하지 않았으므로, 기본값을 상수로 두고 필요 시 호출부에서 조정합니다.
DEFAULT_FIRST_BUY_LADDER_STEPS = 4
DEFAULT_FIRST_BUY_LADDER_STEP_PCT = Decimal("0.03")  # 단계마다 3%씩 아래로


class NormalModeError(Exception):
    """일반모드 주문 생성 규칙을 위반했을 때(예: 이미 리버스모드 전환 조건인데 일반매수를 시도) 발생시키는 예외."""


def _qty_from_amount(amount: Decimal, price: Decimal) -> int:
    """금액과 단가로 매수 가능 수량을 구합니다(소수점 이하 버림 = 매수 시 초과 지출 방지)."""
    if price <= 0:
        raise NormalModeError(f"가격은 0보다 커야 합니다 (입력값: {price}).")
    return int((amount / price).to_integral_value(rounding=ROUND_FLOOR))


def place_decoy_order(prev_close: Decimal) -> OrderIntent:
    """첫매수 "큰수" 미끼 LOC 매수 주문을 생성합니다 (설계도 5-1번).

    실제 체결을 노리지 않는 우회용 주문이므로, 이 주문이 실제로 체결되면 설계도 11번
    "체결 내역과 로컬 상태 불일치 감지"에 해당하는 이상 상황입니다(가격이 12%나 위인데
    체결됐다는 것은 시세가 폭등했다는 뜻). scheduler.py는 이 주문의 체결 여부를 별도로
    감시해야 합니다.
    """
    decoy_price = (prev_close * (Decimal(1) + DECOY_PREMIUM_PCT)).quantize(Decimal("0.01"))
    return OrderIntent(
        side="BUY",
        order_kind="LOC",
        price=decoy_price,
        qty=DECOY_QTY,
        purpose=BUY_TYPE_FIRST,
        is_decoy=True,
    )


def generate_first_buy_orders(
    prev_close: Decimal,
    remaining_cash: Decimal,
    split_count: int,
    *,
    ladder_steps: int = DEFAULT_FIRST_BUY_LADDER_STEPS,
    ladder_step_pct: Decimal = DEFAULT_FIRST_BUY_LADDER_STEP_PCT,
) -> list[OrderIntent]:
    """첫매수(T=0, 보유량 0) 주문들을 생성합니다 (설계도 5-1번).

    1) place_decoy_order()로 만든 미끼 주문 1건
    2) 미끼 아래로, 전일 종가에서 ladder_step_pct씩 내려가며 ladder_steps개의 실체결
       노림 LOC 매수 주문. 이번 회차(T=0) 전체 매수 예산인 single_buy_amount()를
       단계 수만큼 균등 분할해서 각 가격에 배정합니다 — 아직 평단가가 없는 첫매수라서
       전반전/후반전처럼 "별지점/평단" 기준을 쓸 수 없기 때문에, 여러 가격대에 나눠
       걸어 체결 확률을 높이는 방식입니다.
    """
    orders: list[OrderIntent] = [place_decoy_order(prev_close)]

    total_budget = single_buy_amount(remaining_cash, Decimal(0), split_count)
    per_step_budget = total_budget / Decimal(ladder_steps)

    for step in range(1, ladder_steps + 1):
        step_price = (prev_close * (Decimal(1) - ladder_step_pct * Decimal(step))).quantize(Decimal("0.01"))
        qty = _qty_from_amount(per_step_budget, step_price)
        if qty <= 0:
            continue  # 배정 예산으로 1주도 못 사는 가격대는 건너뜁니다.
        orders.append(
            OrderIntent(side="BUY", order_kind="LOC", price=step_price, qty=qty, purpose=BUY_TYPE_FIRST)
        )
    return orders


def generate_first_half_buy_orders(
    avg_price: Decimal, remaining_cash: Decimal, t: Decimal, split_count: int
) -> list[OrderIntent]:
    """전반전 매수 주문을 생성합니다 (설계도 5-2번: T = 1 ~ 분할수/2 미만).

    1회매수액의 절반은 별지점(평단보다 위, 상승 추세 편승용)에, 나머지 절반은
    평단가(추가 하락 시 물타기용)에 LOC로 나눠 겁니다.
    """
    if not is_first_half(t, split_count):
        raise NormalModeError(f"T={t}는 전반전 구간이 아닙니다 (split_count={split_count}).")

    total_budget = single_buy_amount(remaining_cash, t, split_count)
    half_budget = total_budget / Decimal(2)

    star_price = buy_trigger_price(avg_price, t, split_count)
    orders: list[OrderIntent] = []

    star_qty = _qty_from_amount(half_budget, star_price)
    if star_qty > 0:
        orders.append(
            OrderIntent(side="BUY", order_kind="LOC", price=star_price, qty=star_qty, purpose=BUY_TYPE_HALF_STAR)
        )

    avg_qty = _qty_from_amount(half_budget, avg_price)
    if avg_qty > 0:
        orders.append(
            OrderIntent(side="BUY", order_kind="LOC", price=avg_price, qty=avg_qty, purpose=BUY_TYPE_HALF_AVG)
        )

    return orders


def generate_second_half_buy_orders(
    avg_price: Decimal, remaining_cash: Decimal, t: Decimal, split_count: int
) -> list[OrderIntent]:
    """후반전 매수 주문을 생성합니다 (설계도 5-3번: T = 분할수/2 ~ 분할수-1).

    1회매수액 전액을 별지점(후반전에는 평단보다 낮은 가격)에 LOC로 겁니다.
    T가 이미 리버스모드 전환 조건(> 분할수-1)에 도달했다면, 이 함수는 절대 호출되면 안
    되므로 NormalModeError를 던져 호출부(scheduler.py)가 reverse_mode.py로 전환하도록
    강제합니다 (설계도 5-3번 "리버스모드 진입, 이후 매수 로직은 리버스모드 섹션으로 전환").
    """
    if is_first_half(t, split_count):
        raise NormalModeError(f"T={t}는 후반전 구간이 아닙니다 (split_count={split_count}).")
    if is_reverse_trigger(t, split_count):
        raise NormalModeError(
            f"T={t}는 이미 리버스모드 진입 조건입니다 (split_count={split_count}). "
            f"reverse_mode.py를 사용해야 합니다."
        )

    total_budget = single_buy_amount(remaining_cash, t, split_count)
    star_price = buy_trigger_price(avg_price, t, split_count)

    qty = _qty_from_amount(total_budget, star_price)
    if qty <= 0:
        return []
    return [OrderIntent(side="BUY", order_kind="LOC", price=star_price, qty=qty, purpose=BUY_TYPE_FULL_STAR)]


def generate_aux_ladder_buy_orders(
    anchor_price: Decimal,
    cash_budget: Decimal,
    *,
    ladder_steps: int,
    ladder_step_pct: Decimal,
) -> list[OrderIntent]:
    """추가 하락 대응용 "보조" LOC 매수 사다리 주문을 생성합니다 (설계도 5-2, 5-3번).

    기본적으로 scheduler.py는 이 함수를 호출하지 않습니다. 설계도가 "단계별 보조 주문"의
    존재만 언급하고 정확한 가격 간격/예산 배정 규칙을 명시하지 않았기 때문에, 임의로
    실제 현금을 배정하는 로직을 기본 동작에 넣지 않았습니다. 사용하려면 호출부에서
    anchor_price(보통 avg_price 또는 오늘 별지점)와 cash_budget(이 사다리 전체에 쓸
    예비 현금, 사용자가 직접 정책 결정)을 명시적으로 넘겨야 합니다.
    """
    if ladder_steps <= 0:
        raise NormalModeError(f"ladder_steps는 1 이상이어야 합니다 (입력값: {ladder_steps}).")
    per_step_budget = cash_budget / Decimal(ladder_steps)
    orders: list[OrderIntent] = []
    for step in range(1, ladder_steps + 1):
        step_price = (anchor_price * (Decimal(1) - ladder_step_pct * Decimal(step))).quantize(Decimal("0.01"))
        qty = _qty_from_amount(per_step_budget, step_price)
        if qty <= 0:
            continue
        orders.append(
            OrderIntent(side="BUY", order_kind="LOC", price=step_price, qty=qty, purpose=BUY_TYPE_HALF_STAR)
        )
    return orders


def generate_sell_orders(avg_price: Decimal, holding_qty: int, t: Decimal, split_count: int) -> list[OrderIntent]:
    """매도 주문을 생성합니다 (설계도 6번: 전반전/후반전 공통).

    - 보유수량의 1/4 -> 별지점 LOC 매도 (쿼터매도)
    - 나머지 3/4 -> 지정가 매도, 평단 +15%

    보유수량이 4주 미만이면 1/4이 정수 0이 되어 쿼터매도 주문을 만들 수 없습니다. 이
    경우 쿼터매도는 생략하고 지정가매도만 생성합니다(설계도 11번의 "최소 1주 보정"은
    리버스모드 매도수량에 한정된 규칙이라 여기서는 임의로 확대 적용하지 않습니다).
    """
    if holding_qty <= 0:
        raise NormalModeError(f"보유수량이 0 이하이면 매도 주문을 생성할 수 없습니다 (입력값: {holding_qty}).")

    orders: list[OrderIntent] = []
    quarter_qty = holding_qty // 4
    remaining_qty = holding_qty - quarter_qty

    if quarter_qty > 0:
        star_price = sell_trigger_price(avg_price, t, split_count)
        orders.append(
            OrderIntent(side="SELL", order_kind="LOC", price=star_price, qty=quarter_qty, purpose=SELL_TYPE_QUARTER)
        )

    if remaining_qty > 0:
        limit_price = (avg_price * Decimal("1.15")).quantize(Decimal("0.01"))
        orders.append(
            OrderIntent(
                side="SELL", order_kind="LIMIT", price=limit_price, qty=remaining_qty, purpose=SELL_TYPE_LIMIT_15PCT
            )
        )

    return orders
