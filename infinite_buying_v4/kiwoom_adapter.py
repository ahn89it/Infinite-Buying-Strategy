"""
kiwoom_adapter.py
==================
키움증권 REST API(공식 저장소: github.com/Kiwoom-Securities/Kiwoom-REST-API, PyPI 패키지
`kwcli`가 제공하는 `kiwoom` 모듈)를 감싸서, 이 프로젝트의 OrderIntent를 실제 주문으로
제출/취소하고, 체결 내역을 조회/구독하는 어댑터입니다.

이 모듈은 "OAuth 토큰 발급/갱신, REST 호출 재시도 하부구조"는 공식 `kiwoom` 패키지에
맡기고(이미 검증된 공식 구현을 재발명하지 않기 위함), 그 위에 무한매수법 전용 함수
(별지점 LOC매수, 쿼터매도, 지정가매도, MOC매도, 체결 조회/구독, 우리 프로젝트만의
주문 실패 재시도 정책)를 새로 작성합니다.

중요: 이 어댑터는 항상 키움 REST API "실투자(운영)" 엔드포인트만 사용합니다. 키움
모의투자는 해외주식(미국주식) 매매를 지원하지 않아서, 이 프로젝트에는 모의투자 모드
자체가 없습니다(config.py 참고). 실주문 없이 로직만 점검하려면 `.env`의
`DRY_RUN=true` 설정을 사용하세요 — `submit_order_with_retry()`를 호출하는
`scheduler.py` 쪽에서 실제 제출을 건너뛰고 계산 결과만 로그로 남깁니다. 이 모듈의
시세/체결 조회 함수들은 DRY_RUN 여부와 무관하게 항상 실계좌 데이터를 그대로
반환합니다(읽기 전용이라 안전).

사전 준비 (사용자가 직접 해야 하는 일, Claude Code가 대신할 수 없음):
1. 키움증권 OpenAPI 포털에서 실투자용 App Key/Secret 발급
2. `pip install kwcli` (requirements.txt에 포함됨)
3. `.env`에 APP_KEY, APP_SECRET 설정 (config.py 참고)
4. kwcli의 인증 저장 방식(OS 자격 증명 저장소 또는 .env)에 맞춰 최초 1회 토큰 발급 확인

주의(실거래 전 반드시 확인):
- 아래 `trde_tp`/`api_id` 값들은 공식 저장소의 examples/미국주식/주문, examples/미국주식/계좌,
  examples/미국주식/실시간시세 폴더 예제 코드를 근거로 작성했습니다(2026-08 기준 확인).
  API가 개정되면 값이 바뀔 수 있으므로, 실거래 전에 openapi.kiwoom.com 최신 문서와
  대조하세요.
- 실시간 체결통보(F5)의 필드 코드 "907"(매도수구분) 값의 매도/매수 구분(0/1 등)은
  REST 조회 API의 `slby_tp`(0:전체,1:매도,2:매수) 규칙과 동일하다고 가정했습니다.
  모의투자로 사전 검증할 수 없으므로, `DRY_RUN=true` 상태로 실계좌 시세는 그대로
  받으며 실제 수신값을 로그로 찍어 확인한 뒤, 최소 수량으로 실주문 1건을 내서
  직접 검증하는 것을 권장합니다.
"""

from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Awaitable, Callable, Literal

from kiwoom import KiwoomError, get_client, get_ws_client
from kiwoom.core.errors import APIError

from infinite_buying_v4.config import Config
from infinite_buying_v4.notifier import NotifierBase
from infinite_buying_v4.orders import OrderIntent

logger = logging.getLogger("infinite_buying_v4.kiwoom_adapter")

# --- 키움 REST API 주문유형(trde_tp) 코드 매핑 ---
# 00:지정가 03:시장가 26:VWAP지정가 27:TWAP지정가 30:LOC 33:MOC 36:VWAP시장가 37:TWAP시장가
# (examples/미국주식/주문/buy_overseas_stock.py, sell_overseas_stock.py 문서 주석 근거)
_ORDER_KIND_TO_TRDE_TP = {
    "LIMIT": "00",
    "LOC": "30",
    "MOC": "33",
}

