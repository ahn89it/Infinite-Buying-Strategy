"""
test_scheduler.py
==================
scheduler.py의 멱등성 가드(_claim_daily_run), 드라이런 배선(_submit_or_dry_run),
체결 -> T값 이벤트 분류(_classify_daily_events, 특히 부분체결 판정)를 검증합니다.

kiwoom_adapter.py가 공식 `kiwoom` 패키지(PyPI `kwcli`)를 필요로 하므로, 이 테스트
파일은 그 패키지가 설치되어 있어야 수집/실행됩니다(requirements-dev.txt에 포함).
실제 네트워크 호출은 전부 unittest.mock으로 대체합니다 — 진짜 키움 서버에 접근하지
않습니다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from infinite_buying_v4 import db, scheduler
from infinite_buying_v4.config import Config
from infinite_buying_v4.event_log import (
    EVENT_FULL_BUY,
    EVENT_HALF_BUY,
    EVENT_LIMIT_SELL_THEN_LOC_BUY_FULL,
    EVENT_QUARTER_SELL,
)
from infinite_buying_v4.kiwoom_adapter import FillRecord, KiwoomAdapterError, SubmittedOrder
from infinite_buying_v4.notifier import NotifierBase, NotifyLevel
from infinite_buying_v4.orders import OrderIntent
from infinite_buying_v4.trade_history import (
    BUY_TYPE_FIRST,
    BUY_TYPE_HALF_AVG,
    BUY_TYPE_HALF_STAR,
    SELL_TYPE_LIMIT_15PCT,
    SELL_TYPE_QUARTER,
)


class _RecordingNotifier(NotifierBase):
    """실제 채널로 보내지 않고, 어떤 알림이 몇 번 왔는지만 기록하는 테스트용 더블."""

    def __init__(self) -> None:
        self.calls: list[tuple[NotifyLevel, str, str]] = []

    def notify(self, level: NotifyLevel, title: str, message: str) -> None:
        self.calls.append((level, title, message))

    def calls_at(self, level: NotifyLevel) -> list[tuple[str, str]]:
        return [(t, m) for lv, t, m in self.calls if lv == level]


def _make_config(*, dry_run: bool = False, db_path: Path, event_log_path: Path) -> Config:
    """테스트용 최소 Config. load_config()의 .env 의존을 피하기 위해 직접 생성합니다."""
    return Config(
        app_key="test-key",
        app_secret="test-secret",
        api_base_url="https://api.kiwoom.com",
        ws_base_url="wss://api.kiwoom.com:10000",
        ticker="TQQQ",
        exchange_code="ND",
        split_count=40,
        principal=Decimal("10000"),
        compound_on_restart=True,
        db_path=db_path,
        event_log_path=event_log_path,
        dashboard_port=8000,
        dry_run=dry_run,
    )


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.get_connection(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return _make_config(db_path=tmp_path / "test.db", event_log_path=tmp_path / "event_log.jsonl")


# ---------------------------------------------------------------------------
# _run_guarded (스레드 간 SQLite 커넥션 안전성 - 실제 운영 버그 재현/회귀 테스트)
# ---------------------------------------------------------------------------


def test_run_guarded_opens_its_own_connection_in_the_calling_thread(config: Config) -> None:
    """실제 재현된 버그: APScheduler의 BlockingScheduler는 등록한 작업을 스케줄러를
    시작한 스레드가 아니라 내부 스레드풀의 별도 작업자 스레드에서 실행합니다. 예전
    구현은 start_scheduler()가 커넥션을 한 번만 만들어 클로저로 넘겼는데, 그 커넥션을
    만든 스레드와 실제로 쿼리를 실행하는 스레드가 달라서 매 실행마다
    `sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in
    that same thread`로 실패했습니다(2026-09-11부터 실제 운영 로그에서 확인됨 —
    프리장/본장 작업이 단 한 번도 성공하지 못하고 매번 스케줄러가 종료됐음).

    이 테스트는 _run_guarded를 메인 스레드가 아닌 별도 스레드에서 호출해, 그 안에서
    SQLite 쿼리가 예외 없이 실행되는지 검증합니다 — _run_guarded가 항상 "자신을 호출한
    스레드 안에서" 새 커넥션을 여는 한(그리고 그 커넥션을 다른 스레드로 넘기지 않는 한)
    이 테스트는 어떤 스레드에서 호출돼도 통과해야 합니다.
    """
    import threading

    # 스키마가 미리 존재해야 하므로 한 번 열어서 만들어둡니다(이 커넥션은 메인 스레드
    # 것이라 바로 닫고, _run_guarded가 워커 스레드 안에서 별도로 새로 엽니다).
    db.get_connection(config.db_path).close()

    executed_thread_ids: list[int] = []
    errors: list[BaseException] = []

    def fake_fn(cfg: Config, conn, notifier: NotifierBase) -> None:
        # conn이 "지금 이 스레드"에서 실제로 동작하는지 쿼리를 날려 확인합니다.
        conn.execute("SELECT 1").fetchone()
        executed_thread_ids.append(threading.get_ident())

    def worker() -> None:
        try:
            scheduler._run_guarded("PREMARKET", fake_fn, config, _RecordingNotifier())
        except BaseException as exc:  # noqa: BLE001 - 테스트에서 스레드 예외를 회수하기 위함
            errors.append(exc)

    main_thread_id = threading.get_ident()
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive(), "워커 스레드가 제한 시간 내에 끝나지 않았습니다."
    assert errors == [], f"워커 스레드에서 예외 발생: {errors}"
    assert executed_thread_ids == [thread.ident]
    assert executed_thread_ids[0] != main_thread_id  # 실제로 다른 스레드에서 실행됐는지 확인


def test_run_guarded_closes_connection_even_on_failure(config: Config) -> None:
    """fn이 예외를 던져도 _run_guarded가 연 커넥션은 finally에서 닫혀야 합니다
    (열어둔 채로 예외만 다시 던지면 커넥션이 누수됩니다)."""

    def failing_fn(cfg: Config, conn, notifier: NotifierBase) -> None:
        raise RuntimeError("의도적 실패")

    with pytest.raises(RuntimeError):
        scheduler._run_guarded("PREMARKET", failing_fn, config, _RecordingNotifier())

    # 커넥션이 제대로 닫혔다면, 같은 파일로 새 커넥션을 여는 데 문제가 없어야 합니다.
    conn = db.get_connection(config.db_path)
    conn.execute("SELECT 1").fetchone()
    conn.close()


# ---------------------------------------------------------------------------
# _claim_daily_run (멱등성)
# ---------------------------------------------------------------------------


def test_claim_daily_run_first_call_succeeds(conn) -> None:
    assert scheduler._claim_daily_run(conn, "PREMARKET", date(2026, 8, 3)) is True


def test_claim_daily_run_second_call_same_day_fails(conn) -> None:
    """같은 (run_type, run_date)로 두 번째 선점을 시도하면 실패해야 합니다 —
    이것이 컨테이너 재시작 시 중복 주문 제출을 막는 핵심 방어입니다."""
    assert scheduler._claim_daily_run(conn, "PREMARKET", date(2026, 8, 3)) is True
    assert scheduler._claim_daily_run(conn, "PREMARKET", date(2026, 8, 3)) is False


def test_claim_daily_run_different_run_type_is_independent(conn) -> None:
    """PREMARKET과 REGULAR는 판정 키의 일부이므로, 같은 날짜라도 서로 독립적으로 선점됩니다."""
    assert scheduler._claim_daily_run(conn, "PREMARKET", date(2026, 8, 3)) is True
    assert scheduler._claim_daily_run(conn, "REGULAR", date(2026, 8, 3)) is True


def test_claim_daily_run_different_date_is_independent(conn) -> None:
    assert scheduler._claim_daily_run(conn, "PREMARKET", date(2026, 8, 3)) is True
    assert scheduler._claim_daily_run(conn, "PREMARKET", date(2026, 8, 4)) is True


# ---------------------------------------------------------------------------
# _submit_or_dry_run (드라이런 배선)
# ---------------------------------------------------------------------------


def test_submit_or_dry_run_in_dry_run_mode_never_calls_kiwoom(conn, tmp_path: Path) -> None:
    dry_config = _make_config(dry_run=True, db_path=tmp_path / "d.db", event_log_path=tmp_path / "e.jsonl")
    order = OrderIntent(side="BUY", order_kind="LOC", price=Decimal("50.00"), qty=10, purpose=BUY_TYPE_FIRST)
    notifier = _RecordingNotifier()

    with patch("infinite_buying_v4.scheduler.submit_order_with_retry") as mock_submit:
        result = scheduler._submit_or_dry_run(dry_config, order, notifier, conn, date(2026, 8, 3))

    mock_submit.assert_not_called()
    assert result is None
    # submitted_orders에도 기록되면 안 됩니다 (실제 주문이 없으므로 대조할 대상도 없음).
    assert conn.execute("SELECT COUNT(*) AS c FROM submitted_orders").fetchone()["c"] == 0


def test_submit_or_dry_run_live_mode_submits_and_records(conn, config: Config) -> None:
    order = OrderIntent(side="BUY", order_kind="LOC", price=Decimal("50.00"), qty=10, purpose=BUY_TYPE_FIRST)
    notifier = _RecordingNotifier()
    fake_submitted = SubmittedOrder(order_no="ORD-1", raw_response={})

    with patch("infinite_buying_v4.scheduler.submit_order_with_retry", return_value=fake_submitted) as mock_submit:
        result = scheduler._submit_or_dry_run(config, order, notifier, conn, date(2026, 8, 3))

    mock_submit.assert_called_once()
    assert result is fake_submitted
    row = conn.execute("SELECT * FROM submitted_orders WHERE order_no = 'ORD-1'").fetchone()
    assert row is not None
    assert row["purpose"] == BUY_TYPE_FIRST


def test_submit_or_dry_run_retry_exhausted_returns_none_without_recording(conn, config: Config) -> None:
    """submit_order_with_retry가 재시도 끝에 실패(KiwoomAdapterError)하면, 이미 그 함수
    내부에서 CRITICAL 알림을 보냈으므로 여기서는 조용히 None을 반환하고 넘어갑니다."""
    order = OrderIntent(side="BUY", order_kind="LOC", price=Decimal("50.00"), qty=10, purpose=BUY_TYPE_FIRST)
    notifier = _RecordingNotifier()

    with patch("infinite_buying_v4.scheduler.submit_order_with_retry", side_effect=KiwoomAdapterError("실패")):
        result = scheduler._submit_or_dry_run(config, order, notifier, conn, date(2026, 8, 3))

    assert result is None
    assert conn.execute("SELECT COUNT(*) AS c FROM submitted_orders").fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# _classify_daily_events (T값 갱신 이벤트 분류 - 부분체결 포함)
# ---------------------------------------------------------------------------


def _seed_submitted_order(conn, order_no: str, *, side: str, order_kind: str, price: str, qty: int, purpose: str, is_decoy: bool = False) -> None:
    conn.execute(
        """
        INSERT INTO submitted_orders (order_no, submitted_date, side, order_kind, price, qty, purpose, is_decoy)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (order_no, "2026-08-03", side, order_kind, price, qty, purpose, int(is_decoy)),
    )


