"""
market_hours.py
================
미국 동부시간(America/New_York) 기준 장 시간대(프리장/본장/애프터/주간거래)를 판별하는
모듈입니다 (설계도 6번 주문 타이밍 규칙, 11번 "서머타임/비서머타임 자동판별").

왜 zoneinfo를 쓰는가?
- 설계도 6번은 "지정가매도는 서머타임 17시/비서머타임 18시(한국시간)에 건다"처럼
  서머타임 여부에 따라 두 가지 한국시간을 수동으로 나눠 명시합니다. 이걸 코드로 그대로
  옮기면 "미국 서머타임이 매년 정확히 언제 시작/끝나는지"를 우리가 직접 계산해야 하고,
  규칙이 바뀌면(실제로 미국은 과거 여러 번 서머타임 규정을 바꿨습니다) 코드도 같이
  고쳐야 합니다.
- 대신 파이썬 표준 라이브러리 zoneinfo로 "America/New_York" 타임존을 직접 다루면,
  서머타임 계산은 OS의 tzdata가 대신 해줍니다. 그래서 이 모듈은 "지금이 미국 동부시간으로
  몇 시인가"만 계산하고, 그 결과가 자동으로 서머타임을 반영합니다 — 한국시간 17시/18시
  분기 코드가 아예 필요 없습니다.

주의(중요, 실사용 전 확인 필요):
- "주간거래(데이마켓)" 정확한 운영 시간은 증권사 공지에 따라 바뀔 수 있고, 이 문서
  작성 시점 기준으로 제가 100% 확신할 수 있는 값이 아닙니다. 아래 기본값
  (20:00~04:00 ET, 애프터마켓 종료~프리마켓 시작 사이 오버나이트 구간)은 합리적인
  추정치일 뿐이므로, 실거래 전 반드시 키움증권 공식 공지의 "주간거래 운영시간"을 확인해
  DAY_MARKET_START_ET/DAY_MARKET_END_ET를 맞게 조정하세요.
- 미국 증시 휴장일(추수감사절 등)은 이 모듈이 감지하지 못합니다. 휴장일 캘린더가
  필요하면 별도 데이터 소스 연동이 필요합니다(이번 구현 범위 밖).
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Literal
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")

# --- 정규 장 시간대 경계 (미국 동부시간 기준, 서머타임 여부와 무관하게 항상 이 "현지 시각") ---
PREMARKET_START = time(4, 0)  # 프리장 시작
REGULAR_START = time(9, 30)  # 본장 시작
REGULAR_END = time(16, 0)  # 본장 종료
AFTERHOURS_END = time(20, 0)  # 애프터마켓 종료

# 주간거래(데이마켓) 기본 시간대. 위 "주의" 문단 참고 — 실거래 전 검증 필요.
DEFAULT_DAY_MARKET_START_ET = time(20, 0)
DEFAULT_DAY_MARKET_END_ET = time(4, 0)

Session = Literal["PREMARKET", "REGULAR", "AFTERHOURS", "DAY_MARKET", "CLOSED"]


def now_et() -> datetime:
    """현재 시각을 미국 동부시간(America/New_York, tz-aware)으로 반환합니다."""
    return datetime.now(timezone.utc).astimezone(MARKET_TZ)


def is_dst_now(dt: datetime | None = None) -> bool:
    """현재(또는 주어진 시각)가 미국 서머타임 적용 중인지 반환합니다.

    dt가 naive datetime이면 UTC로 간주합니다. zoneinfo가 tzdata를 기반으로 서머타임
    여부를 자동으로 판단하므로, 이 프로젝트 어디에서도 서머타임 시작/종료 날짜를
    하드코딩하지 않습니다.
    """
    reference = _to_et(dt)
    dst_offset = reference.dst()
    return dst_offset is not None and dst_offset.total_seconds() != 0


def _to_et(dt: datetime | None) -> datetime:
    if dt is None:
        return now_et()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MARKET_TZ)


def _time_in_range(t: time, start: time, end: time) -> bool:
    """t가 [start, end) 구간에 있는지 확인합니다. start > end이면 자정을 넘어가는 구간으로 취급합니다
    (예: 주간거래 20:00~04:00처럼 하루를 걸치는 경우)."""
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def get_session(
    dt: datetime | None = None,
    *,
    day_market_start: time = DEFAULT_DAY_MARKET_START_ET,
    day_market_end: time = DEFAULT_DAY_MARKET_END_ET,
) -> Session:
    """주어진 시각(기본값: 현재)이 어느 장 시간대에 속하는지 판별합니다."""
    et_dt = _to_et(dt)
    t = et_dt.time()

    if _time_in_range(t, day_market_start, day_market_end):
        return "DAY_MARKET"
    if PREMARKET_START <= t < REGULAR_START:
        return "PREMARKET"
    if REGULAR_START <= t < REGULAR_END:
        return "REGULAR"
    if REGULAR_END <= t < AFTERHOURS_END:
        return "AFTERHOURS"
    return "CLOSED"


def is_weekday(dt: datetime | None = None) -> bool:
    """월~금이면 True. (미국 증시 휴장일 캘린더는 별도이므로 이 함수만으로 "거래일"을 완전히
    판정할 수는 없습니다 — 위 모듈 docstring의 "주의" 참고.)"""
    return _to_et(dt).weekday() < 5


def can_place_loc_or_moc_order(dt: datetime | None = None) -> bool:
    """LOC/MOC 매수·매도 주문을 지금 제출해도 되는지 (설계도 6번: "LOC매수/매도: 본장 중 아무 때나 가능").

    본장(REGULAR) 시간대에만 True를 반환합니다. 주간거래 시간대 주문은 설계도 6번이
    명시적으로 금지하므로, 이 함수와 can_place_limit_sell_order() 모두 DAY_MARKET에서는
    무조건 False를 반환하도록 구현했습니다.
    """
    return is_weekday(dt) and get_session(dt) == "REGULAR"


def can_place_limit_sell_order(dt: datetime | None = None) -> bool:
    """지정가매도 주문을 지금 제출/유지해도 되는지 (설계도 6번: "프리장 시작 시각에 걸어서
    프리~본장~애프터까지 유지").

    프리장/본장/애프터 어디에서든 True. 주간거래·완전 장마감 시간대에는 False.
    """
    return is_weekday(dt) and get_session(dt) in ("PREMARKET", "REGULAR", "AFTERHOURS")


def is_day_market_session(dt: datetime | None = None) -> bool:
    """주간거래(데이마켓) 시간대인지 확인합니다. 설계도 6번 "주간거래 시간대 주문 절대 금지"의
    실행 전 시간 체크에 사용합니다.
    """
    return get_session(dt) == "DAY_MARKET"


def is_premarket_start(dt: datetime | None = None, *, tolerance_minutes: int = 1) -> bool:
    """지금이 "프리장 시작 시각"(지정가매도 주문을 새로 거는 트리거 시점)인지 확인합니다.

    스케줄러가 이 함수가 True인 순간에 맞춰 지정가매도 주문 갱신 작업을 트리거합니다.
    정확히 04:00:00 ET에 스케줄러가 실행된다는 보장이 없으므로(cron 지연 등), 기본
    1분의 허용 오차를 둡니다.
    """
    et_dt = _to_et(dt)
    start_minutes = PREMARKET_START.hour * 60 + PREMARKET_START.minute
    current_minutes = et_dt.hour * 60 + et_dt.minute
    return abs(current_minutes - start_minutes) <= tolerance_minutes