_BUY_API_ID = "ust20000"  # 미국주식 매수 주문
_SELL_API_ID = "ust20001"  # 미국주식 매도 주문
_CANCEL_API_ID = "ust20003"  # 미국주식 취소 주문
_TODAY_FILLS_API_ID = "ust21510"  # 미국주식 당일 주문체결 확인
_OPEN_ORDERS_API_ID = "ust21050"  # 미국주식 원장 미체결
_DEPOSIT_API_ID = "ust21110"  # 해외주식 예수금
_QUOTE_API_ID = "usa20100"  # 미국주식 현재가 종목정보
_DAILY_CHART_API_ID = "usa06012"  # 미국주식 일 차트
_ORDER_PATH = "/api/us/ordr"
_ACCOUNT_PATH = "/api/us/acnt"
_QUOTE_PATH = "/api/us/mrkcond"
_CHART_PATH = "/api/us/chart"

_REALTIME_FILL_TYPE = "F5"  # 미국주식 실시간 체결
_REALTIME_API_URL = "/api/us/websocket"
# 실시간 체결(F5) 필드 코드 -> 우리가 쓰는 이름으로 매핑 (전체 필드 목록은 공식 예제 주석 참고)
_REALTIME_FILL_COLUMNS = {
    "9203": "order_no",  # 주문번호
    "907": "side_code",  # 매도수구분
    "910": "fill_price",  # 체결가
    "911": "fill_qty",  # 체결량
    "908": "fill_time",  # 주문/체결시간
    "913": "order_status",  # 주문상태
}

_SLBY_TP_BY_SIDE_FILTER = {"ALL": "0", "SELL": "1", "BUY": "2"}


class KiwoomAdapterError(Exception):
    """키움 REST API 호출이 실패했거나 예상치 못한 응답을 받았을 때 발생시키는 예외."""


@dataclass(frozen=True)
class SubmittedOrder:
    """증권사에 제출되어 접수번호를 받은 주문 (아직 "체결"은 아님, 접수 확인일 뿐)."""

    order_no: str
    raw_response: dict[str, Any]


@dataclass(frozen=True)
class FillRecord:
    """실제 체결 1건 (REST 조회 또는 실시간 구독 어느 경로로 왔든 동일한 형태로 통일)."""

    order_no: str
    side: Literal["BUY", "SELL"]
    fill_price: Decimal
    fill_qty: int
    fill_time: str
    order_status: str


def _client():
    """OAuth 토큰 캐싱/자동 갱신을 포함한 공식 REST 클라이언트를 가져옵니다."""
    return get_client()


# 키움 REST API는 "조회할 내역이 0건"인 정상 상황도 return_code=0(성공)이 아니라
# 에러 코드로 응답합니다(예: 미체결 주문이 하나도 없을 때 "[2000](571758:해당
# 계좌의 미체결내역이 없습니다.)"). 이런 응답까지 KiwoomAdapterError로 취급해
# 예외를 던지면, "오늘 정리할 미체결 주문이 없다"는 지극히 정상적인 매일의 상황
# 때문에 프리장 갱신 전체가 매번 실패합니다(실제로 이 문제로 여러 날 연속 프리장이
# 한 번도 성공하지 못했던 것이 로그로 확인됨). 그래서 "내역이 없습니다" 류의
# 메시지가 포함된 APIError는 "빈 결과"로 간주해 빈 리스트를 반환하고, 그 외의
# 진짜 오류(인증 실패, 네트워크 오류, 계좌 권한 문제 등)만 예외로 전파합니다.
_NO_DATA_MESSAGE_MARKERS = ("내역이 없습니다",)


def _is_no_data_error(exc: KiwoomError) -> bool:
    return isinstance(exc, APIError) and any(marker in exc.return_msg for marker in _NO_DATA_MESSAGE_MARKERS)


