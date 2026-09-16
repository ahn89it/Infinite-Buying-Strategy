"""
db.py
=====
SQLite 커넥션 생성과 스키마(테이블) 초기화를 담당하는 공용 모듈입니다.

왜 이 모듈이 필요한가?
- state.py, trade_history.py 등 여러 모듈이 같은 SQLite 파일을 공유합니다.
  "커넥션을 어떻게 열지", "테이블이 없으면 어떻게 만들지"를 각 모듈이 따로
  구현하면 스키마가 흩어지고 중복됩니다. 이 모듈 하나에만 CREATE TABLE 문을
  모아두고, 다른 모듈은 이 모듈이 제공하는 커넥션만 받아서 씁니다.
- 금액/T값 같은 정밀도가 중요한 값은 SQLite에 REAL(float)로 저장하지 않고
  TEXT로 저장합니다. SQLite의 REAL은 IEEE754 float이라 Decimal의 정밀도를
  보장하지 못하기 때문입니다(설계도 2번 "Decimal 타입 사용 권장"과 동일한 이유).
  각 모듈은 저장 시 str(Decimal 값), 로드 시 Decimal(문자열)로 변환해서 씁니다.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# 스키마 정의: 설계도 1번(state), 9-2번(buy/sell_records, cycle_summary),
# 9-4번(portfolio_summary)을 그대로 SQL 테이블로 옮긴 것입니다.
#
# "IF NOT EXISTS"를 써서 이미 테이블이 있는 재시작 상황에서도 안전하게
# 반복 실행할 수 있게 합니다(설계도 1번 "재시작에도 상태가 유지되어야 함").
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    # state: 현재 진행 중인 사이클의 상태를 담는 "싱글턴" 테이블입니다.
    # 항상 id=1인 행 하나만 존재하도록 CHECK 제약을 걸어, 실수로 여러 상태가
    # 동시에 생기는 것을 DB 레벨에서 막습니다.
    """
    CREATE TABLE IF NOT EXISTS state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        mode TEXT NOT NULL,                    -- "NORMAL" | "REVERSE"
        phase TEXT,                             -- "FIRST" | "FIRST_HALF" | "SECOND_HALF" (NORMAL일 때만)
        split_count INTEGER NOT NULL,           -- 20 | 40
        principal TEXT NOT NULL,                -- Decimal 문자열, 현재 사이클 원금
        remaining_cash TEXT NOT NULL,           -- Decimal 문자열, 잔금
        t_value TEXT NOT NULL,                  -- Decimal 문자열, 회차값 T (SQL 예약어 회피를 위해 t_value로 명명)
        avg_price TEXT NOT NULL,                -- Decimal 문자열, 평단가
        holding_qty INTEGER NOT NULL,           -- 보유 수량
        cycle_id INTEGER NOT NULL,              -- 현재 사이클 번호
        cycle_start_date TEXT NOT NULL,         -- YYYY-MM-DD
        reverse_day_count INTEGER NOT NULL DEFAULT 0,  -- 리버스모드 경과일 (D1, D2, ...)
        reverse_prev_qty INTEGER NOT NULL DEFAULT 0,   -- 리버스모드 전일 보유수량
        updated_at TEXT NOT NULL                -- ISO8601 타임스탬프, 마지막 갱신 시각
    )
    """,
    # buy_records: 사람이 조회하는 매수 체결 이력 (설계도 9-2번)
    """
    CREATE TABLE IF NOT EXISTS buy_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id INTEGER NOT NULL,
        buy_date TEXT NOT NULL,                 -- YYYY-MM-DD, 체결일
        buy_price TEXT NOT NULL,                -- Decimal 문자열, 체결 단가
        buy_qty INTEGER NOT NULL,               -- 체결 수량
        buy_amount TEXT NOT NULL,               -- Decimal 문자열, buy_price * buy_qty
        order_type TEXT NOT NULL,               -- FIRST | HALF_STAR | HALF_AVG | FULL_STAR | REVERSE 등
        t_after TEXT NOT NULL,                  -- 이 매수 체결 직후 T값 스냅샷
        avg_price_after TEXT NOT NULL,          -- 이 매수 체결 직후 평단가 스냅샷
        created_at TEXT NOT NULL                -- 레코드 기록 시각(ISO8601)
    )
    """,
    # sell_records: 사람이 조회하는 매도 체결 이력 (설계도 9-2번)
    """
    CREATE TABLE IF NOT EXISTS sell_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id INTEGER NOT NULL,
        sell_date TEXT NOT NULL,
        sell_price TEXT NOT NULL,
        sell_qty INTEGER NOT NULL,
        sell_amount TEXT NOT NULL,
        order_type TEXT NOT NULL,               -- QUARTER | LIMIT_15PCT | REVERSE_MOC | REVERSE_LOC
        avg_price_at_sell TEXT NOT NULL,
        return_pct TEXT NOT NULL,               -- (sell_price - avg_price_at_sell) / avg_price_at_sell * 100
        profit_amount TEXT NOT NULL,             -- (sell_price - avg_price_at_sell) * sell_qty
        t_after TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    # cycle_summary: 사이클 종료(보유수량 0) 시점에 자동 생성되는 요약 (설계도 9-2번)
    """
    CREATE TABLE IF NOT EXISTS cycle_summary (
        cycle_id INTEGER PRIMARY KEY,
        start_date TEXT NOT NULL,
        end_date TEXT,                          -- 사이클이 끝나기 전에는 NULL
        total_buy_amount TEXT NOT NULL DEFAULT '0',
        total_sell_amount TEXT NOT NULL DEFAULT '0',
        cycle_profit_amount TEXT,               -- total_sell_amount - total_buy_amount
        cycle_return_pct TEXT,
        hit_reverse_mode INTEGER NOT NULL DEFAULT 0,  -- SQLite에는 BOOLEAN이 없어 0/1로 저장
        duration_days INTEGER
    )
    """,
    # portfolio_summary: 사이클을 넘어선 계좌 전체 누적 성과, 단일 행(id=1) (설계도 9-4번)
    """
    CREATE TABLE IF NOT EXISTS portfolio_summary (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        strategy_start_date TEXT NOT NULL,
        initial_principal TEXT NOT NULL,
        total_realized_profit TEXT NOT NULL DEFAULT '0',
        total_realized_return_pct TEXT NOT NULL DEFAULT '0',
        current_unrealized_pnl TEXT NOT NULL DEFAULT '0',
        current_unrealized_return_pct TEXT NOT NULL DEFAULT '0',
        total_equity TEXT NOT NULL,
        total_return_pct TEXT NOT NULL DEFAULT '0',
        completed_cycles INTEGER NOT NULL DEFAULT 0,
        last_updated TEXT NOT NULL
    )
    """,
    # submitted_orders: scheduler.py가 오늘 제출한 주문을 기록해두는 내부 장부 테이블입니다.
    # 설계도 9번 스키마에는 없지만(설계도는 "체결 확인된" 기록만 다룸), 다음 실행 때 체결
    # 내역(order_no)과 "이 주문이 무슨 목적(purpose)의 주문이었는지"를 대조하려면 필요한
    # 최소한의 운영용 부가 테이블입니다. 체결 확인 후에는 buy_records/sell_records로
    # 정식 기록되고, 이 테이블의 해당 행은 더 이상 필요 없어져 정리(purge)됩니다.
    """
    CREATE TABLE IF NOT EXISTS submitted_orders (
        order_no TEXT PRIMARY KEY,
        submitted_date TEXT NOT NULL,
        side TEXT NOT NULL,
        order_kind TEXT NOT NULL,
        price TEXT,
        qty INTEGER NOT NULL,
        purpose TEXT NOT NULL,
        is_decoy INTEGER NOT NULL DEFAULT 0
    )
    """,
    # cancelled_orders: submitted_orders에서 체결 매칭 없이 정리(purge)되는 주문의 영구
    # 이력입니다. submitted_orders는 "아직 살아있는 주문"만 담는 작업용 장부라 매칭되면
    # 지워지고, 매칭 없이 오래되면(=취소된 것으로 간주) 그냥 삭제됐었습니다 — 그러면
    # "이 주문이 취소됐다"는 사실 자체가 사라져서, 대시보드나 사람이 나중에 확인할 방법이
    # 없었습니다. 그래서 삭제하기 직전에 이 테이블로 옮겨 담아(scheduler.py의
    # _purge_stale_submitted_orders 참고) 영구히 남깁니다.
    """
    CREATE TABLE IF NOT EXISTS cancelled_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_no TEXT NOT NULL,
        submitted_date TEXT NOT NULL,           -- 원래 제출된 날짜
        cancelled_date TEXT NOT NULL,           -- 취소(정리)로 확정된 날짜 (다음 프리장 실행일)
        side TEXT NOT NULL,
        order_kind TEXT NOT NULL,
        price TEXT,
        qty INTEGER NOT NULL,
        purpose TEXT NOT NULL,
        is_decoy INTEGER NOT NULL DEFAULT 0,
        recorded_at TEXT NOT NULL               -- 이 이력이 기록된 시각 (ISO8601)
    )
    """,
    # daily_run_log: scheduler.py의 멱등성(중복 실행 방지) 가드입니다.
    # (run_type, run_date) 조합을 PRIMARY KEY로 걸어서, 같은 날 같은 종류의 작업
    # (PREMARKET/REGULAR)이 두 번 "성공적으로 시작"할 수 없게 DB 레벨에서 강제합니다.
    # INSERT 시점(작업 시작 직전)에 먼저 claim하고, 이미 존재하면(PK 충돌) 그날 그
    # 작업은 이미 실행된 것으로 간주해 건너뜁니다. 컨테이너가 재시작돼도 이 테이블은
    # data/ 볼륨에 영속화되어 있으므로 중복 제출을 막습니다. 자세한 판정 키와 복구
    # 절차는 운영 런북 문서 참고.
    """
    CREATE TABLE IF NOT EXISTS daily_run_log (
        run_type TEXT NOT NULL,                 -- "PREMARKET" | "REGULAR"
        run_date TEXT NOT NULL,                 -- YYYY-MM-DD (America/New_York 기준 거래일)
        claimed_at TEXT NOT NULL,               -- 이 작업을 시작 선점한 시각 (ISO8601)
        PRIMARY KEY (run_type, run_date)
    )
    """,
    # =========================================================================
    # DRY_RUN 시뮬레이션(dry_run_simulator.py) 전용 테이블 (2026-09-16 추가)
    # =========================================================================
    # 실제 계좌 상태(state, buy_records, ...)는 DRY_RUN 중에는 전혀 갱신되지
    # 않습니다(실제 체결이 없으므로). 하지만 그 상태로는 "이 전략이 정말 동작하는지"를
    # T=0(첫매수) 이상으로는 검증할 수 없습니다. 그래서 실제 계좌와 완전히 분리된
    # "모의 상태(shadow state)"를 두고, 실제 종가/고가/저가 데이터로 그날 주문이
    # 체결됐을지를 시뮬레이션해서 이 모의 상태를 실제처럼 하루하루 진행시킵니다.
    # 스키마는 실제 테이블과 최대한 동일하게 맞춰서 state.py/trade_history.py의
    # 검증된 로직을 그대로 재사용합니다(별도 시뮬레이션 전용 로직을 새로 만들지 않음).

    # dry_run_state: state와 완전히 동일한 스키마의 "모의 계좌" 상태.
    """
    CREATE TABLE IF NOT EXISTS dry_run_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        mode TEXT NOT NULL,
        phase TEXT,
        split_count INTEGER NOT NULL,
        principal TEXT NOT NULL,
        remaining_cash TEXT NOT NULL,
        t_value TEXT NOT NULL,
        avg_price TEXT NOT NULL,
        holding_qty INTEGER NOT NULL,
        cycle_id INTEGER NOT NULL,
        cycle_start_date TEXT NOT NULL,
        reverse_day_count INTEGER NOT NULL DEFAULT 0,
        reverse_prev_qty INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """,
    # dry_run_orders: submitted_orders의 모의 버전. 실제 주문번호가 없으므로(진짜로
    # 제출된 적이 없음) order_no 대신 자동증가 id를 씁니다. 다음날 프리장 시점에 그날의
    # 실제 OHLC와 대조해 체결 여부를 시뮬레이션한 뒤 삭제됩니다(체결 -> dry_run_fills로
    # 이관, 미체결 -> 그냥 삭제, submitted_orders와 달리 "취소 이력"은 남기지 않음 —
    # 어차피 진짜 주문이 아니었으므로 사람이 확인할 실익이 적음).
    """
    CREATE TABLE IF NOT EXISTS dry_run_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        submitted_date TEXT NOT NULL,
        side TEXT NOT NULL,
        order_kind TEXT NOT NULL,
        price TEXT,
        qty INTEGER NOT NULL,
        purpose TEXT NOT NULL,
        is_decoy INTEGER NOT NULL DEFAULT 0
    )
    """,
    # dry_run_buy_records / dry_run_sell_records: 시뮬레이션 결과 "체결됐다"고 판정된
    # 모의 매수/매도 기록입니다. buy_records/sell_records와 스키마를 완전히 동일하게
    # 맞춰서, trade_history.py의 record_buy()/record_sell()/close_cycle_summary()를
    # table 매개변수만 바꿔 그대로 재사용합니다(계산 로직 중복 없음).
    """
    CREATE TABLE IF NOT EXISTS dry_run_buy_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id INTEGER NOT NULL,
        buy_date TEXT NOT NULL,
        buy_price TEXT NOT NULL,
        buy_qty INTEGER NOT NULL,
        buy_amount TEXT NOT NULL,
        order_type TEXT NOT NULL,
        t_after TEXT NOT NULL,
        avg_price_after TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dry_run_sell_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id INTEGER NOT NULL,
        sell_date TEXT NOT NULL,
        sell_price TEXT NOT NULL,
        sell_qty INTEGER NOT NULL,
        sell_amount TEXT NOT NULL,
        order_type TEXT NOT NULL,
        avg_price_at_sell TEXT NOT NULL,
        return_pct TEXT NOT NULL,
        profit_amount TEXT NOT NULL,
        t_after TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    # dry_run_cycle_summary: cycle_summary의 모의 버전. 모의 사이클이 완주(보유수량 0)될
    # 때까지 실제로 며칠 걸리는지도 함께 보여줍니다(실제 하루=모의 하루, 빨리 감기 아님).
    """
    CREATE TABLE IF NOT EXISTS dry_run_cycle_summary (
        cycle_id INTEGER PRIMARY KEY,
        start_date TEXT NOT NULL,
        end_date TEXT,
        total_buy_amount TEXT NOT NULL DEFAULT '0',
        total_sell_amount TEXT NOT NULL DEFAULT '0',
        cycle_profit_amount TEXT,
        cycle_return_pct TEXT,
        hit_reverse_mode INTEGER NOT NULL DEFAULT 0,
        duration_days INTEGER
    )
    """,
    # dry_run_portfolio_summary: portfolio_summary의 모의 버전 (2026-09-16 추가).
    # 실제 계좌는 scheduler.py가 실시간 시세(get_quote)로 매번 갱신하지만, 모의 계좌는
    # 실시간 시세가 없으므로 매 프리장 실행 시점에 조회한 "가장 최근 일봉 종가"를
    # current_price로 삼아 갱신합니다(dry_run_simulator.run_dry_run_premarket 참고) —
    # 그래서 장중 실시간 값이 아니라 최대 하루 전 종가 기준 스냅샷입니다.
    """
    CREATE TABLE IF NOT EXISTS dry_run_portfolio_summary (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        strategy_start_date TEXT NOT NULL,
        initial_principal TEXT NOT NULL,
        total_realized_profit TEXT NOT NULL DEFAULT '0',
        total_realized_return_pct TEXT NOT NULL DEFAULT '0',
        current_unrealized_pnl TEXT NOT NULL DEFAULT '0',
        current_unrealized_return_pct TEXT NOT NULL DEFAULT '0',
        total_equity TEXT NOT NULL,
        total_return_pct TEXT NOT NULL DEFAULT '0',
        completed_cycles INTEGER NOT NULL DEFAULT 0,
        last_updated TEXT NOT NULL
    )
    """,
    # 조회 성능을 위한 인덱스. cycle_id로 거래 이력을 자주 조회하므로(대시보드 등) 추가합니다.
    "CREATE INDEX IF NOT EXISTS idx_buy_records_cycle_id ON buy_records (cycle_id)",
    "CREATE INDEX IF NOT EXISTS idx_sell_records_cycle_id ON sell_records (cycle_id)",
    "CREATE INDEX IF NOT EXISTS idx_cancelled_orders_cancelled_date ON cancelled_orders (cancelled_date)",
    "CREATE INDEX IF NOT EXISTS idx_dry_run_buy_records_cycle_id ON dry_run_buy_records (cycle_id)",
    "CREATE INDEX IF NOT EXISTS idx_dry_run_sell_records_cycle_id ON dry_run_sell_records (cycle_id)",
)


def get_connection(db_path: Path) -> sqlite3.Connection:
    """SQLite 커넥션을 열고 스키마를 초기화한 뒤 반환합니다.

    - db_path의 부모 디렉터리가 없으면 만들어줍니다(Docker 볼륨 최초 마운트 시
      데이터 디렉터리가 비어있는 경우 대비).
    - row_factory를 sqlite3.Row로 설정해, 다른 모듈에서 `row["column_name"]`처럼
      컬럼명으로 접근할 수 있게 합니다(인덱스 번호로 접근하면 컬럼 순서가 바뀔 때
      버그가 생기기 쉬움).
    - foreign_keys는 이 스키마에서 FK를 쓰지 않으므로 별도로 켜지 않습니다.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)  # autocommit 모드: 각 실행이 즉시 커밋됨
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """스키마를 생성합니다(이미 존재하면 아무 것도 하지 않음). 여러 번 호출해도 안전합니다."""
    for statement in _SCHEMA_STATEMENTS:
        conn.execute(statement)


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """`with connect(db_path) as conn:` 형태로 쓰는 컨텍스트 매니저.

    짧은 스크립트(테스트, 일회성 조회 등)에서 커넥션을 열고 확실히 닫기 위해 사용합니다.
    scheduler.py처럼 프로세스 내내 살아있는 장기 실행 컨텍스트에서는 get_connection()을
    직접 써서 커넥션을 계속 재사용하는 것을 권장합니다.
    """
    conn = get_connection(db_path)
    try:
        yield conn
    finally:
        conn.close()
