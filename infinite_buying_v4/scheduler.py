"""
scheduler.py
=============
하루 단위 실행을 오케스트레이션하는 모듈입니다. state.py, event_log.py, trade_history.py,
normal_mode.py, reverse_mode.py, market_hours.py, kiwoom_adapter.py, notifier.py를 전부
이 모듈에서 순서대로 호출해 "오늘 무엇을 할지"를 실행합니다.

왜 실행을 두 번(프리장/본장)으로 나누는가?
- 설계도 6번은 "지정가매도는 프리장 시작 시각에 걸어서 프리~본장~애프터까지 유지"하고,
  "LOC매수/매도는 본장 중"에 넣으라고 명시합니다. 즉 지정가매도와 LOC/MOC 주문은
  제출 가능한 시간대가 다릅니다.
- 그래서 이 모듈은 하루에 두 번 실행되는 두 개의 독립된 진입점을 둡니다:
    1) run_premarket_update(): 프리장 시작 시점에 1회. 전일 체결을 반영해 상태(T, 평단,
       보유수량, 잔금, 모드)를 갱신하고, 그날의 지정가매도 주문을 새로 겁니다.
    2) run_regular_session_orders(): 본장 시작 시점에 1회. 그날의 LOC/MOC 매수·매도
       주문을 제출합니다.
- start_scheduler()가 이 두 진입점을 APScheduler cron 트리거(America/New_York
  타임존 고정)로 매일 자동 실행되게 등록합니다. 타임존을 고정해두면 서머타임 전환도
  APScheduler가 알아서 반영합니다(설계도 11번, market_hours.py와 동일한 이유).

주의(실거래 전 확인 필요, 아래 각 함수 docstring에도 반복 표기):
- kiwoom_adapter.get_today_fills()가 정확히 "어느 거래일" 체결을 반환하는지(당일 실행
  시점 기준 정말 최신 세션의 체결까지 포함하는지)는 실제 응답으로 검증이 필요합니다.
- 체결을 T값 갱신 이벤트로 분류하는 로직(_classify_daily_events)은 설계도 2번 규칙 중
  가장 해석의 여지가 있는 "지정가매도 후 같은 날 LOC매수 체결" 조합 케이스를 다룹니다.
  이 조합이 실제로 발생했을 때는 WARNING 알림을 보내 사람이 한 번 더 확인하도록
  했습니다 — 자동으로 조용히 넘어가지 않습니다.

멱등성/드라이런/예외 정책 (자세한 내용은 운영 런북 문서 참고):
- 멱등성: run_premarket_update()/run_regular_session_orders()는 각각 실행 맨 앞에서
  _claim_daily_run()으로 (run_type, 오늘날짜)를 선점합니다. 이미 선점되어 있으면(=오늘
  이미 실행됨, 컨테이너 재시작 등) 아무 side-effect 없이 즉시 반환합니다.
- 드라이런: config.dry_run=True면 모든 주문 제출이 _submit_or_dry_run()을 통해 실제
  API 호출 없이 로그로만 남습니다. KIWOOM_MODE(real/demo)와는 독립된 별개의 플래그입니다.
- 예외 정책: 이 모듈의 함수가 처리하지 않고 밖으로 던지는 예외(_run_guarded가 감쌈)는
  전부 "자동매매 중단" 대상입니다. CRITICAL 알림을 보낸 뒤 스케줄러 자체를 종료시킵니다
  (다음날 조용히 재시도하지 않음).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

from infinite_buying_v4 import db, event_log, normal_mode, reverse_mode, trade_history
from infinite_buying_v4.config import Config, load_config
from infinite_buying_v4.formulas import is_reverse_trigger, new_average_price
from infinite_buying_v4.kiwoom_adapter import (
    FillRecord,
    KiwoomAdapterError,
    cancel_all_open_orders,
    get_quote,
    get_recent_daily_closes,
    get_today_fills,
    submit_order_with_retry,
)
from infinite_buying_v4.market_hours import can_place_limit_sell_order, can_place_loc_or_moc_order, now_et
from infinite_buying_v4.notifier import LogNotifier, NotifierBase
from infinite_buying_v4.orders import OrderIntent
from infinite_buying_v4.state import (
    MODE_NORMAL,
    MODE_REVERSE,
    PHASE_FIRST,
    PHASE_FIRST_HALF,
    PHASE_SECOND_HALF,
    State,
    load_state,
    save_state,
    start_new_cycle,
)
from infinite_buying_v4.trade_history import (
    BUY_TYPE_FIRST,
    BUY_TYPE_FULL_STAR,
    BUY_TYPE_HALF_AVG,
    BUY_TYPE_HALF_STAR,
    SELL_TYPE_LIMIT_15PCT,
    SELL_TYPE_QUARTER,
    SELL_TYPE_REVERSE_LOC,
    SELL_TYPE_REVERSE_MOC,
)

logger = logging.getLogger("infinite_buying_v4.scheduler")

_BUY_ROUND_PURPOSES = (BUY_TYPE_FIRST, BUY_TYPE_HALF_STAR, BUY_TYPE_HALF_AVG, BUY_TYPE_FULL_STAR)
# 체결 비율이 이 값 이상이면 "전액 체결"로, 0보다 크면 "절반(부분) 체결"로 취급합니다
# (설계도 2번은 이분법적으로 "전액/절반"만 정의하므로, 실제로는 미세한 부분체결이 있어도
# 반올림해서 둘 중 하나로 분류합니다).
_FULL_FILL_RATIO_THRESHOLD = Decimal("0.99")


# ---------------------------------------------------------------------------
# 제출한 주문을 다음 실행 때 체결과 대조하기 위한 내부 장부 (submitted_orders 테이블)
# ---------------------------------------------------------------------------


def _record_submitted_order(conn: sqlite3.Connection, order_no: str, order: OrderIntent, submitted_date: date) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO submitted_orders
            (order_no, submitted_date, side, order_kind, price, qty, purpose, is_decoy)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            order_no,
            submitted_date.isoformat(),
            order.side,
            order.order_kind,
            str(order.price) if order.price is not None else None,
            order.qty,
            order.purpose,
            int(order.is_decoy),
        ),
    )


def _load_pending_orders(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = conn.execute("SELECT * FROM submitted_orders").fetchall()
    return {row["order_no"]: row for row in rows}


def _purge_submitted_order(conn: sqlite3.Connection, order_no: str) -> None:
    conn.execute("DELETE FROM submitted_orders WHERE order_no = ?", (order_no,))


def _purge_stale_submitted_orders(conn: sqlite3.Connection, *, before_date: date) -> int:
    """before_date보다 이전에 제출됐지만 체결 매칭이 안 된(=취소된) 주문 기록을 정리합니다.

    반드시 _classify_daily_events()로 그날의 체결 매칭을 **끝낸 뒤에만** 호출해야 합니다
    (먼저 지우면 매칭할 대상 자체가 사라져 버립니다). 정리하지 않고 방치하면 다음 실행의
    부분체결 비율 계산(buy_intended)에 예전 주문이 계속 끼어들어 결과가 왜곡됩니다.
    """
    cursor = conn.execute("DELETE FROM submitted_orders WHERE submitted_date < ?", (before_date.isoformat(),))
    return cursor.rowcount


# ---------------------------------------------------------------------------
# 멱등성(중복 실행 방지) 가드 — daily_run_log 테이블
# ---------------------------------------------------------------------------


def _claim_daily_run(conn: sqlite3.Connection, run_type: str, run_date: date) -> bool:
    """(run_type, run_date) 조합을 오늘의 실행으로 선점합니다.

    판정 키: **(run_type, run_date)** — run_type은 "PREMARKET" 또는 "REGULAR",
    run_date는 America/New_York 기준 오늘 날짜(YYYY-MM-DD)입니다.
    daily_run_log 테이블의 PRIMARY KEY 제약을 이용해 "먼저 선점한 하나만 통과"를
    SQLite 레벨에서 원자적으로 보장합니다(파이썬 코드의 if문으로 체크하는 것보다
    안전 — 프로세스가 거의 동시에 두 번 뜨는 경쟁 상황에도 DB가 막아줍니다).

    반환값: 선점에 성공(=오늘 이 작업을 처음 시작함)하면 True, 이미 선점되어
    있으면(=오늘 이미 실행됨, 컨테이너 재시작 등으로 다시 호출된 경우) False.

    의도적으로 "먼저 선점 -> 그 다음에 실제 작업 수행" 순서를 씁니다. 즉 이번 실행이
    중간에 실패(프로세스 죽음 등)하더라도 daily_run_log 행은 이미 남아있으므로,
    **재시작해도 같은 날 같은 작업은 자동으로 다시 실행되지 않습니다.** 이는 "실패하면
    안전하게 재시도"보다 "실패하면 사람이 확인하기 전까지는 멈춰있는 것"이 주문 중복
    제출보다 낫다는 판단입니다(운영 런북의 "프로세스 중단 복구" 절차 참고 — 정말
    재실행이 필요하면 daily_run_log에서 해당 행을 수동으로 지우고 재시작해야 합니다).
    """
    try:
        conn.execute(
            "INSERT INTO daily_run_log (run_type, run_date, claimed_at) VALUES (?, ?, ?)",
            (run_type, run_date.isoformat(), datetime.now(timezone.utc).isoformat()),
        )
        return True
    except sqlite3.IntegrityError:
        return False


# ---------------------------------------------------------------------------
# 드라이런(주문 미제출, 로그만) 지원
# ---------------------------------------------------------------------------


def _submit_or_dry_run(
    config: Config,
    order: OrderIntent,
    notifier: NotifierBase,
    conn: sqlite3.Connection,
    submitted_date: date,
):
    """config.dry_run에 따라 실제 제출 또는 로그 기록만 수행하는 공용 진입점.

    모든 주문 제출은 반드시 이 함수를 거쳐야 합니다(kiwoom_adapter.submit_order_with_retry를
    직접 호출하는 코드가 scheduler.py 안에 더 있으면 안 됨) — 드라이런 플래그가 새는
    것을 막기 위한 단일 지점입니다.
    """
    if config.dry_run:
        logger.info(
            "[DRY-RUN] 주문 미제출 (실제 API 호출 없음): side=%s kind=%s price=%s qty=%s purpose=%s decoy=%s",
            order.side,
            order.order_kind,
            order.price,
            order.qty,
            order.purpose,
            order.is_decoy,
        )
        return None

    try:
        submitted = submit_order_with_retry(config, order, notifier=notifier)
    except KiwoomAdapterError:
        return None  # submit_order_with_retry가 이미 CRITICAL 알림을 보냈습니다.
    _record_submitted_order(conn, submitted.order_no, order, submitted_date)
    return submitted


# ---------------------------------------------------------------------------
# 체결 -> T값 갱신 이벤트 분류 (설계도 2번)
# ---------------------------------------------------------------------------


def _classify_daily_events(
    conn: sqlite3.Connection, fills: list[FillRecord], notifier: NotifierBase
) -> list[str]:
    """오늘 확인된 체결들을 submitted_orders 장부와 대조해, event_log.EVENT_* 이벤트
    목록으로 변환합니다. 매칭에 사용한 submitted_orders 행은 여기서 정리(delete)합니다.

    **부분체결 판정의 핵심**: "체결 비율(fill_ratio)"은 "체결된 주문들의 체결액 합"이
    아니라 **"그 라운드에 제출된 모든 매수 주문의 의도 금액(buy_intended) 대비 실제
    체결액(buy_filled)"**으로 계산해야 합니다. 예를 들어 전반전 절반매수(별지점+평단,
    2건)에서 한쪽만 체결되면, 체결된 쪽만 보면 100%처럼 보이지만 전체 라운드 기준으로는
    50%입니다. 그래서 buy_intended는 **fills에 등장했는지와 무관하게 submitted_orders에
    남아있는 모든 매수-라운드 주문**을 기준으로 먼저 집계합니다(아래 1단계).
    """
    pending = _load_pending_orders(conn)

    buy_intended = Decimal(0)
    buy_filled = Decimal(0)
    quarter_sell_filled = False
    limit_sell_filled = False

    # 1단계: 오늘 매칭 대상인 매수-라운드 주문의 "의도 금액" 총합을 먼저 구합니다.
    # 아직 체결되지 않은(=fills에 없는) 형제 주문도 반드시 포함해야 부분체결 비율이
    # 정확해집니다.
    for row in pending.values():
        if row["is_decoy"]:
            continue
        if row["purpose"] in _BUY_ROUND_PURPOSES:
            buy_intended += Decimal(row["qty"]) * Decimal(row["price"] or "0")

    # 2단계: 실제 체결을 하나씩 대조합니다.
    for fill in fills:
        row = pending.get(fill.order_no)
        if row is None:
            # 우리가 이번 실행에서 제출하지 않은(예: 수동 개입, 이전 실행 유실) 체결입니다.
            # 조용히 무시하지 않고 경고만 남깁니다 — 설계도 11번 불일치 감지의 일종입니다.
            logger.warning("submitted_orders에 없는 체결을 발견했습니다 (order_no=%s). 수동 확인 필요.", fill.order_no)
            continue

        purpose = row["purpose"]
        if row["is_decoy"]:
            # 미끼 주문이 실제로 체결됐다면 심각한 이상 상황입니다(설계도 5-1, 11번).
            notifier.notify_critical(
                "미끼(decoy) 주문이 실제로 체결됨",
                f"order_no={fill.order_no}, price={fill.fill_price}, qty={fill.fill_qty}. "
                f"시세 급등 등 예상 밖 상황일 수 있으니 즉시 확인하세요.",
            )
        elif purpose in _BUY_ROUND_PURPOSES:
            buy_filled += Decimal(fill.fill_qty) * fill.fill_price
        elif purpose == SELL_TYPE_QUARTER:
            quarter_sell_filled = True
        elif purpose == SELL_TYPE_LIMIT_15PCT:
            limit_sell_filled = True

        _purge_submitted_order(conn, fill.order_no)

    events: list[str] = []

    if quarter_sell_filled:
        events.append(event_log.EVENT_QUARTER_SELL)

    if buy_filled > 0:
        fill_ratio = (buy_filled / buy_intended) if buy_intended > 0 else Decimal(0)
        is_full = fill_ratio >= _FULL_FILL_RATIO_THRESHOLD

        if limit_sell_filled:
            # 설계도 2번의 복합 규칙: "지정가매도 체결 후 같은 날 LOC매수까지 체결".
            # 자동 판정이 맞는지 사람이 한 번 더 확인할 수 있도록 WARNING을 남깁니다.
            notifier.notify_warning(
                "지정가매도 + 같은 날 LOC매수 동시 체결 감지",
                f"buy_fill_ratio={fill_ratio:.4f}, is_full={is_full}. "
                f"T값 갱신 규칙(설계도 2번)의 복합 케이스가 적용됩니다. 결과를 확인하세요.",
            )
            events.append(
                event_log.EVENT_LIMIT_SELL_THEN_LOC_BUY_FULL
                if is_full
                else event_log.EVENT_LIMIT_SELL_THEN_LOC_BUY_HALF
            )
        else:
            events.append(event_log.EVENT_FULL_BUY if is_full else event_log.EVENT_HALF_BUY)

    return events


# ---------------------------------------------------------------------------
# 프리장 시작 시점 실행: 전일 체결 반영 + 상태 갱신 + 지정가매도 주문 갱신
# ---------------------------------------------------------------------------


def run_premarket_update(config: Config, conn: sqlite3.Connection, notifier: NotifierBase) -> State:
    """프리장 시작 시점에 1회 실행합니다 (설계도 0, 2, 6번).

    1) 전일 미체결 주문 정리
    2) 전일 체결 조회 -> 이벤트 분류/기록 -> T·평단·보유수량·잔금 갱신
    3) 사이클 종료/리버스모드 진입·이탈 판정
    4) 상태 저장
    5) 보유 중이면 오늘의 지정가매도(+15%) 주문을 새로 걸기
    6) portfolio_summary 갱신
    """
    today = now_et().date()

    if not _claim_daily_run(conn, "PREMARKET", today):
        logger.info("오늘(%s)의 프리장 갱신은 이미 실행되었습니다. 중복 실행을 건너뜁니다.", today.isoformat())
        return load_state(conn)

    logger.info("프리장 갱신 시작 (%s)", today.isoformat())

    try:
        cancelled = cancel_all_open_orders(config)
        logger.info("미체결 주문 %d건 정리 완료", cancelled)
    except KiwoomAdapterError as exc:
        notifier.notify_critical("미체결 주문 정리 실패", str(exc))
        raise

    state = load_state(conn)

    try:
        fills = get_today_fills(config)
    except KiwoomAdapterError as exc:
        notifier.notify_critical("체결 조회 실패 - 자동매매 중단", str(exc))
        raise

    if fills:
        state = _apply_fills(conn, config, state, fills, today, notifier)

    # 체결과 매칭되지 못하고 남은(=체결 없이 취소된) 이전 주문 기록을 정리합니다.
    # cancel_all_open_orders()가 이미 증권사 쪽 주문은 취소했으므로, 이 시점에도
    # submitted_orders에 남아있는 "오늘 이전" 행은 앞으로도 절대 체결될 일이 없습니다.
    # 여기서 정리하지 않으면 다음 실행의 _classify_daily_events() buy_intended 계산에
    # 계속 끼어들어 체결 비율을 왜곡시킵니다.
    purged = _purge_stale_submitted_orders(conn, before_date=today)
    if purged:
        logger.info("오늘 이전 미체결(취소됨) 주문 기록 %d건 정리 완료", purged)

    state = _handle_cycle_and_mode_transitions(conn, config, state, today, notifier)
    save_state(conn, state)

    if state.holding_qty > 0:
        _submit_limit_sell_order(config, conn, state, today, notifier)

    try:
        quote = get_quote(config)
        trade_history.update_portfolio_summary(
            conn,
            current_price=quote.current_price,
            remaining_cash=state.remaining_cash,
            avg_price=state.avg_price,
            holding_qty=state.holding_qty,
        )
    except KiwoomAdapterError as exc:
        # 시세 조회 실패는 CRITICAL까지는 아니고(주문 로직에 영향 없음), 다음 실행에서
        # 다시 시도하면 되므로 WARNING으로 남깁니다.
        notifier.notify_warning("포트폴리오 요약 갱신용 시세 조회 실패", str(exc))

    logger.info("프리장 갱신 완료: mode=%s, T=%s, holding_qty=%d", state.mode, state.t, state.holding_qty)
    return state


def _apply_fills(
    conn: sqlite3.Connection,
    config: Config,
    state: State,
    fills: list[FillRecord],
    today: date,
    notifier: NotifierBase,
) -> State:
    """체결 목록을 trade_history에 기록하고, event_log를 재생해 T를 갱신하고,
    평단가/보유수량/잔금을 반영한 새 State를 반환합니다.
    """
    t = state.t
    holding_qty = state.holding_qty
    avg_price = state.avg_price
    remaining_cash = state.remaining_cash

    for fill in fills:
        submitted_row = conn.execute(
            "SELECT * FROM submitted_orders WHERE order_no = ?", (fill.order_no,)
        ).fetchone()
        purpose = submitted_row["purpose"] if submitted_row else "UNKNOWN"

        if fill.side == "BUY":
            avg_price = new_average_price(avg_price, holding_qty, fill.fill_price, fill.fill_qty)
            holding_qty += fill.fill_qty
            remaining_cash -= fill.fill_price * Decimal(fill.fill_qty)
            trade_history.record_buy(
                conn,
                cycle_id=state.cycle_id,
                buy_date=today,
                buy_price=fill.fill_price,
                buy_qty=fill.fill_qty,
                order_type=purpose if purpose != "UNKNOWN" else BUY_TYPE_FIRST,
                t_after=t,  # T는 아래에서 이벤트 반영 후 최종값으로 다시 저장되므로 임시값
                avg_price_after=avg_price,
            )
        else:  # SELL
            holding_qty -= fill.fill_qty
            remaining_cash += fill.fill_price * Decimal(fill.fill_qty)
            trade_history.record_sell(
                conn,
                cycle_id=state.cycle_id,
                sell_date=today,
                sell_price=fill.fill_price,
                sell_qty=fill.fill_qty,
                order_type=purpose if purpose != "UNKNOWN" else SELL_TYPE_QUARTER,
                avg_price_at_sell=avg_price,
                t_after=t,
            )

    events = _classify_daily_events(conn, fills, notifier)
    for event_type in events:
        event = event_log.make_event(event_date=today, event_type=event_type)
        event_log.append_event(config.event_log_path, event)
        t = event_log.apply_event(t, event_type)

    if holding_qty == 0:
        avg_price = Decimal(0)

    return replace(state, t=t, holding_qty=holding_qty, avg_price=avg_price, remaining_cash=remaining_cash)


def _handle_cycle_and_mode_transitions(
    conn: sqlite3.Connection, config: Config, state: State, today: date, notifier: NotifierBase
) -> State:
    """사이클 종료(보유 0), 리버스모드 진입(T > 분할수-1), 리버스모드 이탈(종가 조건)을
    판정해 상태를 전이시킵니다 (설계도 3, 7-1, 7-3, 8번).
    """
    if state.holding_qty == 0 and state.t > 0:
        # 사이클이 방금 끝났습니다 (매도로 보유수량이 0이 됨).
        trade_history.close_cycle_summary(conn, cycle_id=state.cycle_id, end_date=today)
        notifier.notify_info(
            f"사이클 {state.cycle_id} 종료", f"종료일={today.isoformat()}, 잔금={state.remaining_cash}"
        )
        new_state = start_new_cycle(
            conn,
            state,
            fixed_principal=config.principal,
            compound_on_restart=config.compound_on_restart,
            start_date=today,
        )
        trade_history.open_cycle_summary(conn, cycle_id=new_state.cycle_id, start_date=today)
        return new_state

    if state.mode == MODE_NORMAL and is_reverse_trigger(state.t, state.split_count):
        notifier.notify_warning(f"리버스모드 진입 (사이클 {state.cycle_id})", f"T={state.t}")
        trade_history.mark_cycle_hit_reverse_mode(conn, cycle_id=state.cycle_id)
        return replace(state, mode=MODE_REVERSE, phase=None, reverse_day_count=0, reverse_prev_qty=state.holding_qty)

    if state.mode == MODE_REVERSE:
        try:
            quote = get_quote(config)
        except KiwoomAdapterError as exc:
            notifier.notify_warning("리버스모드 종료조건 판정용 시세 조회 실패", str(exc))
            return state
        if reverse_mode.is_reverse_exit_condition(quote.current_price, state.avg_price):
            notifier.notify_info(f"일반모드 복귀 (사이클 {state.cycle_id})", f"종가={quote.current_price}, 평단={state.avg_price}")
            # 후반전 T 계산 공식이 그대로 유효하려면 phase를 다시 지정해야 합니다.
            # 리버스모드 진입은 항상 "T > 분할수-1"에서 발생하므로 복귀 시에도 후반전으로 취급합니다.
            return replace(state, mode=MODE_NORMAL, phase=PHASE_SECOND_HALF)
        return replace(state, reverse_day_count=state.reverse_day_count + 1, reverse_prev_qty=state.holding_qty)

    # 일반모드 phase 재계산 (전반전<->후반전 경계를 매일 다시 확인)
    if state.mode == MODE_NORMAL and state.holding_qty > 0:
        from infinite_buying_v4.formulas import is_first_half

        new_phase = PHASE_FIRST_HALF if is_first_half(state.t, state.split_count) else PHASE_SECOND_HALF
        if new_phase != state.phase and state.phase != PHASE_FIRST:
            return replace(state, phase=new_phase)

    return state


def _submit_limit_sell_order(
    config: Config, conn: sqlite3.Connection, state: State, today: date, notifier: NotifierBase
) -> None:
    """지정가매도(+15%) 주문을 프리장 시작 시점에 새로 겁니다 (설계도 6번)."""
    if not can_place_limit_sell_order():
        logger.info("지정가매도 주문 제출 가능 시간대가 아니므로 건너뜁니다.")
        return

    orders = normal_mode.generate_sell_orders(state.avg_price, state.holding_qty, state.t, state.split_count)
    limit_orders = [o for o in orders if o.purpose == SELL_TYPE_LIMIT_15PCT]
    for order in limit_orders:
        _submit_or_dry_run(config, order, notifier, conn, today)


# ---------------------------------------------------------------------------
# 본장 시작 시점 실행: 그날의 LOC/MOC 매수·매도 주문 제출
# ---------------------------------------------------------------------------


def run_regular_session_orders(config: Config, conn: sqlite3.Connection, notifier: NotifierBase) -> None:
    """본장 시작 시점에 1회 실행합니다 (설계도 5, 6, 7번). 그날의 LOC/MOC 주문을 제출합니다.

    시간대 체크(can_place_loc_or_moc_order)는 side-effect가 없는 순수 조건이므로 멱등성
    선점(_claim_daily_run)보다 먼저 확인합니다 — 잘못된 시간에 호출되어 조기 반환하는
    경우까지 "오늘 실행 완료"로 선점해버리면, 정작 본장 시간에 정상 호출됐을 때 이미
    선점된 것으로 오판해 주문을 못 내는 사고로 이어지기 때문입니다.
    """
    if not can_place_loc_or_moc_order():
        logger.info("LOC/MOC 주문 제출 가능 시간대(본장)가 아니므로 건너뜁니다.")
        return

    today = now_et().date()

    if not _claim_daily_run(conn, "REGULAR", today):
        logger.info("오늘(%s)의 본장 주문 제출은 이미 실행되었습니다. 중복 실행을 건너뜁니다.", today.isoformat())
        return

    state = load_state(conn)

    orders: list[OrderIntent] = []
    if state.mode == MODE_NORMAL:
        orders = _generate_normal_mode_orders(config, state)
    elif state.mode == MODE_REVERSE:
        orders = _generate_reverse_mode_orders(config, state)

    for order in orders:
        _submit_or_dry_run(config, order, notifier, conn, today)

    logger.info("본장 주문 제출 완료: %d건 (dry_run=%s)", len(orders), config.dry_run)


def _generate_normal_mode_orders(config: Config, state: State) -> list[OrderIntent]:
    if state.holding_qty == 0:
        quote = get_quote(config)
        return normal_mode.generate_first_buy_orders(quote.prev_close, state.remaining_cash, state.split_count)
    if state.phase == PHASE_FIRST_HALF:
        return normal_mode.generate_first_half_buy_orders(
            state.avg_price, state.remaining_cash, state.t, state.split_count
        )
    return normal_mode.generate_second_half_buy_orders(
        state.avg_price, state.remaining_cash, state.t, state.split_count
    )


def _generate_reverse_mode_orders(config: Config, state: State) -> list[OrderIntent]:
    if state.reverse_day_count <= 1:
        return [reverse_mode.generate_day1_moc_sell_order(state.holding_qty, state.split_count)]
    recent_closes = get_recent_daily_closes(config, count=5)
    return [reverse_mode.generate_daily_loc_sell_order(state.holding_qty, state.split_count, recent_closes)]


# ---------------------------------------------------------------------------
# APScheduler 등록
# ---------------------------------------------------------------------------


def _run_guarded(run_type: str, fn, config: Config, conn: sqlite3.Connection, notifier: NotifierBase) -> None:
    """작업 실행을 감싸서, 처리되지 않은 예외(StateError/FormulaError/NormalModeError/
    KiwoomAdapterError 등 무엇이든)가 나면 CRITICAL 알림을 보내고 다시 발생시킵니다
    (설계도 11번 "자동매매 중단 + 알림"의 실제 트리거 지점입니다).

    다시 발생시킨 예외는 APScheduler의 EVENT_JOB_ERROR로 전달되고, start_scheduler()에
    등록된 리스너가 이를 받아 스케줄러 자체를 종료시킵니다. "로그만 남기고 다음 거래일에
    자동으로 재시도"하지 않는 이유: 이 예외들은 대부분 일시적 네트워크 문제가 아니라
    상태/로직 불일치를 의미하므로, 원인 파악 전에 자동으로 계속 도는 것이 더 위험합니다.
    복구 절차는 운영 런북 문서의 "장애 복구 런북" 섹션을 참고하세요.
    """
    try:
        fn(config, conn, notifier)
    except Exception as exc:
        logger.exception("%s 작업 중 처리되지 않은 예외가 발생했습니다.", run_type)
        notifier.notify_critical(
            f"{run_type} 작업 실패 - 자동매매 중단",
            f"{type(exc).__name__}: {exc}\n\n조치 방법은 운영 런북의 '장애 복구' 절차를 참고하세요.",
        )
        raise


def start_scheduler(config: Config | None = None, notifier: NotifierBase | None = None):
    """APScheduler에 프리장/본장 작업을 등록하고 블로킹 실행합니다.

    타임존을 America/New_York으로 고정한 cron 트리거를 쓰므로, 서머타임 전환일에도
    "현지 시각 04:00/09:30"이라는 의미가 자동으로 유지됩니다(설계도 11번).

    작업 중 처리되지 않은 예외가 발생하면(_run_guarded 참고) EVENT_JOB_ERROR 리스너가
    스케줄러를 즉시 종료시키고, 이 함수는 반환되며, 프로세스는 종료 코드 1로 끝납니다.
    docker-compose.yml의 `restart: unless-stopped` 정책과 조합하면 컨테이너가 자동
    재시작되는데, 원인이 일시적이면 다음 실행에서 정상화되지만 원인이 지속적(설정
    오류, 상태 손상 등)이면 같은 실패가 반복되며 그때마다 CRITICAL 알림이 발생합니다.
    반복 알림이 오면 재시작에 맡기지 말고 반드시 운영 런북대로 직접 원인을 조치하세요.
    """
    from apscheduler.events import EVENT_JOB_ERROR
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    from infinite_buying_v4.market_hours import MARKET_TZ, PREMARKET_START, REGULAR_START

    # notifier를 config보다 먼저 준비해둡니다 — load_config()/get_connection()이
    # 실패해도(설정 누락, DB 경로 문제 등) 최소한 CRITICAL 로그를 남길 수 있도록
    # 하기 위함입니다(그 전까지는 notifier 자체가 없어 어떤 알림도 남길 수 없었습니다).
    notifier = notifier or LogNotifier()
    try:
        config = config or load_config()
        conn = db.get_connection(config.db_path)
    except Exception as exc:
        logger.exception("설정/DB 초기화 실패로 스케줄러를 시작할 수 없습니다.")
        notifier.notify_critical("설정/DB 초기화 실패 - 스케줄러 시작 불가", f"{type(exc).__name__}: {exc}")
        raise

    scheduler = BlockingScheduler(timezone=MARKET_TZ)
    shutdown_state = {"had_error": False}

    def _on_job_error(event) -> None:
        shutdown_state["had_error"] = True
        logger.critical("작업 오류로 스케줄러를 종료합니다 (job_id=%s).", event.job_id)
        scheduler.shutdown(wait=False)

    scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)

    scheduler.add_job(
        lambda: _run_guarded("PREMARKET", run_premarket_update, config, conn, notifier),
        trigger=CronTrigger(
            day_of_week="mon-fri", hour=PREMARKET_START.hour, minute=PREMARKET_START.minute, timezone=MARKET_TZ
        ),
        id="premarket_update",
    )
    scheduler.add_job(
        lambda: _run_guarded("REGULAR", run_regular_session_orders, config, conn, notifier),
        trigger=CronTrigger(
            day_of_week="mon-fri", hour=REGULAR_START.hour, minute=REGULAR_START.minute, timezone=MARKET_TZ
        ),
        id="regular_session_orders",
    )

    logger.info(
        "스케줄러 시작: 프리장=%s ET, 본장=%s ET (America/New_York 기준), dry_run=%s",
        PREMARKET_START,
        REGULAR_START,
        config.dry_run,
    )
    scheduler.start()

    if shutdown_state["had_error"]:
        import sys

        logger.critical("작업 오류로 스케줄러가 종료되었습니다. 종료 코드 1로 프로세스를 종료합니다.")
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    start_scheduler()
