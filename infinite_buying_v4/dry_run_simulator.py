"""
dry_run_simulator.py
=====================
DRY_RUN 모드에서 "실제 시세였다면 오늘 주문이 체결됐을지"를 시뮬레이션하고,
그 결과로 모의 상태(shadow state)를 실제처럼 하루하루 진행시키는 모듈입니다.

왜 필요한가?
- DRY_RUN은 실제 주문을 절대 제출하지 않습니다(kiwoom_adapter.submit_order*를 호출하지
  않음). 그래서 real `state`/`buy_records`/`sell_records`는 DRY_RUN 중에 전혀 갱신되지
  않고, T=0(첫매수 대기) 상태에서 영원히 멈춰 있습니다 — "이 전략이 정말 동작하는지"를
  실투자 전환 전에 충분히 검증하기에는 부족합니다.
- 이 모듈은 실제 계좌와 완전히 분리된 "모의 계좌"(`dry_run_state` 테이블, 스키마는
  `state`와 동일)를 두고, 실제 시세(일봉 시가/고가/저가/종가)를 기준으로 그날 주문이
  체결됐을지를 판정해서 이 모의 계좌를 진짜처럼 진행시킵니다. 실제 하루가 지나야 모의
  하루도 진행됩니다(빨리 감기 백테스트가 아니라, 실제 시간 흐름에 맞춘 순차 검증입니다).
- normal_mode.py/reverse_mode.py/formulas.py/event_log.py는 전부 이미 순수함수라
  실제 DB나 State 객체에 결합되어 있지 않으므로, 이 모듈은 그 함수들을 실제 매매
  경로와 **그대로 동일하게** 재사용합니다 — 시뮬레이션 전용 계산 로직을 새로 만들지
  않습니다. state.py/trade_history.py도 `table` 매개변수만 `dry_run_*`로 바꿔 그대로
  재사용합니다.

한계(정직하게 밝힘):
- 일봉 데이터는 정규장(본장) 기준입니다. 프리마켓/애프터마켓 중 형성된 가격은 포함하지
  않으므로, 프리~애프터 전체 유지되는 지정가매도의 체결 판정은 "정규장 중에만 스쳤는지"로
  근사한 것이며 실제와 다를 수 있습니다.
- 장중 체결 우선순위(같은 가격에 먼저 걸린 다른 투자자의 주문 등)는 반영하지 않습니다 —
  "그 가격이 그날 시세 범위 안에 있었는지"만으로 단순 판정합니다.
- 이 시뮬레이션은 실제 체결 이벤트 소싱(event_log.py)의 JSONL 파일을 공유하지 않고,
  T값 갱신 규칙(event_log.apply_event)만 재사용해 dry_run_state.t_value에 직접
  반영합니다 — 실제 이벤트 로그와 모의 이벤트를 같은 파일에 뒤섞지 않기 위함입니다.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from infinite_buying_v4 import event_log, normal_mode, reverse_mode, trade_history
from infinite_buying_v4.config import Config
from infinite_buying_v4.formulas import is_reverse_trigger, new_average_price
from infinite_buying_v4.kiwoom_adapter import DailyOHLC, KiwoomAdapterError, get_recent_daily_ohlc
from infinite_buying_v4.orders import OrderIntent
from infinite_buying_v4.state import (
    MODE_NORMAL,
    MODE_REVERSE,
    PHASE_FIRST,
    PHASE_FIRST_HALF,
    PHASE_SECOND_HALF,
    State,
    bootstrap_new_state,
    load_state,
    save_state,
    start_new_cycle,
    state_exists,
)
from infinite_buying_v4.trade_history import (
    BUY_TYPE_FIRST,
    BUY_TYPE_FULL_STAR,
    BUY_TYPE_HALF_AVG,
    BUY_TYPE_HALF_STAR,
    SELL_TYPE_LIMIT_15PCT,
    SELL_TYPE_QUARTER,
)

logger = logging.getLogger("infinite_buying_v4.dry_run_simulator")

# 실제 state.py/trade_history.py 함수들이 향하는 테이블을 전부 dry_run_* 로 바꿉니다.
_STATE_TABLE = "dry_run_state"
_BUY_TABLE = "dry_run_buy_records"
_SELL_TABLE = "dry_run_sell_records"
_CYCLE_TABLE = "dry_run_cycle_summary"
_PORTFOLIO_TABLE = "dry_run_portfolio_summary"

_BUY_ROUND_PURPOSES = (BUY_TYPE_FIRST, BUY_TYPE_HALF_STAR, BUY_TYPE_HALF_AVG, BUY_TYPE_FULL_STAR)
_FULL_FILL_RATIO_THRESHOLD = Decimal("0.99")


class DryRunSimulatorError(Exception):
    """시뮬레이션 진행 중 데이터 정합성 문제가 발견됐을 때 발생시키는 예외."""


@dataclass(frozen=True)
class SimulatedFill:
    """주문 하나가 그날 시세 기준으로 체결됐다고 판정된 결과."""

    order: "SimulatedOrder"
    fill_price: Decimal
    fill_qty: int


@dataclass(frozen=True)
class SimulatedOrder:
    """dry_run_orders 한 행을 파이썬 값으로 옮긴 것 (order_no가 없다는 점만 OrderIntent와 다름)."""

    id: int
    submitted_date: date
    side: str
    order_kind: str
    price: Decimal | None
    qty: int
    purpose: str
    is_decoy: bool


# ---------------------------------------------------------------------------
# 순수함수: 주문 하나가 그날 OHLC로 체결됐을지 판정
# ---------------------------------------------------------------------------


def simulate_order_fill(order: SimulatedOrder, ohlc: DailyOHLC) -> SimulatedFill | None:
    """주문이 그날 실제 시세(OHLC)였다면 체결됐을지 판정합니다. 체결 안 되면 None.

    - MOC(시장가 종가): 무조건 종가에 전량 체결.
    - LOC(지정가 종가): 종가가 지정가를 만족하면(매수: 종가<=지정가, 매도: 종가>=지정가)
      종가에 전량 체결. 그렇지 않으면 미체결.
    - LIMIT(지정가, 정규장 내내 유지): 그날 저가/고가가 지정가를 스쳤으면(매수: 저가<=
      지정가, 매도: 고가>=지정가) 지정가에 전량 체결로 근사. 스치지 않았으면 미체결.
    """
    if order.order_kind == "MOC":
        return SimulatedFill(order=order, fill_price=ohlc.close, fill_qty=order.qty)

    if order.price is None:
        raise DryRunSimulatorError(f"MOC가 아닌 주문에 가격이 없습니다: {order}")

    if order.order_kind == "LOC":
        satisfied = ohlc.close <= order.price if order.side == "BUY" else ohlc.close >= order.price
        return SimulatedFill(order=order, fill_price=ohlc.close, fill_qty=order.qty) if satisfied else None

    if order.order_kind == "LIMIT":
        touched = ohlc.low <= order.price if order.side == "BUY" else ohlc.high >= order.price
        return SimulatedFill(order=order, fill_price=order.price, fill_qty=order.qty) if touched else None

    raise DryRunSimulatorError(f"알 수 없는 order_kind입니다: {order.order_kind!r}")


# ---------------------------------------------------------------------------
# dry_run_orders 장부 (submitted_orders의 모의 버전)
# ---------------------------------------------------------------------------


def record_dry_run_order(conn: sqlite3.Connection, order: OrderIntent, submitted_date: date) -> None:
    """오늘 "이런 주문을 냈을 것"이라고 계산된 OrderIntent를 dry_run_orders에 적어둡니다.

    다음 프리장 시점에 그날의 실제 OHLC로 체결 여부를 판정하기 전까지 여기 대기합니다.
    """
    conn.execute(
        """
        INSERT INTO dry_run_orders (submitted_date, side, order_kind, price, qty, purpose, is_decoy)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            submitted_date.isoformat(),
            order.side,
            order.order_kind,
            str(order.price) if order.price is not None else None,
            order.qty,
            order.purpose,
            int(order.is_decoy),
        ),
    )


