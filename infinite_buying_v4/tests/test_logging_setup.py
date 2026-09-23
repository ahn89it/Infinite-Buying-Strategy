"""
test_logging_setup.py
=======================
logging_setup.py의 로그 파일 회전 설정을 검증합니다.

핵심 검증 포인트: 로그 파일이 "KST 자정" 기준으로 날짜별 회전되는지 — 이는
TimedRotatingFileHandler를 utc=True + atTime=UTC 15:00으로 설정해 구현했습니다
(KST는 서머타임이 없는 UTC+9 고정 오프셋이므로, UTC 15:00이 항상 정확히 KST
00:00과 같습니다). 실제로 자정을 넘겨 파일이 회전되는 것까지 테스트하지는
않습니다(시간 흐름 자체를 mocking해야 해서 배보다 배꼽이 커짐) — 대신 핸들러가
올바른 파라미터로 구성됐는지만 확인합니다.
"""

from __future__ import annotations

import logging
from datetime import time
from decimal import Decimal
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import pytest

from infinite_buying_v4.config import Config
from infinite_buying_v4.logging_setup import configure_logging


@pytest.fixture
def config(tmp_path: Path) -> Config:
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
        db_path=tmp_path / "data" / "test.db",
        event_log_path=tmp_path / "data" / "event_log.jsonl",
        dashboard_port=8000,
        dry_run=True,
    )


@pytest.fixture(autouse=True)
def _cleanup_root_handlers():
    # 다른 테스트/모듈이 root logger에 설정해둔 핸들러와 서로 오염되지 않도록
    # 테스트 전후로 핸들러를 정리합니다.
    yield
    logging.getLogger().handlers.clear()


def _get_file_handler() -> TimedRotatingFileHandler:
    handlers = [h for h in logging.getLogger().handlers if isinstance(h, TimedRotatingFileHandler)]
    assert len(handlers) == 1, "TimedRotatingFileHandler가 정확히 1개 등록되어야 합니다."
    return handlers[0]


def test_configure_logging_creates_log_file_at_data_dir_logs(config: Config) -> None:
    configure_logging(config)

    expected_path = config.db_path.parent / "logs" / "app.log"
    assert expected_path.exists()


def test_configure_logging_rotates_at_kst_midnight_regardless_of_host_timezone(config: Config) -> None:
    """KST는 서머타임이 없는 UTC+9 고정 오프셋이므로, "UTC 15:00마다 회전"으로 설정하면
    호스트 시스템의 시간대 설정과 무관하게 항상 정확히 KST 00:00에 회전됩니다."""
    configure_logging(config)

    handler = _get_file_handler()
    assert handler.utc is True
    assert handler.atTime == time(15, 0)
    # TimedRotatingFileHandler는 when 값을 대문자로 정규화해 저장합니다.
    assert handler.when.upper() == "MIDNIGHT"


def test_configure_logging_does_not_accumulate_handlers_on_repeated_calls(config: Config) -> None:
    """__main__.py처럼 대시보드+스케줄러를 한 프로세스에서 같이 띄우는 경우,
    configure_logging()이 여러 번 불려도 핸들러가 계속 누적되면 안 됩니다
    (누적되면 로그 한 줄마다 파일에 여러 번 중복 기록됨)."""
    configure_logging(config)
    configure_logging(config)
    configure_logging(config)

    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, TimedRotatingFileHandler)]
    console_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler) and not isinstance(h, TimedRotatingFileHandler)]
    assert len(file_handlers) == 1
    assert len(console_handlers) == 1


def test_configure_logging_writes_log_records_to_the_file(config: Config) -> None:
    configure_logging(config)

    logging.getLogger("infinite_buying_v4.test").info("테스트 로그 메시지")

    log_path = config.db_path.parent / "logs" / "app.log"
    content = log_path.read_text(encoding="utf-8")
    assert "테스트 로그 메시지" in content