def test_classify_full_buy_when_both_half_orders_fully_fill(conn) -> None:
    """전반전 절반매수 두 건이 모두 전액 체결되면 FULL_BUY(T+=1)로 분류돼야 합니다."""
    _seed_submitted_order(conn, "O1", side="BUY", order_kind="LOC", price="50.00", qty=10, purpose=BUY_TYPE_HALF_STAR)
    _seed_submitted_order(conn, "O2", side="BUY", order_kind="LOC", price="48.00", qty=10, purpose=BUY_TYPE_HALF_AVG)
    fills = [
        FillRecord(order_no="O1", side="BUY", fill_price=Decimal("50.00"), fill_qty=10, fill_time="", order_status=""),
        FillRecord(order_no="O2", side="BUY", fill_price=Decimal("48.00"), fill_qty=10, fill_time="", order_status=""),
    ]
    notifier = _RecordingNotifier()

    events = scheduler._classify_daily_events(conn, fills, notifier)

    assert events == [EVENT_FULL_BUY]


def test_classify_half_buy_when_only_one_of_two_orders_fills(conn) -> None:
    """두 절반매수 주문 중 하나만 체결되면(다른 하나는 미체결) HALF_BUY(T+=0.5)로 분류돼야 합니다."""

    _seed_submitted_order(conn, "O1", side="BUY", order_kind="LOC", price="50.00", qty=10, purpose=BUY_TYPE_HALF_STAR)
    _seed_submitted_order(conn, "O2", side="BUY", order_kind="LOC", price="48.00", qty=10, purpose=BUY_TYPE_HALF_AVG)
    # O2는 체결되지 않음 (fills 목록에 없음) -> 체결 비율 50%
    fills = [
        FillRecord(order_no="O1", side="BUY", fill_price=Decimal("50.00"), fill_qty=10, fill_time="", order_status=""),
    ]
    notifier = _RecordingNotifier()

    events = scheduler._classify_daily_events(conn, fills, notifier)

    assert events == [EVENT_HALF_BUY]