def _load_orders_for_date(conn: sqlite3.Connection, target_date: date) -> list[SimulatedOrder]:
    rows = conn.execute(
        "SELECT * FROM dry_run_orders WHERE submitted_date = ?", (target_date.isoformat(),)
    ).fetchall()
    return [
        SimulatedOrder(
            id=row["id"],
            submitted_date=date.fromisoformat(row["submitted_date"]),
            side=row["side"],
            order_kind=row["order_kind"],
            price=Decimal(row["price"]) if row["price"] is not None else None,
            qty=row["qty"],
            purpose=row["purpose"],
            is_decoy=bool(row["is_decoy"]),
        )
        for row in rows
    ]


def _delete_orders_for_date(conn: sqlite3.Connection, target_date: date) -> None:
    conn.execute("DELETE FROM dry_run_orders WHERE submitted_date = ?", (target_date.isoformat(),))


# ---------------------------------------------------------------------------
# 모의 상태(shadow state) 부트스트랩
# ---------------------------------------------------------------------------


def ensure_dry_run_state(conn: sqlite3.Connection, config: Config, *, start_date: date) -> State:
    """dry_run_state가 없으면 실제 계좌와 같은 원금/분할수로 새로 만듭니다.

    실제 `state`와 달리, 이 함수는 스케줄러가 필요할 때 **자동으로** 호출합니다 — 모의
    계좌라 실수로 덮어써도 실제 손실이 없고, DRY_RUN 검증을 시작하는 데 사람이 매번
    별도 스크립트를 실행하게 만들 이유가 없기 때문입니다(실제 `bootstrap.py`가 요구하는
    "사람이 명시적으로 1회 실행" 원칙은 real state에만 적용됩니다).
    """
    if state_exists(conn, table=_STATE_TABLE):
        return load_state(conn, table=_STATE_TABLE)

    state = bootstrap_new_state(
        conn, split_count=config.split_count, principal=config.principal, start_date=start_date, table=_STATE_TABLE
    )
    trade_history.open_cycle_summary(conn, cycle_id=state.cycle_id, start_date=start_date, table=_CYCLE_TABLE)
    trade_history.ensure_portfolio_summary(
        conn, strategy_start_date=start_date, initial_principal=config.principal, table=_PORTFOLIO_TABLE
    )
    logger.info(
        "[DRY-RUN 시뮬레이션] 모의 계좌 신규 생성: split_count=%d, principal=%s, start_date=%s",
        state.split_count,
        state.principal,
        start_date.isoformat(),
    )
    return state


