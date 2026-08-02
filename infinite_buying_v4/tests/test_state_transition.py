"""
test_state_transition.py
=========================
state.py의 영속화(save/load)와 상태 전이(bootstrap, 사이클 재시작) 규칙을 검증합니다.

설계도 1번의 핵심 요구사항인 "상태가 없으면 절대 임의값으로 시작하지 않는다"를
가장 먼저, 가장 엄격하게 테스트합니다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from infinite_buying_v4 import db
from infinite_buying_v4.state import (
    MODE_NORMAL,
    MODE_REVERSE,
    PHASE_FIRST,
    PHASE_FIRST_HALF,
    State,
    StateError,
    bootstrap_new_state,
    load_state,
    save_state,
    start_new_cycle,
    state_exists,
)


@pytest.fixture
def conn(tmp_path: Path):
    """테스트마다 새 SQLite 파일로 커넥션을 만듭니다(실제 파일 기반 영속화를 검증하기 위함)."""
    connection = db.get_connection(tmp_path / "test.db")
    yield connection
    connection.close()


def test_load_state_raises_when_no_state_row(conn) -> None:
    """상태 행이 아예 없으면 절대 기본값을 만들지 않고 StateError를 던져야 합니다 (설계도 1번)."""
    with pytest.raises(StateError):
        load_state(conn)


def test_bootstrap_new_state_creates_t0_empty_holding(conn) -> None:
    """신규 시작은 T=0, 보유 0, 사이클 1번, 잔금=원금이어야 합니다."""
    state = bootstrap_new_state(
        conn, split_count=40, principal=Decimal("10000"), start_date=date(2026, 8, 3)
    )
    assert state.mode == MODE_NORMAL
    assert state.phase == PHASE_FIRST
    assert state.t == Decimal(0)
    assert state.holding_qty == 0
    assert state.avg_price == Decimal(0)
    assert state.cycle_id == 1
    assert state.remaining_cash == Decimal("10000")
    assert state_exists(conn) is True


def test_bootstrap_new_state_rejects_when_already_exists(conn) -> None:
    """이미 상태가 있는데 다시 bootstrap하면 기존 데이터를 덮어쓸 위험이 있으므로 거부해야 합니다."""
    bootstrap_new_state(conn, split_count=40, principal=Decimal("10000"), start_date=date(2026, 8, 3))
    with pytest.raises(StateError):
        bootstrap_new_state(conn, split_count=20, principal=Decimal("5000"), start_date=date(2026, 8, 4))


def test_bootstrap_new_state_rejects_invalid_split_count(conn) -> None:
    with pytest.raises(StateError):
        bootstrap_new_state(conn, split_count=30, principal=Decimal("10000"), start_date=date(2026, 8, 3))


def test_bootstrap_new_state_rejects_non_positive_principal(conn) -> None:
    with pytest.raises(StateError):
        bootstrap_new_state(conn, split_count=40, principal=Decimal("0"), start_date=date(2026, 8, 3))


def test_save_and_load_round_trip_preserves_decimal_precision(conn) -> None:
    """Decimal 값이 SQLite에 TEXT로 저장됐다가 다시 로드될 때 정밀도가 그대로 보존돼야 합니다."""
    bootstrap_new_state(conn, split_count=40, principal=Decimal("10000"), start_date=date(2026, 8, 3))
    loaded = load_state(conn)

    updated = State(
        mode=MODE_NORMAL,
        phase=PHASE_FIRST_HALF,
        split_count=40,
        principal=loaded.principal,
        remaining_cash=Decimal("9999.995"),
        t=Decimal("5.165479525"),  # 설계도 1번 예시와 동일한 정밀도
        avg_price=Decimal("52.34"),
        holding_qty=100,
        cycle_id=1,
        cycle_start_date=loaded.cycle_start_date,
        reverse_day_count=0,
        reverse_prev_qty=0,
    )
    save_state(conn, updated)

    reloaded = load_state(conn)
    assert reloaded.t == Decimal("5.165479525")
    assert reloaded.remaining_cash == Decimal("9999.995")
    assert reloaded.avg_price == Decimal("52.34")
    assert reloaded.phase == PHASE_FIRST_HALF


def test_validate_rejects_reverse_mode_with_phase_set() -> None:
    state = State(
        mode=MODE_REVERSE,
        phase=PHASE_FIRST,  # REVERSE인데 phase가 있으면 안 됨
        split_count=40,
        principal=Decimal("10000"),
        remaining_cash=Decimal("10000"),
        t=Decimal("39"),
        avg_price=Decimal("50"),
        holding_qty=100,
        cycle_id=1,
        cycle_start_date=date(2026, 8, 3),
        reverse_day_count=1,
        reverse_prev_qty=100,
    )
    with pytest.raises(StateError):
        state.validate()


def test_validate_rejects_holding_zero_with_nonzero_avg_price() -> None:
    state = State(
        mode=MODE_NORMAL,
        phase=PHASE_FIRST,
        split_count=40,
        principal=Decimal("10000"),
        remaining_cash=Decimal("10000"),
        t=Decimal("0"),
        avg_price=Decimal("50"),  # 보유 0인데 평단가가 남아있으면 초기화 누락
        holding_qty=0,
        cycle_id=1,
        cycle_start_date=date(2026, 8, 3),
        reverse_day_count=0,
        reverse_prev_qty=0,
    )
    with pytest.raises(StateError):
        state.validate()


def test_start_new_cycle_requires_zero_holding(conn) -> None:
    state = bootstrap_new_state(conn, split_count=40, principal=Decimal("10000"), start_date=date(2026, 8, 3))
    still_holding = State(**{**state.__dict__, "holding_qty": 10, "avg_price": Decimal("50")})
    with pytest.raises(StateError):
        start_new_cycle(
            conn,
            still_holding,
            fixed_principal=Decimal("10000"),
            compound_on_restart=True,
            start_date=date(2026, 9, 1),
        )


def test_start_new_cycle_compound_uses_remaining_cash(conn) -> None:
    """복리 모드: 새 원금 = 이전 사이클 종료 시점의 잔금(수익 포함) (설계도 8번)."""
    state = bootstrap_new_state(conn, split_count=40, principal=Decimal("10000"), start_date=date(2026, 8, 3))
    ended = State(**{**state.__dict__, "remaining_cash": Decimal("11500"), "holding_qty": 0})

    new_state = start_new_cycle(
        conn, ended, fixed_principal=Decimal("10000"), compound_on_restart=True, start_date=date(2026, 9, 1)
    )
    assert new_state.principal == Decimal("11500")
    assert new_state.remaining_cash == Decimal("11500")
    assert new_state.cycle_id == 2
    assert new_state.t == Decimal(0)
    assert new_state.holding_qty == 0


def test_start_new_cycle_fixed_uses_config_principal(conn) -> None:
    """단리 모드: 새 원금 = 설정에 고정된 원금(수익과 무관) (설계도 8번)."""
    state = bootstrap_new_state(conn, split_count=40, principal=Decimal("10000"), start_date=date(2026, 8, 3))
    ended = State(**{**state.__dict__, "remaining_cash": Decimal("11500"), "holding_qty": 0})

    new_state = start_new_cycle(
        conn, ended, fixed_principal=Decimal("10000"), compound_on_restart=False, start_date=date(2026, 9, 1)
    )
    assert new_state.principal == Decimal("10000")
    assert new_state.remaining_cash == Decimal("10000")
    assert new_state.cycle_id == 2
