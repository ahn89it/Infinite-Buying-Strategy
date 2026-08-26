"""
test_dashboard_data.py
=======================
dashboard/data.py의 조회/조립 함수를 검증합니다 (설계도 12번).

Flask 없이(HTTP 서버를 띄우지 않고) SQLite 데이터만으로 검증합니다 — data.py가
프레젠테이션 계층(server.py)과 완전히 분리되어 있음을 그대로 보여주는 테스트입니다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from infinite_buying_v4 import db, trade_history as th
from infinite_buying_v4.dashboard.data import (
    build_dashboard_payload,
    get_current_cycle_status,
    get_cycle_history,
    get_portfolio_summary,
    get_recent_trades,
)
from infinite_buying_v4.state import (
    MODE_NORMAL,
    MODE_REVERSE,
    PHASE_FIRST_HALF,
    bootstrap_new_state,
    save_state,
)


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.get_connection(tmp_path / "test.db")
    yield connection
    connection.close()


def test_get_portfolio_summary_returns_none_before_bootstrap(conn) -> None:
    assert get_portfolio_summary(conn) is None


def test_get_portfolio_summary_returns_floats(conn) -> None:
    th.ensure_portfolio_summary(conn, strategy_start_date=date(2026, 8, 3), initial_principal=Decimal("10000"))
    th.update_portfolio_summary(
        conn,
        current_price=Decimal("55.00"),
        remaining_cash=Decimal("5000.00"),
        avg_price=Decimal("50.00"),
        holding_qty=100,
    )
    summary = get_portfolio_summary(conn)
    assert summary is not None
    assert summary["total_equity"] == pytest.approx(10500.00)
    assert summary["current_unrealized_pnl"] == pytest.approx(500.00)
    assert isinstance(summary["completed_cycles"], int)


def test_get_current_cycle_status_returns_none_before_bootstrap(conn) -> None:
    assert get_current_cycle_status(conn, today=date(2026, 8, 3)) is None


def test_get_current_cycle_status_first_phase_has_no_target_prices(conn) -> None:
    bootstrap_new_state(conn, split_count=40, principal=Decimal("10000"), start_date=date(2026, 8, 3))
    status = get_current_cycle_status(conn, today=date(2026, 8, 3))
    assert status["mode"] == MODE_NORMAL
    assert status["holding_qty"] == 0
    assert status["avg_price"] is None
    assert status["next_buy_price"] is None
    assert status["next_sell_price"] is None
    assert "첫매수" in status["note"]


def test_get_current_cycle_status_computes_target_prices_and_elapsed_days(conn) -> None:
    state = bootstrap_new_state(conn, split_count=40, principal=Decimal("10000"), start_date=date(2026, 8, 3))
    holding_state = state.__class__(
        **{**state.__dict__, "phase": PHASE_FIRST_HALF, "t": Decimal("5"), "avg_price": Decimal("50.00"), "holding_qty": 100}
    )
    save_state(conn, holding_state)

    status = get_current_cycle_status(conn, today=date(2026, 8, 10))
    assert status["elapsed_days"] == 7
    assert status["avg_price"] == pytest.approx(50.00)
    assert status["next_buy_price"] is not None
    assert status["next_sell_price"] is not None
    assert status["next_buy_price"] < status["next_sell_price"]  # 매수점 = 별지점 - 0.01


def test_get_current_cycle_status_derives_current_price_from_unrealized_pnl(conn) -> None:
    state = bootstrap_new_state(conn, split_count=40, principal=Decimal("10000"), start_date=date(2026, 8, 3))
    holding_state = state.__class__(
        **{**state.__dict__, "phase": PHASE_FIRST_HALF, "t": Decimal("5"), "avg_price": Decimal("50.00"), "holding_qty": 100}
    )
    save_state(conn, holding_state)

    th.ensure_portfolio_summary(conn, strategy_start_date=date(2026, 8, 3), initial_principal=Decimal("10000"))
    th.update_portfolio_summary(
        conn,
        current_price=Decimal("55.00"),
        remaining_cash=Decimal("5000.00"),
        avg_price=Decimal("50.00"),
        holding_qty=100,
    )

    status = get_current_cycle_status(conn, today=date(2026, 8, 10))
    # current_unrealized_pnl = (55-50)*100 = 500 -> 역산 현재가 = 50 + 500/100 = 55
    assert status["current_price"] == pytest.approx(55.00)


def test_get_current_cycle_status_reverse_mode_has_note_and_no_targets(conn) -> None:
    state = bootstrap_new_state(conn, split_count=40, principal=Decimal("10000"), start_date=date(2026, 8, 3))
    reverse_state = state.__class__(
        **{
            **state.__dict__,
            "mode": MODE_REVERSE,
            "phase": None,
            "t": Decimal("39"),
            "avg_price": Decimal("50.00"),
            "holding_qty": 50,
            "reverse_day_count": 2,
        }
    )
    save_state(conn, reverse_state)

    status = get_current_cycle_status(conn, today=date(2026, 8, 10))
    assert status["mode"] == MODE_REVERSE
    assert status["next_buy_price"] is None
    assert status["next_sell_price"] is None
    assert status["reverse_day_count"] == 2
    assert "리버스모드" in status["note"]


def test_get_recent_trades_merges_and_sorts_desc(conn) -> None:
    th.record_buy(
        conn,
        cycle_id=1,
        buy_date=date(2026, 8, 3),
        buy_price=Decimal("50.00"),
        buy_qty=10,
        order_type=th.BUY_TYPE_FIRST,
        t_after=Decimal("1"),
        avg_price_after=Decimal("50.00"),
    )
    th.record_sell(
        conn,
        cycle_id=1,
        sell_date=date(2026, 8, 4),
        sell_price=Decimal("60.00"),
        sell_qty=5,
        order_type=th.SELL_TYPE_QUARTER,
        avg_price_at_sell=Decimal("50.00"),
        t_after=Decimal("1"),
    )

    trades = get_recent_trades(conn, limit=20)
    assert len(trades) == 2
    assert trades[0]["side"] == "SELL"  # 나중에 기록된 것이 최신순으로 먼저
    assert trades[0]["return_pct"] == pytest.approx(20.0)
    assert trades[1]["side"] == "BUY"
    assert trades[1]["profit_amount"] is None


def test_get_recent_trades_respects_limit(conn) -> None:
    for i in range(5):
        th.record_buy(
            conn,
            cycle_id=1,
            buy_date=date(2026, 8, 3),
            buy_price=Decimal("50.00"),
            buy_qty=1,
            order_type=th.BUY_TYPE_FIRST,
            t_after=Decimal(str(i)),
            avg_price_after=Decimal("50.00"),
        )
    trades = get_recent_trades(conn, limit=3)
    assert len(trades) == 3


def test_get_cycle_history_excludes_open_cycles(conn) -> None:
    th.open_cycle_summary(conn, cycle_id=1, start_date=date(2026, 8, 3))
    th.record_buy(
        conn,
        cycle_id=1,
        buy_date=date(2026, 8, 3),
        buy_price=Decimal("50.00"),
        buy_qty=100,
        order_type=th.BUY_TYPE_FIRST,
        t_after=Decimal("1"),
        avg_price_after=Decimal("50.00"),
    )
    th.record_sell(
        conn,
        cycle_id=1,
        sell_date=date(2026, 8, 20),
        sell_price=Decimal("60.00"),
        sell_qty=100,
        order_type=th.SELL_TYPE_LIMIT_15PCT,
        avg_price_at_sell=Decimal("50.00"),
        t_after=Decimal("1"),
    )
    th.close_cycle_summary(conn, cycle_id=1, end_date=date(2026, 8, 20))
    th.open_cycle_summary(conn, cycle_id=2, start_date=date(2026, 8, 21))  # 아직 진행 중

    history = get_cycle_history(conn)
    assert len(history) == 1
    assert history[0]["cycle_id"] == 1
    assert history[0]["cycle_return_pct"] == pytest.approx(20.0)


def test_build_dashboard_payload_assembles_all_sections(conn) -> None:
    bootstrap_new_state(conn, split_count=40, principal=Decimal("10000"), start_date=date(2026, 8, 3))
    th.ensure_portfolio_summary(conn, strategy_start_date=date(2026, 8, 3), initial_principal=Decimal("10000"))

    payload = build_dashboard_payload(conn, today=date(2026, 8, 3))
    assert set(payload.keys()) == {"portfolio", "cycle", "recent_trades", "cycle_history", "generated_at"}
    assert payload["portfolio"] is not None
    assert payload["cycle"] is not None
    assert payload["recent_trades"] == []
    assert payload["cycle_history"] == []