def test_classify_half_buy_on_partial_fill_of_single_order(conn) -> None:
    """단일 주문이 수량 기준으로 일부만 체결된 경우(예: 10주 중 4주만 체결)도 체결금액
    비율로 판정되어 HALF_BUY가 되어야 합니다 — 이것이 "부분체결"의 핵심 케이스입니다."""

    _seed_submitted_order(conn, "O1", side="BUY", order_kind="LOC", price="50.00", qty=10, purpose=BUY_TYPE_FIRST)
    fills = [
        FillRecord(order_no="O1", side="BUY", fill_price=Decimal("50.00"), fill_qty=4, fill_time="", order_status=""),
    ]
    notifier = _RecordingNotifier()

    events = scheduler._classify_daily_events(conn, fills, notifier)

    assert events == [EVENT_HALF_BUY]


def test_classify_full_buy_when_partial_fill_exceeds_threshold(conn) -> None:
    """체결 비율이 99% 이상이면(예: 100주 중 99주) 반올림해서 FULL_BUY로 분류합니다."""

    _seed_submitted_order(conn, "O1", side="BUY", order_kind="LOC", price="50.00", qty=100, purpose=BUY_TYPE_FIRST)
    fills = [
        FillRecord(order_no="O1", side="BUY", fill_price=Decimal("50.00"), fill_qty=99, fill_time="", order_status=""),
    ]
    notifier = _RecordingNotifier()

    events = scheduler._classify_daily_events(conn, fills, notifier)

    assert events == [EVENT_FULL_BUY]


