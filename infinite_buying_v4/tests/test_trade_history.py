"""
test_trade_history.py
======================
trade_history.py의 매수/매도 기록, 사이클 요약 집계, 포트폴리오 누적 성과 계산을 검증합니다
(설계도 9번).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from infinite_buying_v4 import db, trade_history as th


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.get_connection(tmp_path / "test.db")
    yield connection
    connection.close()


def test_record_buy_computes_amount_and_stores_snapshot(conn) -> None:
    record_id = th.record_buy(
        conn,
        cycle_id=1,
        buy_date=date(2026, 8, 3),
        buy_price=Decimal("50.00"),
        buy_qty=10,
        order_type=th.BUY_TYPE_FIRST,
        t_after=Decimal("1"),
        avg_price_after=Decimal("50.00"),
    )
    row = conn.execute("SELECT * FROM buy_records WHERE id = ?", (record_id,)).fetchone()
    assert Decimal(row["buy_amount"]) == Decimal("500.00")
    assert row["order_type"] == th.BUY_TYPE_FIRST


def test_record_buy_rejects_non_positive_qty(conn) -> None:
    with pytest.raises(th.TradeHistoryError):
        th.record_buy(
            conn,
            cycle_id=1,
            buy_date=date(2026, 8, 3),
            buy_price=Decimal("50.00"),
            buy_qty=0,
            order_type=th.BUY_TYPE_FIRST,
            t_after=Decimal("1"),
            avg_price_after=Decimal("50.00"),
        )


def test_record_sell_computes_return_pct_and_profit(conn) -> None:
    record_id = th.record_sell(
        conn,
        cycle_id=1,
        sell_date=date(2026, 8, 10),
        sell_price=Decimal("60.00"),
        sell_qty=10,
        order_type=th.SELL_TYPE_QUARTER,
        avg_price_at_sell=Decimal("50.00"),
        t_after=Decimal("5"),
    )
    row = conn.execute("SELECT * FROM sell_records WHERE id = ?", (record_id,)).fetchone()
    assert Decimal(row["return_pct"]) == Decimal("20")
    assert Decimal(row["profit_amount"]) == Decimal("100.00")
    assert Decimal(row["sell_amount"]) == Decimal("600.00")


def test_cycle_summary_lifecycle_open_and_close(conn) -> None:
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

    row = conn.execute("SELECT * FROM cycle_summary WHERE cycle_id = 1").fetchone()
    assert row["end_date"] == "2026-08-20"
    assert Decimal(row["total_buy_amount"]) == Decimal("5000.00")
    assert Decimal(row["total_sell_amount"]) == Decimal("6000.00")
    assert Decimal(row["cycle_profit_amount"]) == Decimal("1000.00")
    assert Decimal(row["cycle_return_pct"]) == Decimal("20")
    assert row["duration_days"] == 17


def test_close_cycle_summary_raises_when_not_opened(conn) -> None:
    with pytest.raises(th.TradeHistoryError):
        th.close_cycle_summary(conn, cycle_id=99, end_date=date(2026, 8, 20))


def test_mark_cycle_hit_reverse_mode(conn) -> None:
    th.open_cycle_summary(conn, cycle_id=1, start_date=date(2026, 8, 3))
    th.mark_cycle_hit_reverse_mode(conn, cycle_id=1)
    row = conn.execute("SELECT hit_reverse_mode FROM cycle_summary WHERE cycle_id = 1").fetchone()
    assert row["hit_reverse_mode"] == 1


def test_portfolio_summary_ensure_is_idempotent(conn) -> None:
    th.ensure_portfolio_summary(conn, strategy_start_date=date(2026, 8, 3), initial_principal=Decimal("10000"))
    th.ensure_portfolio_summary(conn, strategy_start_date=date(2026, 8, 3), initial_principal=Decimal("10000"))
    count = conn.execute("SELECT COUNT(*) AS cnt FROM portfolio_summary").fetchone()["cnt"]
    assert count == 1


def test_update_portfolio_summary_computes_totals(conn) -> None:
    th.ensure_portfolio_summary(conn, strategy_start_date=date(2026, 8, 3), initial_principal=Decimal("10000"))

    # 완료된 사이클 하나를 미리 만들어 실현손익 1000이 잡히게 합니다.
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

    # 두 번째(진행 중) 사이클: 보유 50주, 평단 55, 현재가 58, 잔금 6000.
    th.update_portfolio_summary(
        conn,
        current_price=Decimal("58.00"),
        remaining_cash=Decimal("6000.00"),
        avg_price=Decimal("55.00"),
        holding_qty=50,
    )

    row = conn.execute("SELECT * FROM portfolio_summary WHERE id = 1").fetchone()
    assert Decimal(row["total_realized_profit"]) == Decimal("1000.00")
    assert Decimal(row["total_realized_return_pct"]) == Decimal("10")  # 1000/10000*100
    assert Decimal(row["current_unrealized_pnl"]) == Decimal("150.00")  # (58-55)*50
    assert Decimal(row["total_equity"]) == Decimal("8900.00")  # 6000 + 58*50
    assert row["completed_cycles"] == 1


def test_export_to_csv_creates_files_for_all_tables(conn, tmp_path: Path) -> None:
    th.ensure_portfolio_summary(conn, strategy_start_date=date(2026, 8, 3), initial_principal=Decimal("10000"))
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

    output_dir = tmp_path / "export"
    exported = th.export_to_csv(conn, output_dir)

    assert set(exported.keys()) == {"buy_records", "sell_records", "cycle_summary", "portfolio_summary"}
    for path in exported.values():
        assert path.exists()
    buy_csv_content = exported["buy_records"].read_text(encoding="utf-8-sig")
    assert "buy_price" in buy_csv_content
    assert "50.00" in buy_csv_content
