"""
dashboard/data.py
==================
대시보드가 화면에 표시할 데이터를 SQLite에서 읽어 JSON 직렬화 가능한 딕셔너리로
조립하는 모듈입니다 (설계도 12번).

핵심 원칙 (설계도 12-1번 "별도 계산 로직 중복 금지"):
- 이 모듈은 새로운 수익률/손익 계산을 독자적으로 수행하지 않습니다. trade_history.py가
  이미 계산해서 SQLite에 저장해둔 값(portfolio_summary, cycle_summary)을 그대로
  읽어오거나, formulas.py의 순수함수(다음 매수/매도 목표가처럼 "현재 상태만으로
  계산 가능한" 값)를 재사용합니다.
- 키움 API를 직접 호출하지 않습니다. "현재가"는 scheduler.py가 매매 시점마다 이미
  portfolio_summary.current_unrealized_pnl에 반영해둔 값에서 역산합니다
  (current_unrealized_pnl = (현재가 - 평단가) * 보유수량 이므로,
   현재가 = 평단가 + 미실현손익 / 보유수량). 이 값은 scheduler.py의 마지막 갱신
  시점 스냅샷이며, 진짜 실시간 시세가 아닙니다(운영 런북 참고).
- Flask에 대한 의존이 전혀 없는 순수 데이터 계층이라, 실제 HTTP 서버 없이도
  단위테스트로 검증할 수 있습니다(test_dashboard_data.py).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from infinite_buying_v4 import dry_run_simulator
from infinite_buying_v4.formulas import buy_trigger_price, sell_trigger_price
from infinite_buying_v4.state import MODE_NORMAL, MODE_REVERSE, PHASE_FIRST, StateError, load_state

_RECENT_TRADES_LIMIT = 20


def _to_float(value: Any) -> float | None:
    """Decimal/None을 JSON 직렬화 가능한 float로 변환하는 표시 전용 헬퍼.

    반환값은 화면 표시에만 쓰이고, 이 값으로 추가 금액 계산을 다시 하지 않습니다
    (내부 계산은 항상 state.py/trade_history.py의 Decimal 값으로 이루어짐).
    """
    if value is None:
        return None
    return float(value)


def _load_portfolio_row(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """portfolio_summary의 단일 행을 Decimal 타입 그대로 읽어옵니다 (내부용).

    get_current_cycle_status()가 "현재가 역산"에 Decimal 정밀도가 필요해서 쓰는
    내부 헬퍼입니다. 외부(서버 라우트)에는 get_portfolio_summary()의 float 버전만 노출합니다.
    """
    row = conn.execute("SELECT * FROM portfolio_summary WHERE id = 1").fetchone()
    if row is None:
        return None
    return {
        "strategy_start_date": row["strategy_start_date"],
        "initial_principal": Decimal(row["initial_principal"]),
        "total_realized_profit": Decimal(row["total_realized_profit"]),
        "total_realized_return_pct": Decimal(row["total_realized_return_pct"]),
        "current_unrealized_pnl": Decimal(row["current_unrealized_pnl"]),
        "current_unrealized_return_pct": Decimal(row["current_unrealized_return_pct"]),
        "total_equity": Decimal(row["total_equity"]),
        "total_return_pct": Decimal(row["total_return_pct"]),
        "completed_cycles": row["completed_cycles"],
        "last_updated": row["last_updated"],
    }


def get_portfolio_summary(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """상단 요약 카드용 데이터 (설계도 12-2번 "상단 요약 카드").

    아직 bootstrap.py를 실행하지 않아 portfolio_summary 행이 없으면 None을 반환합니다
    (대시보드 쪽에서 "아직 시작 전" 안내를 표시하도록).
    """
    raw = _load_portfolio_row(conn)
    if raw is None:
        return None
    return {
        "strategy_start_date": raw["strategy_start_date"],
        "initial_principal": _to_float(raw["initial_principal"]),
        "total_realized_profit": _to_float(raw["total_realized_profit"]),
        "total_realized_return_pct": _to_float(raw["total_realized_return_pct"]),
        "current_unrealized_pnl": _to_float(raw["current_unrealized_pnl"]),
        "current_unrealized_return_pct": _to_float(raw["current_unrealized_return_pct"]),
        "total_equity": _to_float(raw["total_equity"]),
        "total_return_pct": _to_float(raw["total_return_pct"]),
        "completed_cycles": raw["completed_cycles"],
        "last_updated": raw["last_updated"],
    }


def get_current_cycle_status(conn: sqlite3.Connection, *, today: date) -> dict[str, Any] | None:
    """현재 사이클 현황 데이터 (설계도 12-2번 "현재 사이클 현황").

    state 테이블이 아직 없으면(bootstrap 전) None을 반환합니다.
    """
    try:
        s = load_state(conn)
    except StateError:
        return None

    elapsed_days = (today - s.cycle_start_date).days

    # 현재가 역산: current_unrealized_pnl = (현재가 - 평단가) * 보유수량 이므로
    # 현재가 = 평단가 + 미실현손익 / 보유수량. 보유수량이 0이면 역산할 수 없으므로 None.
    current_price: Decimal | None = None
    if s.holding_qty > 0:
        portfolio_raw = _load_portfolio_row(conn)
        if portfolio_raw is not None:
            current_price = s.avg_price + (portfolio_raw["current_unrealized_pnl"] / Decimal(s.holding_qty))

    next_buy_price: Decimal | None = None
    next_sell_price: Decimal | None = None
    note: str | None = None

    if s.mode == MODE_NORMAL:
        if s.phase == PHASE_FIRST:
            note = "첫매수 대기 중 (아직 평단가가 형성되지 않았습니다)"
        else:
            next_buy_price = buy_trigger_price(s.avg_price, s.t, s.split_count)
            next_sell_price = sell_trigger_price(s.avg_price, s.t, s.split_count)
    elif s.mode == MODE_REVERSE:
        note = (
            "리버스모드: 매도가는 직전 5거래일 종가 평균으로 매일 재계산되며, "
            "실시간 시세가 필요해 대시보드에서는 미리보기를 제공하지 않습니다. "
            "scheduler.py 로그를 확인하세요."
        )

    return {
        "cycle_id": s.cycle_id,
        "mode": s.mode,
        "phase": s.phase,
        "start_date": s.cycle_start_date.isoformat(),
        "elapsed_days": elapsed_days,
        "t": _to_float(s.t),
        "split_count": s.split_count,
        "avg_price": _to_float(s.avg_price) if s.holding_qty > 0 else None,
        "current_price": _to_float(current_price),
        "holding_qty": s.holding_qty,
        "remaining_cash": _to_float(s.remaining_cash),
        "next_buy_price": _to_float(next_buy_price),
        "next_sell_price": _to_float(next_sell_price),
        "reverse_day_count": s.reverse_day_count if s.mode == MODE_REVERSE else None,
        "note": note,
    }


def get_recent_trades(conn: sqlite3.Connection, *, limit: int = _RECENT_TRADES_LIMIT) -> list[dict[str, Any]]:
    """최근 거래 내역 테이블용 데이터 (설계도 12-2번 "최근 거래 내역 테이블").

    buy_records/sell_records를 하나로 합쳐 기록 시각(created_at) 최신순으로 최대
    limit건 반환합니다.
    """
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT buy_date AS trade_date, 'BUY' AS side, buy_price AS price, buy_qty AS qty,
                   order_type, NULL AS profit_amount, NULL AS return_pct, created_at
            FROM buy_records
            UNION ALL
            SELECT sell_date AS trade_date, 'SELL' AS side, sell_price AS price, sell_qty AS qty,
                   order_type, profit_amount, return_pct, created_at
            FROM sell_records
        )
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    return [
        {
            "date": row["trade_date"],
            "side": row["side"],
            "price": _to_float(Decimal(row["price"])),
            "qty": row["qty"],
            "order_type": row["order_type"],
            "profit_amount": _to_float(Decimal(row["profit_amount"])) if row["profit_amount"] is not None else None,
            "return_pct": _to_float(Decimal(row["return_pct"])) if row["return_pct"] is not None else None,
        }
        for row in rows
    ]


def get_cycle_history(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """사이클별 성과 히스토리 (설계도 12-2번 "사이클별 성과 히스토리", cycle_return_pct 막대그래프).

    완료된 사이클(end_date가 있는 것)만 오래된 순으로 반환합니다.
    """
    rows = conn.execute(
        """
        SELECT cycle_id, start_date, end_date, cycle_return_pct, cycle_profit_amount, hit_reverse_mode
        FROM cycle_summary
        WHERE end_date IS NOT NULL
        ORDER BY cycle_id ASC
        """
    ).fetchall()

    return [
        {
            "cycle_id": row["cycle_id"],
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "cycle_return_pct": _to_float(Decimal(row["cycle_return_pct"])) if row["cycle_return_pct"] is not None else None,
            "cycle_profit_amount": (
                _to_float(Decimal(row["cycle_profit_amount"])) if row["cycle_profit_amount"] is not None else None
            ),
            "hit_reverse_mode": bool(row["hit_reverse_mode"]),
        }
        for row in rows
    ]


def get_today_order_activity(conn: sqlite3.Connection, *, today: date) -> dict[str, Any]:
    """오늘 제출된 주문 중 "이미 체결된 것"과 "아직 체결 대기 중인 것"을 구분해서 반환합니다.

    `get_recent_trades()`(최근 거래 내역)는 **체결이 확인된 것만** 보여주므로, "오늘
    주문은 냈는데 아직 체결 안 된 것"은 거기 나타나지 않습니다. 로그를 볼 수 없는
    사람도 대시보드만으로 "오늘 무슨 주문이 나갔고 그중 뭐가 체결됐는지"를 전부
    파악할 수 있도록 이 함수를 별도로 둡니다.

    - `filled`: buy_records/sell_records 중 오늘 날짜인 것 (이미 체결 확정)
    - `pending`: submitted_orders 중 오늘 제출된 것 (아직 체결 매칭 전 = 대기 중)

    오늘 제출됐다가 끝내 체결되지 않고 취소된 주문은, 다음 프리장 실행 시점에
    `submitted_orders`에서 `cancelled_orders`로 옮겨져 영구 이력으로 남습니다 —
    그 내역은 `get_recent_cancelled_orders()`로 조회합니다(오늘 이 함수의 `pending`에는
    당연히 나타나지 않습니다. 아직 오늘 취소가 확정되지 않았기 때문).
    """
    filled_rows = conn.execute(
        """
        SELECT * FROM (
            SELECT buy_date AS trade_date, 'BUY' AS side, buy_price AS price, buy_qty AS qty,
                   order_type, NULL AS profit_amount, NULL AS return_pct, created_at
            FROM buy_records WHERE buy_date = ?
            UNION ALL
            SELECT sell_date AS trade_date, 'SELL' AS side, sell_price AS price, sell_qty AS qty,
                   order_type, profit_amount, return_pct, created_at
            FROM sell_records WHERE sell_date = ?
        )
        ORDER BY created_at ASC
        """,
        (today.isoformat(), today.isoformat()),
    ).fetchall()

    pending_rows = conn.execute(
        "SELECT * FROM submitted_orders WHERE submitted_date = ? ORDER BY side, purpose",
        (today.isoformat(),),
    ).fetchall()

    return {
        "filled": [
            {
                "side": row["side"],
                "price": _to_float(Decimal(row["price"])),
                "qty": row["qty"],
                "order_type": row["order_type"],
                "profit_amount": _to_float(Decimal(row["profit_amount"])) if row["profit_amount"] is not None else None,
                "return_pct": _to_float(Decimal(row["return_pct"])) if row["return_pct"] is not None else None,
            }
            for row in filled_rows
        ],
        "pending": [
            {
                "side": row["side"],
                "order_kind": row["order_kind"],
                "price": _to_float(Decimal(row["price"])) if row["price"] is not None else None,
                "qty": row["qty"],
                "purpose": row["purpose"],
                "is_decoy": bool(row["is_decoy"]),
            }
            for row in pending_rows
        ],
    }


_RECENT_CANCELLED_LIMIT = 20


def get_recent_cancelled_orders(conn: sqlite3.Connection, *, limit: int = _RECENT_CANCELLED_LIMIT) -> list[dict[str, Any]]:
    """최근 취소(미체결 정리)된 주문 이력을 반환합니다.

    `submitted_orders`에 남아있던 주문이 체결 매칭 없이 오래되면(=취소로 간주),
    `scheduler._purge_stale_submitted_orders()`가 삭제 직전에 `cancelled_orders`로
    옮겨 담습니다(설계도 범위 밖, 이 프로젝트가 추가한 운영용 이력 테이블). 이 함수가
    없으면 "오늘의 주문 현황"에서 대기 중이던 주문이 다음날 조용히 사라지는 것만 보이고,
    실제로 취소됐다는 사실 자체는 로그 없이는 알 수 없었습니다.
    """
    rows = conn.execute(
        "SELECT * FROM cancelled_orders ORDER BY recorded_at DESC LIMIT ?",
        (limit,),
    ).fetchall()

    return [
        {
            "submitted_date": row["submitted_date"],
            "cancelled_date": row["cancelled_date"],
            "side": row["side"],
            "order_kind": row["order_kind"],
            "price": _to_float(Decimal(row["price"])) if row["price"] is not None else None,
            "qty": row["qty"],
            "purpose": row["purpose"],
            "is_decoy": bool(row["is_decoy"]),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# DRY_RUN 모의 계좌(shadow state) 조회 — dry_run_simulator.py가 진행시킨 값
# ---------------------------------------------------------------------------


def get_dry_run_status(conn: sqlite3.Connection, *, today: date) -> dict[str, Any] | None:
    """DRY_RUN 모의 계좌 현황 (실투자 전환 전 검증용, 설계도 범위 밖 이 프로젝트의 추가 기능).

    실제 `state`는 DRY_RUN 중 절대 전진하지 않으므로(주문이 진짜로 체결될 일이 없어
    T=0/holding_qty=0에 멈춰 있음), 이 함수는 그 대신 dry_run_simulator.py가 실제 시세로
    "체결됐을 것"이라 판정하며 진행시킨 모의 계좌(dry_run_state)를 보여줍니다. 아직
    한 번도 프리장/본장이 돌지 않아 dry_run_state가 없으면 None을 반환합니다 — 이 경우
    화면에서는 "아직 시뮬레이션 데이터 없음"으로 처리하면 됩니다.

    총평가금액/총수익률처럼 계좌 전체를 아우르는 값은 여기가 아니라
    get_dry_run_portfolio_summary()가 담당합니다(get_portfolio_summary()와
    get_current_cycle_status()가 분리된 것과 같은 구조).
    """
    try:
        s = dry_run_simulator.load_dry_run_state(conn)
    except StateError:
        return None

    return {
        "cycle_id": s.cycle_id,
        "mode": s.mode,
        "phase": s.phase,
        "start_date": s.cycle_start_date.isoformat(),
        "elapsed_days": (today - s.cycle_start_date).days,
        "t": _to_float(s.t),
        "split_count": s.split_count,
        "avg_price": _to_float(s.avg_price) if s.holding_qty > 0 else None,
        "holding_qty": s.holding_qty,
        "remaining_cash": _to_float(s.remaining_cash),
        "reverse_day_count": s.reverse_day_count if s.mode == MODE_REVERSE else None,
    }


def get_dry_run_portfolio_summary(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """DRY_RUN 모의 계좌의 총평가금액/총수익률 등 요약 (get_portfolio_summary()의 dry_run_* 버전).

    `dry_run_portfolio_summary`는 dry_run_simulator.run_dry_run_premarket()이 매 프리장
    실행 시점마다 "가장 최근 일봉 종가"를 현재가로 삼아 갱신합니다. 실제 계좌의
    portfolio_summary는 get_quote()의 근실시간 시세를 쓰지만, 모의 계좌는 실시간 시세가
    없으므로 **최대 하루 전 종가 기준 스냅샷**입니다 — 화면에도 이 사실을 표시합니다.

    아직 한 번도 프리장이 돌지 않아 행이 없으면 None을 반환합니다.
    """
    row = conn.execute("SELECT * FROM dry_run_portfolio_summary WHERE id = 1").fetchone()
    if row is None:
        return None
    return {
        "strategy_start_date": row["strategy_start_date"],
        "initial_principal": _to_float(Decimal(row["initial_principal"])),
        "total_realized_profit": _to_float(Decimal(row["total_realized_profit"])),
        "total_realized_return_pct": _to_float(Decimal(row["total_realized_return_pct"])),
        "current_unrealized_pnl": _to_float(Decimal(row["current_unrealized_pnl"])),
        "current_unrealized_return_pct": _to_float(Decimal(row["current_unrealized_return_pct"])),
        "total_equity": _to_float(Decimal(row["total_equity"])),
        "total_return_pct": _to_float(Decimal(row["total_return_pct"])),
        "completed_cycles": row["completed_cycles"],
        "last_updated": row["last_updated"],
    }


def get_dry_run_recent_trades(conn: sqlite3.Connection, *, limit: int = _RECENT_TRADES_LIMIT) -> list[dict[str, Any]]:
    """모의 계좌의 최근 매수/매도 기록 (get_recent_trades()의 dry_run_* 버전)."""
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT buy_date AS trade_date, 'BUY' AS side, buy_price AS price, buy_qty AS qty,
                   order_type, NULL AS profit_amount, NULL AS return_pct, created_at
            FROM dry_run_buy_records
            UNION ALL
            SELECT sell_date AS trade_date, 'SELL' AS side, sell_price AS price, sell_qty AS qty,
                   order_type, profit_amount, return_pct, created_at
            FROM dry_run_sell_records
        )
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    return [
        {
            "date": row["trade_date"],
            "side": row["side"],
            "price": _to_float(Decimal(row["price"])),
            "qty": row["qty"],
            "order_type": row["order_type"],
            "profit_amount": _to_float(Decimal(row["profit_amount"])) if row["profit_amount"] is not None else None,
            "return_pct": _to_float(Decimal(row["return_pct"])) if row["return_pct"] is not None else None,
        }
        for row in rows
    ]


def get_dry_run_cycle_history(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """모의 계좌의 완료된 사이클 히스토리 (get_cycle_history()의 dry_run_* 버전)."""
    rows = conn.execute(
        """
        SELECT cycle_id, start_date, end_date, cycle_return_pct, cycle_profit_amount, hit_reverse_mode
        FROM dry_run_cycle_summary
        WHERE end_date IS NOT NULL
        ORDER BY cycle_id ASC
        """
    ).fetchall()

    return [
        {
            "cycle_id": row["cycle_id"],
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "cycle_return_pct": _to_float(Decimal(row["cycle_return_pct"])) if row["cycle_return_pct"] is not None else None,
            "cycle_profit_amount": (
                _to_float(Decimal(row["cycle_profit_amount"])) if row["cycle_profit_amount"] is not None else None
            ),
            "hit_reverse_mode": bool(row["hit_reverse_mode"]),
        }
        for row in rows
    ]


def get_dry_run_pending_orders(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """아직 체결 판정 전인(=다음 프리장에서 실제 종가로 대조될) 모의 주문 목록.

    실제 `submitted_orders`와 달리 하루 이상 쌓여 있을 수 있습니다(예: 노트북이 며칠
    꺼져 있어 프리장이 며칠째 못 돈 경우) — 그래서 submitted_date도 함께 보여줍니다.
    """
    rows = conn.execute("SELECT * FROM dry_run_orders ORDER BY submitted_date, side, purpose").fetchall()
    return [
        {
            "submitted_date": row["submitted_date"],
            "side": row["side"],
            "order_kind": row["order_kind"],
            "price": _to_float(Decimal(row["price"])) if row["price"] is not None else None,
            "qty": row["qty"],
            "purpose": row["purpose"],
            "is_decoy": bool(row["is_decoy"]),
        }
        for row in rows
    ]


def build_dashboard_payload(conn: sqlite3.Connection, *, today: date, dry_run_enabled: bool = False) -> dict[str, Any]:
    """대시보드 프런트엔드가 한 번의 요청으로 받아가는 전체 JSON 페이로드를 조립합니다.

    server.py의 `/api/dashboard` 라우트가 이 함수 하나만 호출합니다 — 라우트 코드에는
    쿼리 로직이 전혀 없어야 합니다(HTTP 계층과 데이터 계층 분리).

    `dry_run_enabled`(=config.dry_run)가 True일 때만 "dry_run" 섹션을 채웁니다 —
    실투자로 전환한 뒤에도 예전 dry_run_* 테이블 데이터가 DB에 남아있을 수 있으므로,
    테이블 존재 여부가 아니라 현재 config를 기준으로 화면 노출 여부를 결정합니다.
    """
    return {
        "portfolio": get_portfolio_summary(conn),
        "cycle": get_current_cycle_status(conn, today=today),
        "recent_trades": get_recent_trades(conn),
        "cycle_history": get_cycle_history(conn),
        "today_orders": get_today_order_activity(conn, today=today),
        "cancelled_orders": get_recent_cancelled_orders(conn),
        "dry_run": (
            {
                "portfolio": get_dry_run_portfolio_summary(conn),
                "status": get_dry_run_status(conn, today=today),
                "recent_trades": get_dry_run_recent_trades(conn),
                "cycle_history": get_dry_run_cycle_history(conn),
                "pending_orders": get_dry_run_pending_orders(conn),
            }
            if dry_run_enabled
            else None
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