# ---------------------------------------------------------------------------
# 체결 시뮬레이션 -> 모의 상태 갱신 (실제 scheduler._apply_fills와 동일한 구조)
# ---------------------------------------------------------------------------


def _evaluate_buy_round_ratio(orders: list[SimulatedOrder], fills: list[SimulatedFill]) -> Decimal:
    """그날 매수 라운드 전체 의도 **수량** 대비 실제 체결 **수량** 비율. (실제
    scheduler.py의 부분체결 판정과 동일한 원칙: 형제 주문의 미체결분도 분모에 포함).

    금액(price*qty)이 아니라 수량으로 비교하는 이유: LOC 주문은 지정가가 아니라
    그날 종가에 체결되므로, 지정가와 종가가 달라 "의도 금액"과 "체결 금액"이 항상
    조금씩 어긋납니다 — 그러면 수량 전체가 체결됐어도 fill_ratio가 100%보다 살짝
    낮게 나와 FULL_BUY이어야 할 체결이 HALF_BUY로 잘못 분류됩니다(이 시뮬레이터를
    만들다가 실제 숫자로 재현되어 발견, scheduler.py의 동일 로직도 함께 수정함).
    수량은 가격과 무관하게 정확히 비교되므로 이 문제가 없습니다.
    """
    buy_orders = [o for o in orders if o.side == "BUY" and o.purpose in _BUY_ROUND_PURPOSES and not o.is_decoy]
    if not buy_orders:
        return Decimal(0)
    intended_qty = sum(o.qty for o in buy_orders)
    filled_qty = sum(
        f.fill_qty
        for f in fills
        if f.order.side == "BUY" and f.order.purpose in _BUY_ROUND_PURPOSES and not f.order.is_decoy
    )
    return (Decimal(filled_qty) / Decimal(intended_qty)) if intended_qty > 0 else Decimal(0)


