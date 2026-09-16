"""
state.py
========
프로그램이 매일 추적해야 하는 핵심 상태(State)를 정의하고, SQLite에 영속화(저장/로드)하는
모듈입니다 (설계도 1번).

핵심 설계 원칙 (설계도 1번 원문):
    "상태 파일이 없거나 손상된 경우 수동 개입 없이는 절대 임의값으로 시작하지
    않도록 안전장치(assert/예외 발생)를 둡니다."

그래서 이 모듈은 두 가지 경로를 엄격히 분리합니다:
    1) load_state(): 기존 상태를 "이어받는" 경로. 상태 행이 없거나 값이 이상하면
       무조건 StateError를 던집니다. 절대로 기본값을 만들어 대신 반환하지 않습니다.
    2) bootstrap_new_state()/start_new_cycle(): 사람이 "지금부터 신규로 시작한다"
       또는 "이번 사이클이 끝났으니 다음 사이클을 시작한다"는 것을 명시적으로
       요청했을 때만 호출되는 별도 함수입니다. scheduler.py의 일상적인 자동 실행
       경로에서는 이 함수들이 저절로 호출되지 않습니다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal

# --- 모드/페이즈 상수 ---
# 문자열을 매직 리터럴로 여기저기 흩뿌리지 않기 위해 상수로 선언합니다.
# (오타로 인한 버그를 IDE/타입체커가 잡아낼 수 있게 함)
MODE_NORMAL = "NORMAL"
MODE_REVERSE = "REVERSE"

PHASE_FIRST = "FIRST"  # 첫매수 (T=0)
PHASE_FIRST_HALF = "FIRST_HALF"  # 전반전
PHASE_SECOND_HALF = "SECOND_HALF"  # 후반전

Mode = Literal["NORMAL", "REVERSE"]
Phase = Literal["FIRST", "FIRST_HALF", "SECOND_HALF"]

_VALID_SPLIT_COUNTS = (20, 40)


class StateError(Exception):
    """상태 로드/저장이 불가능하거나 상태값이 불변식(invariant)을 위반할 때 발생시키는 예외.

    이 예외가 발생하면 자동매매는 절대 계속 진행해서는 안 됩니다(설계도 1, 11번).
    """


@dataclass(frozen=True)
class State:
    """무한매수법 진행 상태 스냅샷 (설계도 1번 표를 그대로 옮김).

    frozen=True로 불변 객체로 만든 이유: 상태 전이는 "이전 상태 + 이벤트 -> 새 상태"
    형태로 명시적으로 이루어져야 재현/디버깅이 쉽습니다(이벤트 소싱 패턴, 설계도 2번).
    필드를 직접 수정하는 대신 dataclasses.replace()로 "새 State"를 만들어 사용합니다.
    """

    mode: Mode
    phase: Phase | None  # NORMAL일 때만 의미 있음. REVERSE일 때는 None.
    split_count: int  # 20 또는 40
    principal: Decimal  # 현재 사이클의 원금
    remaining_cash: Decimal  # 잔금 = 원금 - 누적매수금액 + 누적매도금액
    t: Decimal  # 회차값 T (소수점 허용)
    avg_price: Decimal  # 현재 평단가
    holding_qty: int  # 현재 보유 수량
    cycle_id: int  # 현재 사이클 번호 (사이클 종료마다 +1)
    cycle_start_date: date  # 현재 사이클 시작일
    reverse_day_count: int  # 리버스모드 진입 후 경과일 (D1, D2, ...)
    reverse_prev_qty: int  # 리버스모드 전일 보유수량 (매도수량 계산용)

    def validate(self) -> None:
        """상태값이 스스로 모순되지 않는지 확인합니다. 위반 시 StateError.

        DB에서 막 읽어온 직후, 그리고 저장하기 직전에 항상 호출해서, 손상된
        상태값이 조용히 사용되는 일이 없게 합니다.
        """
        if self.mode not in (MODE_NORMAL, MODE_REVERSE):
            raise StateError(f"알 수 없는 mode입니다: {self.mode!r}")
        if self.mode == MODE_NORMAL and self.phase not in (
            PHASE_FIRST,
            PHASE_FIRST_HALF,
            PHASE_SECOND_HALF,
        ):
            raise StateError(f"NORMAL 모드인데 phase가 올바르지 않습니다: {self.phase!r}")
        if self.mode == MODE_REVERSE and self.phase is not None:
            raise StateError(f"REVERSE 모드에서는 phase가 None이어야 합니다 (현재: {self.phase!r})")
        if self.split_count not in _VALID_SPLIT_COUNTS:
            raise StateError(f"split_count는 {_VALID_SPLIT_COUNTS} 중 하나여야 합니다: {self.split_count}")
        if self.holding_qty < 0:
            raise StateError(f"holding_qty는 음수일 수 없습니다: {self.holding_qty}")
        if self.remaining_cash < 0:
            raise StateError(f"remaining_cash는 음수일 수 없습니다: {self.remaining_cash}")
        if self.t < 0:
            raise StateError(f"T값은 음수일 수 없습니다: {self.t}")
        if self.holding_qty == 0 and self.avg_price != 0:
            raise StateError(
                f"보유수량이 0인데 평단가가 0이 아닙니다(사이클 종료 후 초기화 누락 의심): {self.avg_price}"
            )


def load_state(conn: sqlite3.Connection, *, table: str = "state") -> State:
    """SQLite에서 현재 상태를 로드합니다.

    상태 행이 없으면(state 테이블이 비어있음) 절대 기본값을 만들지 않고
    StateError를 던집니다 — 설계도 1번의 핵심 안전장치입니다. 신규로 시작하려면
    이 함수 대신 bootstrap_new_state()를 사람이 명시적으로 호출해야 합니다.

    `table` 매개변수: 기본은 실제 계좌 상태인 `state` 테이블이지만, DRY_RUN
    시뮬레이션이 실제 계좌를 절대 건드리지 않으면서도 이 검증된 영속화 로직을
    그대로 재사용할 수 있도록 `dry_run_state`(스키마는 동일) 같은 다른 테이블도
    지정할 수 있게 했습니다(dry_run_simulator.py 참고). 이 값은 항상 코드 내부
    상수(`"state"` 또는 `"dry_run_state"`)만 들어오고 사용자 입력이 절대 아니므로,
    SQL 문자열에 직접 끼워 넣어도 인젝션 위험이 없습니다.
    """
    row = conn.execute(f"SELECT * FROM {table} WHERE id = 1").fetchone()  # noqa: S608
    if row is None:
        raise StateError(
            "상태 데이터가 없습니다. 신규 시작이라면 bootstrap_new_state()를 명시적으로 "
            "호출해서 최초 상태를 생성해야 합니다. 기존 상태가 있어야 하는 상황이라면 "
            "DB 파일이 잘못된 경로를 가리키고 있지 않은지 확인하세요."
        )
    try:
        state = State(
            mode=row["mode"],
            phase=row["phase"],
            split_count=row["split_count"],
            principal=Decimal(row["principal"]),
            remaining_cash=Decimal(row["remaining_cash"]),
            t=Decimal(row["t_value"]),
            avg_price=Decimal(row["avg_price"]),
            holding_qty=row["holding_qty"],
            cycle_id=row["cycle_id"],
            cycle_start_date=date.fromisoformat(row["cycle_start_date"]),
            reverse_day_count=row["reverse_day_count"],
            reverse_prev_qty=row["reverse_prev_qty"],
        )
    except (ValueError, TypeError, ArithmeticError) as exc:
        # Decimal() 변환 실패, 날짜 파싱 실패 등 "행은 있지만 값이 손상된" 경우도
        # 여기서 전부 StateError로 통일해서, 호출부가 항상 같은 예외 타입만 처리하면 되게 합니다.
        raise StateError(f"저장된 상태값을 읽는 중 오류가 발생했습니다(데이터 손상 의심): {exc}") from exc

    state.validate()
    return state


def save_state(conn: sqlite3.Connection, state: State, *, table: str = "state") -> None:
    """현재 상태를 SQLite에 저장합니다 (id=1 행을 UPSERT).

    저장 직전에 반드시 validate()를 호출해, 잘못된 상태가 그대로 영속화되는
    것을 막습니다. `table` 설명은 load_state() 참고.
    """
    state.validate()
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        f"""
        INSERT INTO {table} (
            id, mode, phase, split_count, principal, remaining_cash, t_value,
            avg_price, holding_qty, cycle_id, cycle_start_date,
            reverse_day_count, reverse_prev_qty, updated_at
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            mode = excluded.mode,
            phase = excluded.phase,
            split_count = excluded.split_count,
            principal = excluded.principal,
            remaining_cash = excluded.remaining_cash,
            t_value = excluded.t_value,
            avg_price = excluded.avg_price,
            holding_qty = excluded.holding_qty,
            cycle_id = excluded.cycle_id,
            cycle_start_date = excluded.cycle_start_date,
            reverse_day_count = excluded.reverse_day_count,
            reverse_prev_qty = excluded.reverse_prev_qty,
            updated_at = excluded.updated_at
        """,  # noqa: S608
        (
            state.mode,
            state.phase,
            state.split_count,
            str(state.principal),
            str(state.remaining_cash),
            str(state.t),
            str(state.avg_price),
            state.holding_qty,
            state.cycle_id,
            state.cycle_start_date.isoformat(),
            state.reverse_day_count,
            state.reverse_prev_qty,
            now_iso,
        ),
    )


def state_exists(conn: sqlite3.Connection, *, table: str = "state") -> bool:
    """상태 행이 이미 존재하는지 확인합니다. bootstrap 전에 "이미 시작됨"을 감지할 때 사용."""
    row = conn.execute(f"SELECT 1 FROM {table} WHERE id = 1").fetchone()  # noqa: S608
    return row is not None


def bootstrap_new_state(
    conn: sqlite3.Connection,
    *,
    split_count: int,
    principal: Decimal,
    start_date: date,
    table: str = "state",
) -> State:
    """완전히 새로운 무한매수법을 시작합니다 (T=0, 보유 0, 사이클 1번).

    이 함수는 프로그램이 자동으로 호출해서는 안 되고, 사용자가 "이 계좌로 무한매수법을
    처음 시작한다"고 명시적으로 의도했을 때만(예: 최초 셋업 스크립트 1회 실행) 호출해야
    합니다. 이미 상태가 존재하면 실수로 원금/이력을 덮어쓰는 사고를 막기 위해 StateError를
    던집니다.

    예외: `table="dry_run_state"`로 호출하는 DRY_RUN 시뮬레이션(dry_run_simulator.py)은
    실제 계좌가 아니므로, 사람이 매번 수동으로 부트스트랩할 필요 없이 스케줄러가
    필요할 때 자동으로 호출합니다(진짜 `state` 테이블에는 이 자동 호출을 절대 하지 않음).
    """
    if state_exists(conn, table=table):
        raise StateError(
            "이미 상태가 존재합니다. 신규 시작은 상태가 전혀 없는 계좌에서만 허용됩니다. "
            "사이클을 새로 시작하려면 start_new_cycle()을 사용하세요."
        )
    if split_count not in _VALID_SPLIT_COUNTS:
        raise StateError(f"split_count는 {_VALID_SPLIT_COUNTS} 중 하나여야 합니다: {split_count}")
    if principal <= 0:
        raise StateError(f"원금(principal)은 0보다 커야 합니다: {principal}")

    state = State(
        mode=MODE_NORMAL,
        phase=PHASE_FIRST,
        split_count=split_count,
        principal=principal,
        remaining_cash=principal,  # 아직 아무것도 매수하지 않았으므로 잔금 = 원금
        t=Decimal(0),
        avg_price=Decimal(0),
        holding_qty=0,
        cycle_id=1,
        cycle_start_date=start_date,
        reverse_day_count=0,
        reverse_prev_qty=0,
    )
    save_state(conn, state, table=table)
    return state


def start_new_cycle(
    conn: sqlite3.Connection,
    previous_state: State,
    *,
    fixed_principal: Decimal,
    compound_on_restart: bool,
    start_date: date,
    table: str = "state",
) -> State:
    """사이클 종료(보유수량 0) 후 다음 사이클을 시작합니다 (설계도 8번).

    - previous_state.holding_qty가 0이 아니면 아직 사이클이 끝나지 않은 것이므로 호출하면 안 됩니다.
    - compound_on_restart=True(복리): 새 원금 = 이전 사이클 종료 시점의 remaining_cash
      (이전 사이클 실현 손익이 그대로 반영된 잔금을 다음 사이클 원금으로 그대로 이어받음).
    - compound_on_restart=False(단리): 새 원금 = 설정 파일에 고정된 fixed_principal
      (수익/손실과 무관하게 항상 같은 금액으로 재시작).
    """
    if previous_state.holding_qty != 0:
        raise StateError(
            f"보유수량이 0이 아닌데 새 사이클을 시작할 수 없습니다 (현재 보유수량: {previous_state.holding_qty})"
        )

    new_principal = previous_state.remaining_cash if compound_on_restart else fixed_principal
    if new_principal <= 0:
        raise StateError(f"다음 사이클 원금이 0 이하입니다: {new_principal}. 자동매매를 중단해야 합니다.")

    new_state = replace(
        previous_state,
        mode=MODE_NORMAL,
        phase=PHASE_FIRST,
        principal=new_principal,
        remaining_cash=new_principal,
        t=Decimal(0),
        avg_price=Decimal(0),
        holding_qty=0,
        cycle_id=previous_state.cycle_id + 1,
        cycle_start_date=start_date,
        reverse_day_count=0,
        reverse_prev_qty=0,
    )
    save_state(conn, new_state, table=table)
    return new_state
