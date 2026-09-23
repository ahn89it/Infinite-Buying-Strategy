"""
test_kiwoom_adapter.py
========================
kiwoom_adapter.py 중 실제 키움 API 인증 없이도 검증 가능한 순수 로직만 다룹니다.

키움 REST API는 "조회할 내역이 0건"인 정상 상황(예: 오늘 미체결 주문이 하나도
없음, 오늘 체결이 하나도 없음)도 return_code=0(성공)이 아니라 에러 응답으로
내려줍니다(예: "[2000](571758:해당 계좌의 미체결내역이 없습니다.)"). 이 테스트는
실제 운영 로그 분석으로 발견된 버그 — get_open_orders()/get_today_fills()가 이
"빈 결과" 응답까지 KiwoomAdapterError로 취급해 예외를 던지는 바람에, 미체결
주문이 하나도 없는(=지극히 정상적인) 날마다 프리장 갱신 전체가 실패하던 문제 —
를 고정 검증합니다.

실제 REST/WebSocket 호출부(_client().fetch_page 내부)는 진짜 키움 API 인증이
필요하므로, `_client()`를 unittest.mock으로 대체해 네트워크 없이 검증합니다.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from kiwoom.core.errors import APIError

from infinite_buying_v4 import kiwoom_adapter
from infinite_buying_v4.config import Config
from infinite_buying_v4.kiwoom_adapter import KiwoomAdapterError, get_open_orders, get_today_fills


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
        db_path=tmp_path / "test.db",
        event_log_path=tmp_path / "event_log.jsonl",
        dashboard_port=8000,
        dry_run=True,
    )


def _fake_response(result_list: list[dict]) -> MagicMock:
    response = MagicMock()
    response.body = {"result_list": result_list}
    return response


# ---------------------------------------------------------------------------
# _is_no_data_error (순수함수)
# ---------------------------------------------------------------------------


def test_is_no_data_error_matches_no_open_orders_message() -> None:
    exc = APIError(20, "[2000](571758:해당 계좌의미체결내역이 없습니다.)")
    assert kiwoom_adapter._is_no_data_error(exc) is True


def test_is_no_data_error_false_for_unrelated_api_error() -> None:
    exc = APIError(20, "[2000](508540:해외증권주문 가능 계좌가 아닙니다.)")
    assert kiwoom_adapter._is_no_data_error(exc) is False


def test_is_no_data_error_false_for_non_api_error() -> None:
    from kiwoom import KiwoomError

    assert kiwoom_adapter._is_no_data_error(KiwoomError("네트워크 오류")) is False


# ---------------------------------------------------------------------------
# get_open_orders — "미체결 없음" 응답은 빈 리스트, 그 외 오류는 그대로 전파
# ---------------------------------------------------------------------------


def test_get_open_orders_returns_empty_list_when_account_has_no_open_orders(config: Config) -> None:
    with patch.object(kiwoom_adapter, "_client") as mock_client:
        mock_client.return_value.fetch_page.side_effect = APIError(
            20, "[2000](571758:해당 계좌의미체결내역이 없습니다.)"
        )
        result = get_open_orders(config)

    assert result == []


def test_get_open_orders_still_raises_for_real_errors(config: Config) -> None:
    with patch.object(kiwoom_adapter, "_client") as mock_client:
        mock_client.return_value.fetch_page.side_effect = APIError(
            20, "[2000](508540:해외증권주문 가능 계좌가 아닙니다.)"
        )
        with pytest.raises(KiwoomAdapterError):
            get_open_orders(config)


def test_get_open_orders_parses_rows_with_remaining_quantity(config: Config) -> None:
    with patch.object(kiwoom_adapter, "_client") as mock_client:
        mock_client.return_value.fetch_page.return_value = _fake_response(
            [
                {"ord_no": "1", "ord_remnq": "5"},
                {"ord_no": "2", "ord_remnq": "0"},  # 잔량 0 -> 이미 종료된 주문, 제외되어야 함
            ]
        )
        result = get_open_orders(config)

    assert result == ["1"]


# ---------------------------------------------------------------------------
# get_today_fills — 동일한 "결과 없음" 처리
# ---------------------------------------------------------------------------


def test_get_today_fills_returns_empty_list_when_no_fills_today(config: Config) -> None:
    with patch.object(kiwoom_adapter, "_client") as mock_client:
        mock_client.return_value.fetch_page.side_effect = APIError(20, "[2000](123456:당일 체결내역이 없습니다.)")
        result = get_today_fills(config)

    assert result == []


def test_get_today_fills_still_raises_for_real_errors(config: Config) -> None:
    with patch.object(kiwoom_adapter, "_client") as mock_client:
        mock_client.return_value.fetch_page.side_effect = APIError(
            20, "[2000](508540:해외증권주문 가능 계좌가 아닙니다.)"
        )
        with pytest.raises(KiwoomAdapterError):
            get_today_fills(config)