def run_dry_run_premarket(conn: sqlite3.Connection, config: Config, today: date) -> None:
    """전일 dry_run_orders를 그날의 실제 OHLC와 대조해 시뮬레이션 체결을 확정하고,
    모의 상태(dry_run_state)를 갱신합니다. run_premarket_update()가 DRY_RUN일 때 호출합니다.
    """
    state = ensure_dry_run_state(conn, config, start_date=today)

    try:
        ohlc_bars = get_recent_daily_ohlc(config, count=5)
    except KiwoomAdapterError as exc:
        logger.warning("[DRY-RUN 시뮬레이션] OHLC 조회 실패로 이번 프리장 시뮬레이션을 건너뜁니다: %s", exc)
        return

    # 시뮬레이션 대상 날짜: dry_run_orders에 남아있는(=아직 판정 안 된) 모든 날짜.
    # 노트북이 며칠 꺼져있었어도 밀린 날짜를 순서대로 전부 처리합니다.
    pending_dates = sorted(
        {date.fromisoformat(r["submitted_date"]) for r in conn.execute("SELECT DISTINCT submitted_date FROM dry_run_orders")}
    )
    ohlc_by_date = {bar.trade_date: bar for bar in ohlc_bars}

    for target_date in pending_dates:
        ohlc = ohlc_by_date.get(target_date)
        if ohlc is None:
            continue  # 아직 그날 종가가 API에 안 잡힘(휴장일 등) - 다음 프리장 때 다시 시도
        state = _settle_day(conn, config, state, target_date, ohlc)

    save_state(conn, state, table=_STATE_TABLE)
    _refresh_dry_run_portfolio_summary(conn, config, state, latest_close=ohlc_bars[-1].close)


def _refresh_dry_run_portfolio_summary(conn: sqlite3.Connection, config: Config, state: State, *, latest_close: Decimal) -> None:
    """모의 계좌의 총평가금액/총수익률 등을 재계산합니다 (real state의 portfolio_summary에
    대응, 1-12절/대시보드 "DRY-RUN 모의 계좌" 요약 카드가 이 값을 읽습니다).

    real 경로(scheduler.run_premarket_update)는 get_quote()로 근실시간 현재가를 쓰지만,
    모의 계좌는 실시간 시세가 없으므로 이번 프리장 시점에 조회한 **가장 최근 일봉 종가**를
    현재가로 씁니다 — 최대 하루 전 종가 기준 스냅샷이라는 뜻이며, 대시보드 쪽 안내 문구에도
    이 사실을 표기합니다.

    `ensure_portfolio_summary()`는 `ensure_dry_run_state()`가 모의 계좌를 처음 만들 때
    이미 호출해두지만, 이 기능(2026-09-16 추가) 이전부터 DRY_RUN을 돌려온 DB는
    `dry_run_state`는 있어도 `dry_run_portfolio_summary` 행이 없을 수 있습니다 — 그런
    경우를 대비해 없으면 여기서 한 번 더 만들어줍니다(멱등이라 이미 있으면 아무 일도 안 함).
    """
    trade_history.ensure_portfolio_summary(
        conn, strategy_start_date=state.cycle_start_date, initial_principal=config.principal, table=_PORTFOLIO_TABLE
    )
    trade_history.update_portfolio_summary(
        conn,
        current_price=latest_close,
        remaining_cash=state.remaining_cash,
        avg_price=state.avg_price,
        holding_qty=state.holding_qty,
        table=_PORTFOLIO_TABLE,
        cycle_table=_CYCLE_TABLE,
    )