def test_classify_quarter_sell(conn) -> None:

    _seed_submitted_order(conn, "O1", side="SELL", order_kind="LOC", price="57.50", qty=25, purpose=SELL_TYPE_QUARTER)
    fills = [
        FillRecord(order_no="O1", side="SELL", fill_price=Decimal("57.50"), fill_qty=25, fill_time="", order_status=""),
    ]
    notifier = _RecordingNotifier()

    events = scheduler._classify_daily_events(conn, fills, notifier)

    assert events == [EVENT_QUARTER_SELL]


def test_classify_limit_sell_then_full_loc_buy_combo_warns(conn) -> None:
    """지정가매도 + 같은 날 LOC매수(전액) 동시 체결 -> 복합 이벤트 + WARNING 알림."""

    _seed_submitted_order(conn, "S1", side="SELL", order_kind="LIMIT", price="57.50", qty=75, purpose=SELL_TYPE_LIMIT_15PCT)
    _seed_submitted_order(conn, "B1", side="BUY", order_kind="LOC", price="45.00", qty=10, purpose=BUY_TYPE_FIRST)
    fills = [
        FillRecord(order_no="S1", side="SELL", fill_price=Decimal("57.50"), fill_qty=75, fill_time="", order_status=""),
        FillRecord(order_no="B1", side="BUY", fill_price=Decimal("45.00"), fill_qty=10, fill_time="", order_status=""),
    ]
    notifier = _RecordingNotifier()

    events = scheduler._classify_daily_events(conn, fills, notifier)

    assert events == [EVENT_LIMIT_SELL_THEN_LOC_BUY_FULL]
    assert len(notifier.calls_at(NotifyLevel.WARNING)) == 1


def test_classify_decoy_fill_triggers_critical_and_is_excluded_from_buy_ratio(conn) -> None:
    """미끼(decoy) 주문이 체결되면 CRITICAL 알림만 발생하고, buy_intended/buy_filled
    집계에는 포함되지 않아야 합니다(오염 방지)."""
    _seed_submitted_order(
        conn, "D1", side="BUY", order_kind="LOC", price="56.00", qty=1, purpose=BUY_TYPE_FIRST, is_decoy=True
    )
    fills = [
        FillRecord(order_no="D1", side="BUY", fill_price=Decimal("56.00"), fill_qty=1, fill_time="", order_status=""),
    ]
    notifier = _RecordingNotifier()

    events = scheduler._classify_daily_events(conn, fills, notifier)

    assert events == []  # 미끼 체결만으로는 T값 갱신 이벤트가 생기지 않음
    assert len(notifier.calls_at(NotifyLevel.CRITICAL)) == 1


def test_classify_purges_matched_submitted_orders(conn) -> None:
    """대조에 사용한 submitted_orders 행은 정리(delete)되어야 다음날 재사용되지 않습니다."""
    _seed_submitted_order(conn, "O1", side="BUY", order_kind="LOC", price="50.00", qty=10, purpose=BUY_TYPE_FIRST)
    fills = [
        FillRecord(order_no="O1", side="BUY", fill_price=Decimal("50.00"), fill_qty=10, fill_time="", order_status=""),
    ]
    scheduler._classify_daily_events(conn, fills, _RecordingNotifier())

    assert conn.execute("SELECT COUNT(*) AS c FROM submitted_orders").fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# _purge_stale_submitted_orders (미체결 취소 주문 정리)