def submit_order(config: Config, order: OrderIntent) -> SubmittedOrder:
    """OrderIntent 하나를 키움 REST API로 제출합니다 (재시도 없는 단발 호출).

    일반적으로는 이 함수를 직접 쓰지 말고 submit_order_with_retry()를 사용하세요
    (설계도 11번 "증권사 API 주문 실패/재시도 로직").
    """
    trde_tp = _ORDER_KIND_TO_TRDE_TP[order.order_kind]
    api_id = _BUY_API_ID if order.side == "BUY" else _SELL_API_ID

    body: dict[str, Any] = {
        "stex_tp": config.exchange_code,
        "stk_cd": config.ticker,
        "ord_qty": str(order.qty),
        "trde_tp": trde_tp,
    }
    if order.price is not None:
        body["ord_uv"] = str(order.price)  # 시장가/MOC는 가격을 지정하지 않음

    try:
        response = _client().fetch_page(api_id=api_id, path=_ORDER_PATH, body=body)
    except KiwoomError as exc:
        raise KiwoomAdapterError(
            f"{order.side} 주문 제출 실패 (purpose={order.purpose}, qty={order.qty}, price={order.price}): {exc}"
        ) from exc

    response_body = response.body
    return_code = response_body.get("return_code")
    if return_code not in (None, 0, "0"):
        raise KiwoomAdapterError(
            f"{order.side} 주문이 증권사에서 거부되었습니다 (purpose={order.purpose}): "
            f"code={return_code}, msg={response_body.get('return_msg')}"
        )

    return SubmittedOrder(order_no=str(response_body.get("ord_no", "")), raw_response=response_body)


def submit_order_with_retry(
    config: Config,
    order: OrderIntent,
    *,
    notifier: NotifierBase,
    max_retries: int = 3,
    backoff_seconds: float = 2.0,
) -> SubmittedOrder:
    """주문 제출을 재시도하며, 모두 실패하면 CRITICAL 알림을 보내고 예외를 던집니다
    (설계도 11번). 재시도 간격은 시도 횟수에 비례해 늘어나는 단순 선형 백오프입니다
    (증권사 API에 짧은 시간 동안 과도한 재요청을 보내지 않기 위함).

    이 함수가 예외를 던지면, 호출부(scheduler.py)는 반드시 그날의 자동매매 실행을
    중단해야 합니다 — 원인 파악 전에 남은 주문을 계속 제출하면 상태 불일치 위험이
    커집니다.
    """
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return submit_order(config, order)
        except KiwoomAdapterError as exc:
            last_error = exc
            logger.warning("주문 제출 실패 (%d/%d회): %s", attempt, max_retries, exc)
            if attempt < max_retries:
                time.sleep(backoff_seconds * attempt)

    notifier.notify_critical(
        "주문 제출 반복 실패 - 자동매매 중단 필요",
        f"{order.side} {order.purpose} 주문(qty={order.qty}, price={order.price})을 "
        f"{max_retries}회 재시도했지만 모두 실패했습니다. 마지막 오류: {last_error}",
    )
    raise KiwoomAdapterError(f"{max_retries}회 재시도 후에도 주문 제출에 실패했습니다: {last_error}")


def cancel_order(config: Config, orig_ord_no: str) -> dict[str, Any]:
    """기존 주문을 취소합니다 (미체결 잔량이 남은 주문을 다음 거래일 로직 시작 전에
    정리할 때 사용)."""
    body = {"orig_ord_no": orig_ord_no, "stex_tp": config.exchange_code, "stk_cd": config.ticker}
    try:
        response = _client().fetch_page(api_id=_CANCEL_API_ID, path=_ORDER_PATH, body=body)
    except KiwoomError as exc:
        raise KiwoomAdapterError(f"주문 취소 실패 (orig_ord_no={orig_ord_no}): {exc}") from exc
    return response.body


def get_today_fills(config: Config, *, side: Literal["ALL", "BUY", "SELL"] = "ALL") -> list[FillRecord]:
    """당일 체결 내역을 REST로 조회합니다 (설계도 9-3번: "실제 체결 확인된 시점에만 기록").

    scheduler.py는 장 마감 후 이 함수로 그날의 실제 체결을 받아와, trade_history.py에
    기록하고 event_log.py 재생을 위한 이벤트를 만듭니다. cntr_qty(체결수량)가 0인 행은
    아직 미체결이므로 제외합니다.

    그날 체결이 하나도 없는 것도 정상 상황입니다(예: DRY_RUN이라 애초에 주문을 낸
    적이 없는 날) — 키움 API가 이 경우를 에러 코드로 응답하더라도(_is_no_data_error
    참고) 빈 리스트를 반환합니다.
    """
    body = {
        "slby_tp": _SLBY_TP_BY_SIDE_FILTER[side],
        "stex_tp": config.exchange_code,
        "stk_cd": config.ticker,
    }
    try:
        response = _client().fetch_page(api_id=_TODAY_FILLS_API_ID, path=_ACCOUNT_PATH, body=body)
    except APIError as exc:
        if _is_no_data_error(exc):
            return []
        raise KiwoomAdapterError(f"당일 체결 조회 실패: {exc}") from exc
    except KiwoomError as exc:
        raise KiwoomAdapterError(f"당일 체결 조회 실패: {exc}") from exc

    rows = response.body.get("result_list") or []
    fills: list[FillRecord] = []
    for row in rows:
        cntr_qty = int(row.get("cntr_qty") or 0)
        if cntr_qty <= 0:
            continue
        fills.append(
            FillRecord(
                order_no=str(row.get("ord_no", "")),
                side="SELL" if str(row.get("slby_tp")) == "1" else "BUY",
                fill_price=Decimal(str(row.get("cntr_uv") or "0")),
                fill_qty=cntr_qty,
                fill_time=str(row.get("cntr_time", "")),
                order_status=str(row.get("ord_stat", "")),
            )
        )
    return fills


