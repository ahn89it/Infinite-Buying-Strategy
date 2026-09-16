"""
test_dry_run_simulator.py
===========================
dry_run_simulator.py의 체결 시뮬레이션 판정, 모의 상태(shadow state) 진행,
사이클 종료/리버스모드 전이를 검증합니다.

kiwoom_adapter.get_recent_daily_ohlc()는 실제 API가 필요하므로 이 테스트에서는
직접 호출하지 않고, run_dry_run_premarket()을 거치지 않은 채 내부 함수
(_settle_day 등)를 직접 호출해 검증합니다 — simulate_order_fill 자체는 순수함수라
네트워크 없이 전 구간 검증 가능합니다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from infinite_buying_v4 import db, dry_run_simulator as sim
from infinite_buying_v4.config import Config
from infinite_buying_v4.kiwoom_adapter import DailyOHLC
from infinite_buying_v4.state import MODE_NORMAL, MODE_REVERSE, PHASE_FIRST, load_state
from infinite_buying_v4.trade_history import (
    BUY_TYPE_FIRST,
    BUY_TYPE_HALF_AVG,
    BUY_TYPE_HALF_STAR,
    SELL_TYPE_LIMIT_15PCT,
    SELL_TYPE_QUARTER,
)


def _ohlc(d: date, *, o: str, h: str, l: str, c: str) -> DailyOHLC:  # noqa: E741
    return DailyOHLC(trade_date=d, open=Decimal(o), high=Decimal(h), low=Decimal(l), close=Decimal(c))


def _order(*, id_=1, submitted_date=date(2026, 8, 3), side="BUY", order_kind="LOC", price="50.00", qty=10, purpose=BUY_TYPE_FIRST, is_decoy=False):
    return sim.SimulatedOrder(
        id=id_,
        submitted_date=submitted_date,
        side=side,
        order_kind=order_kind,
        price=Decimal(price) if price is not None else None,
        qty=qty,
        purpose=purpose,
        is_decoy=is_decoy,
    )


# ---------------------------------------------------------------------------
# simulate_order_fill (순수함수)
# ---------------------------------------------------------------------------


def test_moc_always_fills_at_close() -> None:
    order = _order(order_kind="MOC", side="SELL", price=None, qty=5)
    ohlc = _ohlc(date(2026, 8, 3), o="50", h="55", l="48", c="52.34")
    fill = sim.simulate_order_fill(order, ohlc)
    assert fill is not None
    assert fill.fill_price == Decimal("52.34")
    assert fill.fill_qty == 5


def test_loc_buy_fills_when_close_at_or_below_price() -> None:
    order = _order(order_kind="LOC", side="BUY", price="50.00")
    ohlc_fills = _ohlc(date(2026, 8, 3), o="49", h="51", l="48", c="49.50")
    ohlc_no_fill = _ohlc(date(2026, 8, 3), o="49", h="51", l="48", c="50.01")

    assert sim.simulate_order_fill(order, ohlc_fills).fill_price == Decimal("49.50")
    assert sim.simulate_order_fill(order, ohlc_no_fill) is None


def test_loc_sell_fills_when_close_at_or_above_price() -> None:
    order = _order(order_kind="LOC", side="SELL", price="50.00")
    ohlc_fills = _ohlc(date(2026, 8, 3), o="49", h="51", l="48", c="50.50")
    ohlc_no_fill = _ohlc(date(2026, 8, 3), o="49", h="51", l="48", c="49.99")

    assert sim.simulate_order_fill(order, ohlc_fills).fill_price == Decimal("50.50")
    assert sim.simulate_order_fill(order, ohlc_no_fill) is None


def test_loc_boundary_exact_price_fills() -> None:
    """경계값: 종가가 지정가와 정확히 같으면 체결(매수/매도 둘 다 <=/>= 이므로 포함)."""
    buy = _order(order_kind="LOC", side="BUY", price="50.00")
    sell = _order(order_kind="LOC", side="SELL", price="50.00")
    ohlc = _ohlc(date(2026, 8, 3), o="50", h="50", l="50", c="50.00")
    assert sim.simulate_order_fill(buy, ohlc) is not None
    assert sim.simulate_order_fill(sell, ohlc) is not None


def test_limit_buy_fills_when_low_touches_price() -> None:
    order = _order(order_kind="LIMIT", side="BUY", price="50.00")
    ohlc_touch = _ohlc(date(2026, 8, 3), o="52", h="53", l="49.50", c="52.00")
    ohlc_no_touch = _ohlc(date(2026, 8, 3), o="52", h="53", l="50.50", c="52.00")

    fill = sim.simulate_order_fill(order, ohlc_touch)
    assert fill is not None
    assert fill.fill_price == Decimal("50.00")  # LIMIT은 지정가 자체로 체결 근사
    assert sim.simulate_order_fill(order, ohlc_no_touch) is None


def test_limit_sell_fills_when_high_touches_price() -> None:
    order = _order(order_kind="LIMIT", side="SELL", price="57.50")
    ohlc_touch = _ohlc(date(2026, 8, 3), o="55", h="58.00", l="54", c="56.00")
    ohlc_no_touch = _ohlc(date(2026, 8, 3), o="55", h="57.00", l="54", c="56.00")

    fill = sim.simulate_order_fill(order, ohlc_touch)
    assert fill is not None
    assert fill.fill_price == Decimal("57.50")
    assert sim.simulate_order_fill(order, ohlc_no_touch) is None


def test_loc_or_limit_without_price_raises() -> None:
    order = _order(order_kind="LOC", price=None)
    with pytest.raises(sim.DryRunSimulatorError):
        sim.simulate_order_fill(order, _ohlc(date(2026, 8, 3), o="1", h="1", l="1", c="1"))


def test_unknown_order_kind_raises() -> None:
    order = _order(order_kind="BOGUS")
    with pytest.raises(sim.DryRunSimulatorError):
        sim.simulate_order_fill(order, _ohlc(date(2026, 8, 3), o="1", h="1", l="1", c="1"))


def test_decoy_order_uses_normal_loc_rule() -> None:
    """미끼주문도 판정 규칙 자체는 일반 LOC와 동일합니다(체결 자체가 이상 신호일 뿐)."""
    decoy = _order(order_kind="LOC", side="BUY", price="56.00", is_decoy=True)
    ohlc_surge = _ohlc(date(2026, 8, 3), o="50", h="57", l="49", c="56.50")  # 급등해서 미끼가 안 먹힘(매수는 close<=price라야 체결)
    ohlc_normal = _ohlc(date(2026, 8, 3), o="50", h="51", l="49", c="50.10")
    assert sim.simulate_order_fill(decoy, ohlc_surge) is None
    assert sim.simulate_order_fill(decoy, ohlc_normal) is not None  # 종가(50.10) <= 56.00 이므로 체결


# ---------------------------------------------------------------------------
# dry_run_orders 장부 CRUD
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.get_connection(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        app_key="k", app_secret="s", api_base_url="https://api.kiwoom.com", ws_base_url="wss://api.kiwoom.com:10000",
        ticker="TQQQ", exchange_code="ND", split_count=40, principal=Decimal("500"), compound_on_restart=True,
        db_path=tmp_path / "test.db", event_log_path=tmp_path / "e.jsonl", dashboard_port=8000, dry_run=True,
    )


def test_record_and_load_and_delete_orders_for_date(conn) -> None:
    from infinite_buying_v4.orders import OrderIntent

    order = OrderIntent(side="BUY", order_kind="LOC", price=Decimal("50.00"), qty=10, purpose=BUY_TYPE_FIRST)
    sim.record_dry_run_order(conn, order, date(2026, 8, 3))

    loaded = sim._load_orders_for_date(conn, date(2026, 8, 3))
    assert len(loaded) == 1
    assert loaded[0].price == Decimal("50.00")
    assert loaded[0].qty == 10

    assert sim._load_orders_for_date(conn, date(2026, 8, 4)) == []

    sim._delete_orders_for_date(conn, date(2026, 8, 3))
    assert sim._load_orders_for_date(conn, date(2026, 8, 3)) == []


# ---------------------------------------------------------------------------
# ensure_dry_run_state
# ---------------------------------------------------------------------------


def test_ensure_dry_run_state_creates_new_when_missing(conn, config: Config) -> None:
    state = sim.ensure_dry_run_state(conn, config, start_date=date(2026, 8, 3))
    assert state.mode == MODE_NORMAL
    assert state.phase == PHASE_FIRST
    assert state.t == Decimal(0)
    assert state.principal == config.principal

    row = conn.execute("SELECT * FROM dry_run_cycle_summary WHERE cycle_id = 1").fetchone()
    assert row is not None


def test_ensure_dry_run_state_is_idempotent(conn, config: Config) -> None:
    first = sim.ensure_dry_run_state(conn, config, start_date=date(2026, 8, 3))
    second = sim.ensure_dry_run_state(conn, config, start_date=date(2026, 8, 4))
    assert first == second  # 두 번째 호출은 새로 만들지 않고 기존 걸 그대로 반환


# ---------------------------------------------------------------------------
# _settle_day (핵심 통합 시나리오)
# ---------------------------------------------------------------------------


def test_settle_day_full_fill_updates_state_and_records_buy(conn, config: Config) -> None:
    sim.ensure_dry_run_state(conn, config, start_date=date(2026, 8, 3))
    state = load_state(conn, table="dry_run_state")

    conn.execute(
        "INSERT INTO dry_run_orders (submitted_date, side, order_kind, price, qty, purpose, is_decoy) VALUES (?,?,?,?,?,?,?)",
        ("2026-08-03", "BUY", "LOC", "49.00", 10, BUY_TYPE_FIRST, 0),
    )
    ohlc = _ohlc(date(2026, 8, 3), o="49", h="50", l="48", c="48.50")  # close(48.50) <= 49.00 -> 체결

    new_state = sim._settle_day(conn, config, state, date(2026, 8, 3), ohlc)

    assert new_state.holding_qty == 10
    assert new_state.avg_price == Decimal("48.50")
    assert new_state.t == Decimal(1)  # 전액 체결 -> FULL_BUY
    assert new_state.remaining_cash == config.principal - Decimal("48.50") * 10

    buy_row = conn.execute("SELECT * FROM dry_run_buy_records").fetchone()
    assert buy_row is not None
    assert Decimal(buy_row["buy_price"]) == Decimal("48.50")

    # 정산된 주문은 정리되어야 합니다.
    assert sim._load_orders_for_date(conn, date(2026, 8, 3)) == []


def test_settle_day_no_fill_leaves_state_unchanged(conn, config: Config) -> None:
    sim.ensure_dry_run_state(conn, config, start_date=date(2026, 8, 3))
    state = load_state(conn, table="dry_run_state")

    conn.execute(
        "INSERT INTO dry_run_orders (submitted_date, side, order_kind, price, qty, purpose, is_decoy) VALUES (?,?,?,?,?,?,?)",
        ("2026-08-03", "BUY", "LOC", "40.00", 10, BUY_TYPE_FIRST, 0),
    )
    ohlc = _ohlc(date(2026, 8, 3), o="49", h="50", l="48", c="48.50")  # close(48.50) > 40.00 -> 미체결

    new_state = sim._settle_day(conn, config, state, date(2026, 8, 3), ohlc)

    assert new_state.holding_qty == 0
    assert new_state.t == Decimal(0)
    assert conn.execute("SELECT COUNT(*) AS c FROM dry_run_buy_records").fetchone()["c"] == 0


def test_settle_day_half_fill_of_two_sibling_orders(conn, config: Config) -> None:
    """전반전 절반매수 두 건 중 한쪽만 체결되면 HALF_BUY(T+=0.5), 나머지는 미체결로 남지
    않고(그날 정산이 끝나면 dry_run_orders에서 정리됨) 다음날 다시 새로 생성됩니다."""
    sim.ensure_dry_run_state(conn, config, start_date=date(2026, 8, 3))
    state = load_state(conn, table="dry_run_state")
    from infinite_buying_v4.state import save_state

    state = state.__class__(**{**state.__dict__, "avg_price": Decimal("50.00"), "holding_qty": 100, "t": Decimal("5")})
    save_state(conn, state, table="dry_run_state")

    # HALF_AVG(45.00)는 종가 44에 체결(44<=45), HALF_STAR(42.00)는 미체결(44>42).
    conn.execute(
        "INSERT INTO dry_run_orders (submitted_date, side, order_kind, price, qty, purpose, is_decoy) VALUES (?,?,?,?,?,?,?)",
        ("2026-08-03", "BUY", "LOC", "45.00", 10, BUY_TYPE_HALF_AVG, 0),
    )
    conn.execute(
        "INSERT INTO dry_run_orders (submitted_date, side, order_kind, price, qty, purpose, is_decoy) VALUES (?,?,?,?,?,?,?)",
        ("2026-08-03", "BUY", "LOC", "42.00", 10, BUY_TYPE_HALF_STAR, 0),
    )
    ohlc = _ohlc(date(2026, 8, 3), o="50", h="51", l="43", c="44.00")

    new_state = sim._settle_day(conn, config, state, date(2026, 8, 3), ohlc)

    assert new_state.holding_qty == 110  # HALF_AVG(10주)만 체결됨
    assert new_state.t == Decimal("5.5")  # 형제 주문 미체결분이 반영된 HALF_BUY (T += 0.5)
    assert conn.execute("SELECT COUNT(*) AS c FROM dry_run_buy_records").fetchone()["c"] == 1
    # 정산 후 그날 주문은 체결 여부와 무관하게 전부 정리됩니다(다음날 새로 생성됨).
    assert sim._load_orders_for_date(conn, date(2026, 8, 3)) == []


def test_settle_day_quarter_and_limit_sell_close_cycle(conn, config: Config) -> None:
    sim.ensure_dry_run_state(conn, config, start_date=date(2026, 8, 3))
    state = load_state(conn, table="dry_run_state")
    from infinite_buying_v4.state import save_state

    state = state.__class__(**{**state.__dict__, "avg_price": Decimal("50.00"), "holding_qty": 4, "t": Decimal("5")})
    save_state(conn, state, table="dry_run_state")

    conn.execute(
        "INSERT INTO dry_run_orders (submitted_date, side, order_kind, price, qty, purpose, is_decoy) VALUES (?,?,?,?,?,?,?)",
        ("2026-08-03", "SELL", "LOC", "55.00", 1, SELL_TYPE_QUARTER, 0),
    )
    conn.execute(
        "INSERT INTO dry_run_orders (submitted_date, side, order_kind, price, qty, purpose, is_decoy) VALUES (?,?,?,?,?,?,?)",
        ("2026-08-03", "SELL", "LIMIT", "57.50", 3, SELL_TYPE_LIMIT_15PCT, 0),
    )
    # 종가 60 (QUARTER는 LOC: close>=55 체결), 고가 58(LIMIT: high>=57.50 체결)
    ohlc = _ohlc(date(2026, 8, 3), o="56", h="58.00", l="55", c="60.00")

    new_state = sim._settle_day(conn, config, state, date(2026, 8, 3), ohlc)

    assert new_state.holding_qty == 0
    assert new_state.avg_price == Decimal(0)
    assert new_state.cycle_id == 2  # 사이클 종료 후 새 사이클 시작

    closed = conn.execute("SELECT * FROM dry_run_cycle_summary WHERE cycle_id = 1").fetchone()
    assert closed["end_date"] == "2026-08-03"
    assert Decimal(closed["cycle_profit_amount"]) > 0


def test_settle_day_enters_reverse_mode_when_t_crosses_threshold(conn, config: Config) -> None:
    sim.ensure_dry_run_state(conn, config, start_date=date(2026, 8, 3))
    state = load_state(conn, table="dry_run_state")
    from infinite_buying_v4.state import save_state

    state = state.__class__(
        **{**state.__dict__, "avg_price": Decimal("50.00"), "holding_qty": 100, "t": Decimal("39"), "phase": "SECOND_HALF"}
    )
    save_state(conn, state, table="dry_run_state")

    conn.execute(
        "INSERT INTO dry_run_orders (submitted_date, side, order_kind, price, qty, purpose, is_decoy) VALUES (?,?,?,?,?,?,?)",
        ("2026-08-03", "BUY", "LOC", "45.00", 10, "FULL_STAR", 0),
    )
    ohlc = _ohlc(date(2026, 8, 3), o="45", h="46", l="44", c="45.00")  # 체결 -> T=40 -> 리버스 트리거(>39)

    new_state = sim._settle_day(conn, config, state, date(2026, 8, 3), ohlc)

    assert new_state.mode == MODE_REVERSE
    assert new_state.phase is None
    assert new_state.reverse_prev_qty == 110


# ---------------------------------------------------------------------------
# run_dry_run_premarket() — 모의 계좌 요약(dry_run_portfolio_summary) 갱신
# ---------------------------------------------------------------------------


def test_run_dry_run_premarket_refreshes_portfolio_summary_using_latest_close(conn, config: Config) -> None:
    """대시보드 "DRY-RUN 모의 계좌" 요약 카드가 읽는 dry_run_portfolio_summary가,
    run_dry_run_premarket() 한 번 호출로 정산 결과를 반영해 갱신되는지 검증합니다.
    현재가로는 조회된 일봉 중 가장 최신(마지막) 종가를 씁니다."""
    from infinite_buying_v4.dashboard.data import get_dry_run_portfolio_summary

    sim.ensure_dry_run_state(conn, config, start_date=date(2026, 8, 3))
    conn.execute(
        "INSERT INTO dry_run_orders (submitted_date, side, order_kind, price, qty, purpose, is_decoy) VALUES (?,?,?,?,?,?,?)",
        ("2026-08-03", "BUY", "LOC", "50.00", 10, BUY_TYPE_FIRST, 0),
    )
    # 8/3 종가 49(매수 체결가), 8/4가 가장 최신 바 -> 미실현손익 계산의 현재가로 51이 쓰여야 함
    fake_ohlc = [
        _ohlc(date(2026, 8, 3), o="49.50", h="50.50", l="48.50", c="49.00"),
        _ohlc(date(2026, 8, 4), o="49.50", h="51.50", l="49.00", c="51.00"),
    ]

    with patch("infinite_buying_v4.dry_run_simulator.get_recent_daily_ohlc", return_value=fake_ohlc):
        sim.run_dry_run_premarket(conn, config, date(2026, 8, 4))

    summary = get_dry_run_portfolio_summary(conn)
    assert summary is not None
    # 10주를 49.00에 매수, 현재가(가장 최신 종가) 51.00 -> 미실현손익 = (51-49)*10 = 20
    assert summary["current_unrealized_pnl"] == 20.0
    principal = float(config.principal)
    assert summary["total_equity"] == principal - 490.0 + 51.0 * 10  # 잔금 + 평가금액
