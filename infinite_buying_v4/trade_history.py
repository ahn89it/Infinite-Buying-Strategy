"""
trade_history.py
=================
사람이 조회/분석하는 매수·매도 이력, 사이클 요약, 전체 누적 성과를 SQLite에 기록하는
모듈입니다 (설계도 9번).

event_log.py(T값 재생용 내부 로그)와의 차이:
- event_log.py는 "프로그램이 스스로 T값을 다시 계산하기 위한" append-only 내부 로그입니다.
- trade_history.py는 "사람이 계좌 상태/수익률을 확인하기 위한" 조회용 기록입니다.
  체결이 실제로 확인된 시점에만 기록하며(설계도 9-3번), 미체결 주문은 절대 저장하지 않습니다.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from infinite_buying_v4.formulas import profit_amount as calc_profit_amount
from infinite_buying_v4.formulas import return_pct as calc_return_pct

# --- 매수 주문 유형 (설계도 9-2번 buy_records.order_type) ---
BUY_TYPE_FIRST = "FIRST"  # 첫매수
BUY_TYPE_HALF_STAR = "HALF_STAR"  # 전반전, 별지점 절반 매수
BUY_TYPE_HALF_AVG = "HALF_AVG"  # 전반전, 평단가 절반 매수
BUY_TYPE_FULL_STAR = "FULL_STAR"  # 후반전, 별지점 전액 매수
BUY_TYPE_REVERSE = "REVERSE"  # 리버스모드 관련 매수(체결 재개 등)

# --- 매도 주문 유형 (설계도 9-2번 sell_records.order_type) ---
SELL_TYPE_QUARTER = "QUARTER"  # 쿼터매도 (보유수량의 1/4, 별지점)
SELL_TYPE_LIMIT_15PCT = "LIMIT_15PCT"  # 지정가매도 (나머지 3/4, 평단+15%)
SELL_TYPE_REVERSE_MOC = "REVERSE_MOC"  # 리버스모드 D1 매도
SELL_TYPE_REVERSE_LOC = "REVERSE_LOC"  # 리버스모드 D2 이후 매도

_CSV_TABLES = ("buy_records", "sell_records", "cycle_summary", "portfolio_summary")


class TradeHistoryError(Exception):
    """거래 이력 기록/집계 중 데이터 정합성 문제가 발견됐을 때 발생시키는 예외."""


@dataclass(frozen=True)
class BuyRecord:
    id: int
    cycle_id: int
    buy_date: date
    buy_price: Decimal
    buy_qty: int
    buy_amount: Decimal
    order_type: str
    t_after: Decimal
    avg_price_after: Decimal


@dataclass(frozen=True)
class SellRecord:
    id: int
    cycle_id: int
    sell_date: date
    sell_price: Decimal
    sell_qty: int
    sell_amount: Decimal
    order_type: str
    avg_price_at_sell: Decimal
    return_pct: Decimal
    profit_amount: Decimal
    t_after: Decimal


def record_buy(
    conn: sqlite3.Connection,
    *,
    cycle_id: int,
    buy_date: date,
    buy_price: Decimal,
    buy_qty: int,
    order_type: str,
    t_after: Decimal,
    avg_price_after: Decimal,
) -> int:
    """체결이 확인된 매수 1건을 기록합니다. 반환값은 새로 생성된 buy_records.id.

    호출 시점 규칙(설계도 9-3번): 이 함수는 반드시 "실제 체결 확인 콜백"에서만
    호출해야 하며, 아직 체결되지 않은 주문 제출 시점에 호출하면 안 됩니다.
    """
    if buy_qty <= 0:
        raise TradeHistoryError(f"buy_qty는 1 이상이어야 합니다 (입력값: {buy_qty}).")
    buy_amount = buy_price * Decimal(buy_qty)
    cursor = conn.execute(
        """
        INSERT INTO buy_records (
            cycle_id, buy_date, buy_price, buy_qty, buy_amount, order_type,
            t_after, avg_price_after, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cycle_id,
            buy_date.isoformat(),
            str(buy_price),
            buy_qty,
            str(buy_amount),
            order_type,
            str(t_after),
            str(avg_price_after),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return int(cursor.lastrowid)


def record_sell(
    conn: sqlite3.Connection,
    *,
    cycle_id: int,
    sell_date: date,
    sell_price: Decimal,
    sell_qty: int,
    order_type: str,
    avg_price_at_sell: Decimal,
    t_after: Decimal,
) -> int:
    """체결이 확인된 매도 1건을 기록합니다. 수익률/수익금은 formulas.py 공식으로 계산합니다
    (설계도 9-2번: return_pct, profit_amount는 이 매도 건 자체의 avg_price_at_sell 기준).
    """
    if sell_qty <= 0:
        raise TradeHistoryError(f"sell_qty는 1 이상이어야 합니다 (입력값: {sell_qty}).")
    sell_amount = sell_price * Decimal(sell_qty)
    pct = calc_return_pct(sell_price, avg_price_at_sell)
    profit = calc_profit_amount(sell_price, avg_price_at_sell, sell_qty)

    cursor = conn.execute(
        """
        INSERT INTO sell_records (
            cycle_id, sell_date, sell_price, sell_qty, sell_amount, order_type,
            avg_price_at_sell, return_pct, profit_amount, t_after, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cycle_id,
            sell_date.isoformat(),
            str(sell_price),
            sell_qty,
            str(sell_amount),
            order_type,
            str(avg_price_at_sell),
            str(pct),
            str(profit),
            str(t_after),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return int(cursor.lastrowid)


def open_cycle_summary(
    conn: sqlite3.Connection, *, cycle_id: int, start_date: date, hit_reverse_mode: bool = False
) -> None:
    """새 사이클이 시작될 때 cycle_summary에 "진행 중" 행을 만듭니다 (end_date=NULL).

    scheduler.py가 state.bootstrap_new_state()/start_new_cycle()과 짝을 맞춰 호출해야 합니다.
    """
    conn.execute(
        """
        INSERT INTO cycle_summary (
            cycle_id, start_date, end_date, total_buy_amount, total_sell_amount,
            cycle_profit_amount, cycle_return_pct, hit_reverse_mode, duration_days
        ) VALUES (?, ?, NULL, '0', '0', NULL, NULL, ?, NULL)
        """,
        (cycle_id, start_date.isoformat(), int(hit_reverse_mode)),
    )


def mark_cycle_hit_reverse_mode(conn: sqlite3.Connection, *, cycle_id: int) -> None:
    """진행 중인 사이클이 리버스모드를 경유했음을 표시합니다 (설계도 9-2번 hit_reverse_mode)."""
    conn.execute(
        "UPDATE cycle_summary SET hit_reverse_mode = 1 WHERE cycle_id = ?",
        (cycle_id,),
    )


def close_cycle_summary(conn: sqlite3.Connection, *, cycle_id: int, end_date: date) -> None:
    """사이클 종료(보유수량 0) 시점에 buy_records/sell_records를 집계해 cycle_summary를 확정합니다
    (설계도 9-2, 9-3번: "건별 수익률과 사이클 전체 수익률이 다를 수 있음"에 따라 여기서 별도로
    한 번 더 집계).
    """
    row = conn.execute(
        "SELECT start_date FROM cycle_summary WHERE cycle_id = ?", (cycle_id,)
    ).fetchone()
    if row is None:
        raise TradeHistoryError(
            f"cycle_id={cycle_id}에 대한 cycle_summary 행이 없습니다. open_cycle_summary()를 "
            f"먼저 호출했는지 확인하세요."
        )
    start_date = date.fromisoformat(row["start_date"])

    # SQLite의 SUM()은 값을 float로 취급해 Decimal 정밀도가 깨지므로, 파이썬에서 직접 합산합니다.
    total_buy = _sum_decimal_column(conn, "buy_records", "buy_amount", cycle_id)
    total_sell = _sum_decimal_column(conn, "sell_records", "sell_amount", cycle_id)

    profit = total_sell - total_buy
    return_pct = (profit / total_buy * Decimal(100)) if total_buy > 0 else Decimal(0)
    duration_days = (end_date - start_date).days

    conn.execute(
        """
        UPDATE cycle_summary
        SET end_date = ?, total_buy_amount = ?, total_sell_amount = ?,
            cycle_profit_amount = ?, cycle_return_pct = ?, duration_days = ?
        WHERE cycle_id = ?
        """,
        (
            end_date.isoformat(),
            str(total_buy),
            str(total_sell),
            str(profit),
            str(return_pct),
            duration_days,
            cycle_id,
        ),
    )


def _sum_decimal_column(conn: sqlite3.Connection, table: str, column: str, cycle_id: int) -> Decimal:
    """TEXT로 저장된 Decimal 컬럼을 SQL SUM이 아닌 파이썬에서 직접 합산하는 헬퍼.

    SQLite의 SUM()은 값을 float로 취급하므로, Decimal 정밀도를 지키려면 각 행을 읽어
    파이썬에서 Decimal끼리 더해야 합니다.
    """
    rows = conn.execute(f"SELECT {column} FROM {table} WHERE cycle_id = ?", (cycle_id,)).fetchall()  # noqa: S608
    total = Decimal(0)
    for r in rows:
        total += Decimal(r[column])
    return total


def ensure_portfolio_summary(
    conn: sqlite3.Connection, *, strategy_start_date: date, initial_principal: Decimal
) -> None:
    """portfolio_summary(설계도 9-4번) 싱글턴 행을 최초 1회 생성합니다.

    이미 존재하면 아무 것도 하지 않습니다(멱등). strategy_start_date/initial_principal은
    전략을 처음 시작한 시점에 딱 한 번 고정되는 값이므로, 이후 update_portfolio_summary()는
    이 값들을 다시 받지 않고 DB에 저장된 값을 그대로 재사용합니다.
    """
    row = conn.execute("SELECT 1 FROM portfolio_summary WHERE id = 1").fetchone()
    if row is not None:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO portfolio_summary (
            id, strategy_start_date, initial_principal, total_realized_profit,
            total_realized_return_pct, current_unrealized_pnl, current_unrealized_return_pct,
            total_equity, total_return_pct, completed_cycles, last_updated
        ) VALUES (1, ?, ?, '0', '0', '0', '0', ?, '0', 0, ?)
        """,
        (strategy_start_date.isoformat(), str(initial_principal), str(initial_principal), now_iso),
    )


def update_portfolio_summary(
    conn: sqlite3.Connection,
    *,
    current_price: Decimal,
    remaining_cash: Decimal,
    avg_price: Decimal,
    holding_qty: int,
) -> None:
    """계좌 전체 누적 성과를 재계산합니다 (설계도 9-4번).

    매수/매도 체결이 있을 때마다, 그리고 매일 장 마감 후 시세 갱신 시점에 호출해야 합니다.
    ensure_portfolio_summary()가 먼저 호출되어 행이 존재해야 합니다.
    """
    row = conn.execute("SELECT * FROM portfolio_summary WHERE id = 1").fetchone()
    if row is None:
        raise TradeHistoryError("portfolio_summary 행이 없습니다. ensure_portfolio_summary()를 먼저 호출하세요.")

    initial_principal = Decimal(row["initial_principal"])

    completed_row = conn.execute(
        "SELECT cycle_profit_amount FROM cycle_summary WHERE end_date IS NOT NULL"
    ).fetchall()
    total_realized_profit = sum((Decimal(r["cycle_profit_amount"]) for r in completed_row), Decimal(0))
    total_realized_return_pct = (
        (total_realized_profit / initial_principal * Decimal(100)) if initial_principal > 0 else Decimal(0)
    )

    current_unrealized_pnl = (current_price - avg_price) * Decimal(holding_qty)
    current_unrealized_return_pct = (
        ((current_price - avg_price) / avg_price * Decimal(100)) if avg_price > 0 else Decimal(0)
    )

    total_equity = remaining_cash + current_price * Decimal(holding_qty)
    total_return_pct = (
        ((total_equity - initial_principal) / initial_principal * Decimal(100))
        if initial_principal > 0
        else Decimal(0)
    )

    completed_cycles = conn.execute(
        "SELECT COUNT(*) AS cnt FROM cycle_summary WHERE end_date IS NOT NULL"
    ).fetchone()["cnt"]

    conn.execute(
        """
        UPDATE portfolio_summary
        SET total_realized_profit = ?, total_realized_return_pct = ?,
            current_unrealized_pnl = ?, current_unrealized_return_pct = ?,
            total_equity = ?, total_return_pct = ?, completed_cycles = ?, last_updated = ?
        WHERE id = 1
        """,
        (
            str(total_realized_profit),
            str(total_realized_return_pct),
            str(current_unrealized_pnl),
            str(current_unrealized_return_pct),
            str(total_equity),
            str(total_return_pct),
            completed_cycles,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def export_to_csv(conn: sqlite3.Connection, output_dir: Path) -> dict[str, Path]:
    """buy_records/sell_records/cycle_summary/portfolio_summary를 각각 CSV로 내보냅니다
    (설계도 9-3번: "엑셀에서 바로 열람 가능하도록"). 반환값은 {테이블명: 저장경로}.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    exported: dict[str, Path] = {}
    for table in _CSV_TABLES:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 (테이블명은 내부 상수 목록에서만 옴)
        output_path = output_dir / f"{table}.csv"
        with output_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            if rows:
                writer.writerow(rows[0].keys())
                for r in rows:
                    writer.writerow(tuple(r))
            else:
                # 데이터가 없어도 컬럼 헤더는 알 수 있도록 스키마에서 컬럼명만 가져와 씁니다.
                columns = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]  # noqa: S608
                writer.writerow(columns)
        exported[table] = output_path
    return exported