def get_open_orders(config: Config) -> list[str]:
    """현재 미체결 잔량이 남아있는 주문번호 목록을 조회합니다.

    scheduler.py가 매일 프리장 시작 시점에 그날의 새 주문을 걸기 전, 전날 남아있는
    미체결 주문(특히 매일 새로 거는 지정가매도)을 먼저 정리하는 용도로 사용합니다.
    ord_remnq(주문잔량)가 0보다 큰 것만 "아직 살아있는 미체결 주문"으로 취급합니다.

    미체결 주문이 하나도 없는 것은 매일 있을 수 있는 정상 상황입니다 — 키움 API가
    이 경우를 에러 코드로 응답하더라도(_is_no_data_error 참고) 빈 리스트를 반환합니다.
    """
    try:
        response = _client().fetch_page(api_id=_OPEN_ORDERS_API_ID, path=_ACCOUNT_PATH, body={})
    except APIError as exc:
        if _is_no_data_error(exc):
            return []
        raise KiwoomAdapterError(f"미체결 주문 조회 실패: {exc}") from exc
    except KiwoomError as exc:
        raise KiwoomAdapterError(f"미체결 주문 조회 실패: {exc}") from exc

    rows = response.body.get("result_list") or []
    order_numbers: list[str] = []
    for row in rows:
        remaining_qty = int(row.get("ord_remnq") or 0)
        if remaining_qty > 0:
            order_numbers.append(str(row.get("ord_no", "")))
    return order_numbers


def cancel_all_open_orders(config: Config) -> int:
    """미체결 주문을 모두 취소합니다. 반환값은 취소를 시도한 주문 개수.

    개별 취소가 실패해도(이미 체결/취소된 주문 등) 전체 작업을 중단하지 않고 계속
    진행하며, 실패 건은 경고 로그만 남깁니다 — 오래된 취소 대상 주문 하나 때문에
    나머지 정상 취소까지 막히면 안 되기 때문입니다.
    """
    order_numbers = get_open_orders(config)
    for order_no in order_numbers:
        try:
            cancel_order(config, order_no)
        except KiwoomAdapterError as exc:
            logger.warning("미체결 주문 취소 실패 (order_no=%s): %s", order_no, exc)
    return len(order_numbers)


def get_usd_deposit(config: Config) -> Decimal:
    """USD 외화예수금(주문 가능 현금)을 조회합니다. remaining_cash와 실제 계좌 잔고가
    일치하는지 검증(설계도 11번 불일치 감지)하는 데 사용할 수 있습니다.
    """
    try:
        response = _client().fetch_page(api_id=_DEPOSIT_API_ID, path=_ACCOUNT_PATH, body={})
    except KiwoomError as exc:
        raise KiwoomAdapterError(f"예수금 조회 실패: {exc}") from exc

    rows = response.body.get("result_list") or []
    for row in rows:
        if row.get("crnc_code") == "USD":
            return Decimal(str(row.get("fc_entra") or "0"))
    raise KiwoomAdapterError("응답에서 USD 예수금 정보를 찾을 수 없습니다.")


@dataclass(frozen=True)
class Quote:
    current_price: Decimal
    prev_close: Decimal