# ---------------------------------------------------------------------------


def test_purge_stale_submitted_orders_removes_only_older_rows(conn) -> None:
    """오늘 이전에 제출된(=체결 없이 취소된) 주문만 정리하고, 오늘 제출된 것은 남겨야 합니다."""
    conn.execute(
        "INSERT INTO submitted_orders (order_no, submitted_date, side, order_kind, price, qty, purpose, is_decoy)"
        " VALUES ('OLD1', '2026-08-01', 'BUY', 'LOC', '50.00', 10, ?, 0)",
        (BUY_TYPE_FIRST,),
    )
    conn.execute(
        "INSERT INTO submitted_orders (order_no, submitted_date, side, order_kind, price, qty, purpose, is_decoy)"
        " VALUES ('TODAY1', '2026-08-03', 'BUY', 'LOC', '50.00', 10, ?, 0)",
        (BUY_TYPE_FIRST,),
    )

    purged = scheduler._purge_stale_submitted_orders(conn, before_date=date(2026, 8, 3))

    assert purged == 1
    remaining = {r["order_no"] for r in conn.execute("SELECT order_no FROM submitted_orders")}
    assert remaining == {"TODAY1"}


def test_purge_stale_submitted_orders_archives_to_cancelled_orders(conn) -> None:
    """정리되는 주문은 삭제 전에 cancelled_orders에 영구 이력으로 남아야 합니다."""
    conn.execute(
        "INSERT INTO submitted_orders (order_no, submitted_date, side, order_kind, price, qty, purpose, is_decoy)"
        " VALUES ('OLD1', '2026-08-01', 'SELL', 'LIMIT', '57.50', 75, ?, 0)",
        (SELL_TYPE_LIMIT_15PCT,),
    )

    scheduler._purge_stale_submitted_orders(conn, before_date=date(2026, 8, 3))

    row = conn.execute("SELECT * FROM cancelled_orders WHERE order_no = 'OLD1'").fetchone()
    assert row is not None
    assert row["submitted_date"] == "2026-08-01"
    assert row["cancelled_date"] == "2026-08-03"
    assert row["side"] == "SELL"
    assert row["price"] == "57.50"
    assert row["qty"] == 75
    assert row["purpose"] == SELL_TYPE_LIMIT_15PCT
    assert row["is_decoy"] == 0


def test_purge_stale_submitted_orders_does_not_archive_todays_rows(conn) -> None:
    """오늘 제출된(아직 살아있는) 주문은 cancelled_orders로 옮겨지면 안 됩니다."""
    conn.execute(
        "INSERT INTO submitted_orders (order_no, submitted_date, side, order_kind, price, qty, purpose, is_decoy)"
        " VALUES ('TODAY1', '2026-08-03', 'BUY', 'LOC', '50.00', 10, ?, 0)",
        (BUY_TYPE_FIRST,),
    )

    scheduler._purge_stale_submitted_orders(conn, before_date=date(2026, 8, 3))

    assert conn.execute("SELECT COUNT(*) AS c FROM cancelled_orders").fetchone()["c"] == 0


def test_stale_orders_do_not_pollute_next_run_fill_ratio(conn) -> None:
    """정리하지 않았다면 발생했을 시나리오를 재현: 어제 취소된 주문이 남아있는 상태에서
    오늘 주문이 100% 체결돼도, 정리를 거치면 어제 주문이 비율 계산에 섞이지 않아야 합니다."""
    _seed_submitted_order(conn, "OLD1", side="BUY", order_kind="LOC", price="50.00", qty=999, purpose=BUY_TYPE_FIRST)
    conn.execute("UPDATE submitted_orders SET submitted_date = '2026-08-01' WHERE order_no = 'OLD1'")

    scheduler._purge_stale_submitted_orders(conn, before_date=date(2026, 8, 3))

    _seed_submitted_order(conn, "T1", side="BUY", order_kind="LOC", price="50.00", qty=10, purpose=BUY_TYPE_FIRST)
    fills = [FillRecord(order_no="T1", side="BUY", fill_price=Decimal("50.00"), fill_qty=10, fill_time="", order_status="")]

    events = scheduler._classify_daily_events(conn, fills, _RecordingNotifier())

    assert events == [EVENT_FULL_BUY]
