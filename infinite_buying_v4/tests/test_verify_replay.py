"""
test_verify_replay.py
======================
verify_replay.py의 핵심 순수함수(compute_replayed_t)와, CLI 종료 코드 계약
(0=일치, 1=불일치, --repair --apply로 실제 복구)을 검증합니다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from infinite_buying_v4 import db
from infinite_buying_v4.event_log import EVENT_FULL_BUY, EVENT_HALF_BUY, append_event, make_event
from infinite_buying_v4.state import bootstrap_new_state
from infinite_buying_v4.verify_replay import compute_replayed_t, run_verification


def test_compute_replayed_t_only_includes_current_cycle_events() -> None:
    """사이클 시작일 이전(=이전 사이클)의 이벤트는 재생 대상에서 제외되어야 합니다."""
    events = [
        make_event(event_date=date(2026, 7, 1), event_type=EVENT_FULL_BUY),  # 이전 사이클
        make_event(event_date=date(2026, 8, 3), event_type=EVENT_FULL_BUY),  # 현재 사이클
        make_event(event_date=date(2026, 8, 4), event_type=EVENT_HALF_BUY),  # 현재 사이클
    ]
    replayed_t, cycle_events = compute_replayed_t(events, cycle_start_date=date(2026, 8, 3))

    assert len(cycle_events) == 2
    assert replayed_t == Decimal("1.5")


def test_compute_replayed_t_empty_events_returns_zero() -> None:
    replayed_t, cycle_events = compute_replayed_t([], cycle_start_date=date(2026, 8, 3))
    assert replayed_t == Decimal(0)
    assert cycle_events == []


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch):
    """load_config()가 이 테스트 전용 .env/DB/이벤트로그를 보도록 격리합니다."""
    db_path = tmp_path / "test.db"
    event_log_path = tmp_path / "event_log.jsonl"
    monkeypatch.setenv("APP_KEY", "x")
    monkeypatch.setenv("APP_SECRET", "x")
    monkeypatch.setenv("SPLIT_COUNT", "40")
    monkeypatch.setenv("PRINCIPAL", "10000")
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("EVENT_LOG_PATH", str(event_log_path))
    return db_path, event_log_path


def test_run_verification_matching_t_returns_exit_code_0(isolated_env) -> None:
    db_path, event_log_path = isolated_env
    conn = db.get_connection(db_path)
    state = bootstrap_new_state(conn, split_count=40, principal=Decimal("10000"), start_date=date(2026, 8, 3))
    append_event(event_log_path, make_event(event_date=date(2026, 8, 3), event_type=EVENT_FULL_BUY))
    # state.t는 bootstrap 시 0이므로, 이벤트를 반영한 값(1)과 다르게 만들어 재현합니다.
    from dataclasses import replace

    from infinite_buying_v4.state import save_state

    save_state(conn, replace(state, t=Decimal("1")))
    conn.close()

    exit_code = run_verification(repair=False, apply_fix=False)
    assert exit_code == 0


def test_run_verification_mismatched_t_returns_exit_code_1_without_writing(isolated_env) -> None:
    db_path, event_log_path = isolated_env
    conn = db.get_connection(db_path)
    bootstrap_new_state(conn, split_count=40, principal=Decimal("10000"), start_date=date(2026, 8, 3))
    append_event(event_log_path, make_event(event_date=date(2026, 8, 3), event_type=EVENT_FULL_BUY))
    conn.close()
    # state.t == 0 (bootstrap 기본값)인데, 이벤트 로그를 재생하면 T=1이 되어야 하므로 불일치.

    exit_code = run_verification(repair=False, apply_fix=False)
    assert exit_code == 1

    conn = db.get_connection(db_path)
    row = conn.execute("SELECT t_value FROM state WHERE id = 1").fetchone()
    conn.close()
    assert row["t_value"] == "0"  # --apply 없이는 수정되지 않아야 함


def test_run_verification_repair_apply_writes_fixed_t(isolated_env) -> None:
    db_path, event_log_path = isolated_env
    conn = db.get_connection(db_path)
    bootstrap_new_state(conn, split_count=40, principal=Decimal("10000"), start_date=date(2026, 8, 3))
    append_event(event_log_path, make_event(event_date=date(2026, 8, 3), event_type=EVENT_FULL_BUY))
    conn.close()

    exit_code = run_verification(repair=True, apply_fix=True)
    assert exit_code == 1  # 불일치가 있었다는 사실 자체는 여전히 1로 보고합니다(수정 여부와 무관).

    conn = db.get_connection(db_path)
    row = conn.execute("SELECT t_value FROM state WHERE id = 1").fetchone()
    conn.close()
    assert row["t_value"] == "1"  # 재생 결과로 실제 갱신됨