def get_quote(config: Config) -> Quote:
    """현재가/전일종가를 조회합니다.

    - 첫매수 미끼주문 가격(설계도 5-1번)의 기준인 "전일 종가"
    - portfolio_summary 갱신(설계도 9-4번)에 쓰는 "현재가"
    - 리버스모드 종료 조건 판정(설계도 7-3번)에 쓰는 "종가"(장중에는 현재가로 근사)
    세 곳 모두 이 함수 하나로 충당합니다.
    """
    body = {"stex_tp": config.exchange_code, "stk_cd": config.ticker}
    try:
        response = _client().fetch_page(api_id=_QUOTE_API_ID, path=_QUOTE_PATH, body=body)
    except KiwoomError as exc:
        raise KiwoomAdapterError(f"현재가 조회 실패: {exc}") from exc

    data = response.body
    return Quote(
        current_price=Decimal(str(data.get("cur_prc") or "0")),
        prev_close=Decimal(str(data.get("base_close_pric") or "0")),
    )


def get_recent_daily_closes(config: Config, *, count: int = 5) -> list[Decimal]:
    """직전 count 거래일의 종가를 "오래된 날짜 -> 최신 날짜" 순서로 반환합니다
    (설계도 7-2번: 리버스모드 별지점 = 직전 5거래일 종가 평균).

    일봉 차트 API는 조회 시작일(strt_dt)부터의 데이터를 반환하므로, 넉넉히 최근 20일치를
    요청한 뒤 날짜 기준 정렬 후 마지막 count개만 취합니다(주말/휴장일 때문에 "5거래일 전"의
    정확한 캘린더 날짜를 미리 계산하기 어렵기 때문).
    """
    lookback_start = (now_et_date_minus_calendar_days(20)).strftime("%Y%m%d")
    body = {
        "stex_tp": config.exchange_code,
        "stk_cd": config.ticker,
        "strt_dt": lookback_start,
        "upd_stkpc_tp": "1",
        "exrt_appl_tp": "0",
    }
    try:
        response = _client().fetch_page(api_id=_DAILY_CHART_API_ID, path=_CHART_PATH, body=body)
    except KiwoomError as exc:
        raise KiwoomAdapterError(f"일봉 차트 조회 실패: {exc}") from exc

    rows = response.body.get("result_list") or []
    parsed = sorted(
        ((str(row.get("dt", "")), Decimal(str(row.get("cur_prc") or "0"))) for row in rows if row.get("dt")),
        key=lambda pair: pair[0],
    )
    if len(parsed) < count:
        raise KiwoomAdapterError(
            f"직전 {count}거래일 종가를 조회하는 데 필요한 데이터가 부족합니다 (조회된 일수: {len(parsed)})."
        )
    return [price for _, price in parsed[-count:]]


@dataclass(frozen=True)
class DailyOHLC:
    """하루치 시가/고가/저가/종가. DRY_RUN 체결 시뮬레이션(dry_run_simulator.py)이
    "그날 실제 가격이었다면 주문이 체결됐을지"를 판정하는 데 씁니다.
    """

    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


def get_recent_daily_ohlc(config: Config, *, count: int = 5) -> list[DailyOHLC]:
    """직전 count 거래일의 시가/고가/저가/종가를 "오래된 날짜 -> 최신 날짜" 순서로
    반환합니다. `get_recent_daily_closes()`와 같은 일봉 차트 API를 쓰되, 종가 외에
    고가/저가/시가까지 함께 돌려줍니다 — DRY_RUN 모드에서 LOC/MOC/지정가 주문이
    "그날 실제로 체결됐을지"를 시뮬레이션하려면 종가뿐 아니라 고가/저가(장중에 지정가를
    스쳤는지)도 필요하기 때문입니다.

    주의: 이 일봉 데이터는 정규장(본장) 기준입니다. 프리마켓/애프터마켓 중 형성된
    가격은 포함하지 않으므로, 지정가매도(프리~애프터 전체 유지)의 시뮬레이션은
    "정규장 중에만 스쳤는지"로 근사한 것이며 실제와 다를 수 있습니다.
    """
    lookback_start = (now_et_date_minus_calendar_days(20)).strftime("%Y%m%d")
    body = {
        "stex_tp": config.exchange_code,
        "stk_cd": config.ticker,
        "strt_dt": lookback_start,
        "upd_stkpc_tp": "1",
        "exrt_appl_tp": "0",
    }
    try:
        response = _client().fetch_page(api_id=_DAILY_CHART_API_ID, path=_CHART_PATH, body=body)
    except KiwoomError as exc:
        raise KiwoomAdapterError(f"일봉 차트(OHLC) 조회 실패: {exc}") from exc

    rows = response.body.get("result_list") or []
    parsed = sorted(
        (
            DailyOHLC(
                trade_date=date(int(str(row["dt"])[:4]), int(str(row["dt"])[4:6]), int(str(row["dt"])[6:8])),
                open=Decimal(str(row.get("open_pric") or "0")),
                high=Decimal(str(row.get("high_pric") or "0")),
                low=Decimal(str(row.get("low_pric") or "0")),
                close=Decimal(str(row.get("cur_prc") or "0")),
            )
            for row in rows
            if row.get("dt")
        ),
        key=lambda bar: bar.trade_date,
    )
    if len(parsed) < count:
        raise KiwoomAdapterError(
            f"직전 {count}거래일 OHLC를 조회하는 데 필요한 데이터가 부족합니다 (조회된 일수: {len(parsed)})."
        )
    return parsed[-count:]