def _settle_day(conn: sqlite3.Connection, config: Config, state: State, target_date: date, ohlc: DailyOHLC) -> State:
    """하루치 dry_run_orders를 정산합니다: 체결 판정 -> 모의 buy/sell 기록 -> T/평단/
    보유수량/잔금 갱신 -> 사이클 종료/리버스모드 전이 -> dry_run_orders 정리."""
    orders = _load_orders_for_date(conn, target_date)
    if not orders:
        return state

    fills = [f for o in orders if (f := simulate_order_fill(o, ohlc)) is not None]

    for fill in fills:
        if fill.order.is_decoy:
            logger.warning(
                "[DRY-RUN 시뮬레이션] 미끼 주문이 시뮬레이션상 체결됐을 것으로 판정됨(day=%s, price=%s) — "
                "실제였다면 이상 상황이니 눈여겨보세요.",
                target_date.isoformat(),
                fill.fill_price,
            )
        if fill.order.side == "BUY":
            state = replace(
                state,
                avg_price=new_average_price(state.avg_price, state.holding_qty, fill.fill_price, fill.fill_qty),
                holding_qty=state.holding_qty + fill.fill_qty,
                remaining_cash=state.remaining_cash - fill.fill_price * Decimal(fill.fill_qty),
            )
            trade_history.record_buy(
                conn,
                cycle_id=state.cycle_id,
                buy_date=target_date,
                buy_price=fill.fill_price,
                buy_qty=fill.fill_qty,
                order_type=fill.order.purpose,
                t_after=state.t,
                avg_price_after=state.avg_price,
                table=_BUY_TABLE,
            )
        else:
            state = replace(
                state,
                holding_qty=state.holding_qty - fill.fill_qty,
                remaining_cash=state.remaining_cash + fill.fill_price * Decimal(fill.fill_qty),
            )
            trade_history.record_sell(
                conn,
                cycle_id=state.cycle_id,
                sell_date=target_date,
                sell_price=fill.fill_price,
                sell_qty=fill.fill_qty,
                order_type=fill.order.purpose,
                avg_price_at_sell=state.avg_price,
                t_after=state.t,
                table=_SELL_TABLE,
            )

    # T값 갱신: 부분체결 비율 기준으로 매수 이벤트를, 매도 종류로 매도 이벤트를 판정합니다
    # (실제 scheduler._classify_daily_events와 동일한 원칙).
    quarter_sell_filled = any(f.order.purpose == SELL_TYPE_QUARTER for f in fills)
    limit_sell_filled = any(f.order.purpose == SELL_TYPE_LIMIT_15PCT for f in fills)
    buy_fill_ratio = _evaluate_buy_round_ratio(orders, fills)
    has_buy_fill = any(f.order.side == "BUY" and not f.order.is_decoy for f in fills)

    t = state.t
    if quarter_sell_filled:
        t = event_log.apply_event(t, event_log.EVENT_QUARTER_SELL)
    if has_buy_fill:
        is_full = buy_fill_ratio >= _FULL_FILL_RATIO_THRESHOLD
        if limit_sell_filled:
            t = event_log.apply_event(
                t,
                event_log.EVENT_LIMIT_SELL_THEN_LOC_BUY_FULL if is_full else event_log.EVENT_LIMIT_SELL_THEN_LOC_BUY_HALF,
            )
        else:
            t = event_log.apply_event(t, event_log.EVENT_FULL_BUY if is_full else event_log.EVENT_HALF_BUY)
    state = replace(state, t=t)

    if state.holding_qty == 0 and orders:
        state = replace(state, avg_price=Decimal(0))
        trade_history.close_cycle_summary(
            conn, cycle_id=state.cycle_id, end_date=target_date,
            buy_table=_BUY_TABLE, sell_table=_SELL_TABLE, cycle_table=_CYCLE_TABLE,
        )
        logger.info("[DRY-RUN 시뮬레이션] 모의 사이클 %d 종료 (종료일=%s)", state.cycle_id, target_date.isoformat())
        state = start_new_cycle(
            conn, state, fixed_principal=config.principal, compound_on_restart=config.compound_on_restart,
            start_date=target_date, table=_STATE_TABLE,
        )
        trade_history.open_cycle_summary(conn, cycle_id=state.cycle_id, start_date=target_date, table=_CYCLE_TABLE)
    elif state.mode == MODE_NORMAL and is_reverse_trigger(state.t, state.split_count):
        state = replace(state, mode=MODE_REVERSE, phase=None, reverse_day_count=0, reverse_prev_qty=state.holding_qty)
        trade_history.mark_cycle_hit_reverse_mode(conn, cycle_id=state.cycle_id, table=_CYCLE_TABLE)
        logger.info("[DRY-RUN 시뮬레이션] 모의 계좌 리버스모드 진입 (T=%s)", state.t)
    elif state.mode == MODE_REVERSE:
        if reverse_mode.is_reverse_exit_condition(ohlc.close, state.avg_price):
            state = replace(state, mode=MODE_NORMAL, phase=PHASE_SECOND_HALF)
            logger.info("[DRY-RUN 시뮬레이션] 모의 계좌 일반모드 복귀")
        else:
            state = replace(state, reverse_day_count=state.reverse_day_count + 1, reverse_prev_qty=state.holding_qty)
    elif state.mode == MODE_NORMAL and state.holding_qty > 0 and state.phase != PHASE_FIRST:
        from infinite_buying_v4.formulas import is_first_half

        new_phase = PHASE_FIRST_HALF if is_first_half(state.t, state.split_count) else PHASE_SECOND_HALF
        if new_phase != state.phase:
            state = replace(state, phase=new_phase)

    _delete_orders_for_date(conn, target_date)
    return state