def now_et_date_minus_calendar_days(days: int):
    """get_recent_daily_closes()에서만 쓰는 작은 헬퍼: 미국 동부시간 기준 오늘로부터
    days일 전 날짜를 반환합니다. (거래일이 아니라 달력일 기준으로 넉넉히 앞당겨서
    조회 시작일로 쓰기 위함 — 정확한 거래일 계산이 아니라 "최소 이만큼은 포함되게"가 목적)
    """
    from datetime import timedelta

    from infinite_buying_v4.market_hours import now_et

    return now_et().date() - timedelta(days=days)


def _decode_realtime_fill_event(event: dict[str, Any]) -> FillRecord | None:
    """실시간 웹소켓 이벤트 1건을 FillRecord로 변환합니다. 체결량이 없는(주문 접수 등)
    이벤트는 None을 반환해 무시합니다."""
    fill_qty_raw = event.get("fill_qty")
    try:
        fill_qty = int(fill_qty_raw) if fill_qty_raw not in (None, "") else 0
    except (TypeError, ValueError):
        fill_qty = 0
    if fill_qty <= 0:
        return None
    return FillRecord(
        order_no=str(event.get("order_no", "")),
        side="SELL" if str(event.get("side_code")) == "1" else "BUY",
        fill_price=Decimal(str(event.get("fill_price") or "0")),
        fill_qty=fill_qty,
        fill_time=str(event.get("fill_time", "")),
        order_status=str(event.get("order_status", "")),
    )


async def subscribe_realtime_fills(
    config: Config,
    on_fill: Callable[[FillRecord], Awaitable[None] | None],
) -> None:
    """미국주식 실시간 체결(F5)을 구독하고, 체결 이벤트가 올 때마다 on_fill 콜백을
    호출합니다 (설계도 9-3, 11번: 체결을 실시간으로 확인해 상태 불일치를 빠르게 감지).

    이 함수는 웹소켓 연결이 끊기기 전까지 반환하지 않는 장기 실행 코루틴입니다.
    scheduler.py에서 `asyncio.create_task(subscribe_realtime_fills(...))`로 백그라운드에
    띄워두고, 본 프로세스는 계속 스케줄된 작업을 수행하는 방식을 권장합니다.
    on_fill은 동기 함수/코루틴 함수 둘 다 지원합니다.
    """
    from kiwoom.realtime import run_pubsub

    reg_packet = {
        "trnm": "REG",
        "grp_no": "1",
        "refresh": "1",
        "data": [
            {
                "item": [{"jmcode": config.ticker, "stex_tp": config.exchange_code}],
                "type": [_REALTIME_FILL_TYPE],
            }
        ],
    }

    async def _consumer(queue: "Any") -> None:
        while True:
            event = await queue.get()
            fill = _decode_realtime_fill_event(event)
            if fill is None:
                continue
            result = on_fill(fill)
            if inspect.isawaitable(result):
                await result

    await run_pubsub(
        get_ws_client(),
        api_url=_REALTIME_API_URL,
        bodies=reg_packet,
        consumers={_REALTIME_FILL_TYPE: _consumer},
        columns=_REALTIME_FILL_COLUMNS,
        max_messages=None,  # 무제한 구독 (연결이 끊길 때까지 계속 수신)
    )