# ---------------------------------------------------------------------------
# 본장 시점: 모의 주문 생성 (실제 scheduler._generate_normal_mode_orders와 동일한 구조)
# ---------------------------------------------------------------------------


def generate_dry_run_orders(config: Config, state: State, prev_close: Decimal, recent_closes: list[Decimal]) -> list[OrderIntent]:
    """모의 상태 기준으로 오늘의 LOC/MOC 주문을 생성합니다. 실제 normal_mode.py/
    reverse_mode.py 함수를 그대로 호출합니다(전략 계산 로직 중복 없음)."""
    if state.mode == MODE_NORMAL:
        if state.holding_qty == 0:
            return normal_mode.generate_first_buy_orders(prev_close, state.remaining_cash, state.split_count)
        if state.phase == PHASE_FIRST_HALF:
            return normal_mode.generate_first_half_buy_orders(
                state.avg_price, state.remaining_cash, state.t, state.split_count
            )
        return normal_mode.generate_second_half_buy_orders(
            state.avg_price, state.remaining_cash, state.t, state.split_count
        )
    if state.mode == MODE_REVERSE:
        if state.reverse_day_count <= 1:
            return [reverse_mode.generate_day1_moc_sell_order(state.holding_qty, state.split_count)]
        return [reverse_mode.generate_daily_loc_sell_order(state.holding_qty, state.split_count, recent_closes)]
    return []


def load_dry_run_state(conn: sqlite3.Connection) -> State:
    """모의 계좌(shadow state) 조회 공용 함수. scheduler.py/dashboard에서 재사용합니다."""
    return load_state(conn, table=_STATE_TABLE)


def generate_dry_run_sell_orders(state: State) -> list[OrderIntent]:
    """모의 상태 기준 지정가매도(+15%) 주문(프리장 시점 생성분)."""
    if state.mode != MODE_NORMAL or state.holding_qty <= 0:
        return []
    return [
        o
        for o in normal_mode.generate_sell_orders(state.avg_price, state.holding_qty, state.t, state.split_count)
        if o.purpose == SELL_TYPE_LIMIT_15PCT
    ]
