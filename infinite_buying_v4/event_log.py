"""
event_log.py
=============
체결 이벤트를 append-only JSONL 파일에 기록하고, 그 이벤트들을 순서대로 "재생(replay)"해서
T값을 갱신하는 모듈입니다 (설계도 2번, 이벤트 소싱 패턴).

trade_history.py(SQLite, 사람이 보는 이력)와의 차이:
- 이 모듈의 목적은 오직 하나, "T값이 왜 지금 이 값인지"를 언제든 처음부터 다시 계산해서
  검증할 수 있게 하는 것입니다. 그래서 SQLite의 UPDATE 가능한 테이블이 아니라, 파일 끝에만
  추가되는(append-only) JSONL을 씁니다 — 중간 값을 실수로 고쳐써서 재생 결과가 오염되는
  사고를 구조적으로 방지합니다.
- state.py에 저장된 T값은 "매일 장 마감 후, 그날 체결 내역을 이 모듈로 재생해서 얻은 결과"를
  캐싱해둔 것일 뿐, 진짜 원천 데이터(source of truth)는 이 이벤트 로그입니다.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

# --- T값 갱신 규칙에 등장하는 이벤트 종류 (설계도 2번 표 그대로) ---
EVENT_FULL_BUY = "FULL_BUY"  # 1회 매수 완료 (해당 회차 매수금 전액 체결) -> T += 1
EVENT_HALF_BUY = "HALF_BUY"  # 절반 매수 체결 (전반전 매수의 절반 금액만 체결) -> T += 0.5
EVENT_QUARTER_SELL = "QUARTER_SELL"  # 쿼터매도 (보유수량의 1/4을 별지점에서 매도) -> T *= 0.75
# 지정가매도 체결 후 같은 날 LOC매수까지 체결된 경우. 그날 재매수가 "1회 완료분"이었는지
# "절반분"이었는지에 따라 계수가 다르므로 이벤트를 둘로 나눕니다.
EVENT_LIMIT_SELL_THEN_LOC_BUY_FULL = "LIMIT_SELL_THEN_LOC_BUY_FULL"  # T = T*0.25 + 1
EVENT_LIMIT_SELL_THEN_LOC_BUY_HALF = "LIMIT_SELL_THEN_LOC_BUY_HALF"  # T = T*0.25 + 0.5

_VALID_EVENT_TYPES = (
    EVENT_FULL_BUY,
    EVENT_HALF_BUY,
    EVENT_QUARTER_SELL,
    EVENT_LIMIT_SELL_THEN_LOC_BUY_FULL,
    EVENT_LIMIT_SELL_THEN_LOC_BUY_HALF,
)


class EventLogError(Exception):
    """이벤트 로그 기록/재생 중 문제가 발생했을 때(손상된 줄, 알 수 없는 이벤트 종류 등) 발생시키는 예외."""


@dataclass(frozen=True)
class Event:
    """체결 이벤트 1건.

    price/qty/note는 T값 계산에는 쓰이지 않지만, 나중에 "이 이벤트가 정확히 뭐였는지"를
    사람이 다시 확인할 수 있도록 감사(audit) 목적으로 함께 남깁니다.
    """

    timestamp: str  # ISO8601 (UTC), 이벤트가 기록된 시각
    event_date: str  # YYYY-MM-DD, 체결일 (재생 시 "그날 순서대로"를 판단하는 기준)
    event_type: str  # 위 EVENT_* 상수 중 하나
    price: str | None = None  # Decimal을 JSON으로 남기기 위해 문자열로 저장
    qty: int | None = None
    note: str | None = None

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Event":
        return Event(
            timestamp=data["timestamp"],
            event_date=data["event_date"],
            event_type=data["event_type"],
            price=data.get("price"),
            qty=data.get("qty"),
            note=data.get("note"),
        )


def make_event(
    *,
    event_date: date,
    event_type: str,
    price: Decimal | None = None,
    qty: int | None = None,
    note: str | None = None,
) -> Event:
    """이벤트 객체를 생성합니다. 잘못된 event_type을 미리 걸러 오타로 인한 재생 오류를 방지합니다."""
    if event_type not in _VALID_EVENT_TYPES:
        raise EventLogError(f"알 수 없는 event_type입니다: {event_type!r}")
    return Event(
        timestamp=datetime.now(timezone.utc).isoformat(),
        event_date=event_date.isoformat(),
        event_type=event_type,
        price=str(price) if price is not None else None,
        qty=qty,
        note=note,
    )


def append_event(path: Path, event: Event) -> None:
    """이벤트를 JSONL 파일 끝에 한 줄 추가합니다(append-only).

    파일이 없으면 새로 만듭니다. 중간 줄을 수정/삭제하는 함수는 이 모듈에 의도적으로
    두지 않았습니다 — 이벤트 로그는 사실 그대로의 기록이어야 하기 때문입니다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(event.to_json_line() + "\n")


def read_all_events(path: Path) -> list[Event]:
    """이벤트 로그 파일 전체를 읽어 시간 순서대로 반환합니다.

    파일이 아직 없으면(최초 실행 전) 빈 리스트를 반환합니다 — 이벤트가 하나도 없는 것은
    "정상적인 신규 시작" 상태이므로 예외가 아닙니다(state.py의 "상태 파일 없음"과는 다른 경우:
    여기서는 state.py가 이미 bootstrap을 통해 T=0 상태를 명시적으로 만들어 두었다는 전제).
    줄이 손상되어(JSON 파싱 실패) 있으면 이건 데이터 무결성 문제이므로 EventLogError를 던집니다.
    """
    if not path.exists():
        return []
    events: list[Event] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                events.append(Event.from_dict(data))
            except (json.JSONDecodeError, KeyError) as exc:
                raise EventLogError(
                    f"이벤트 로그 {path} 의 {line_no}번째 줄이 손상되었습니다: {exc}"
                ) from exc
    return events


def read_events_on_date(path: Path, target_date: date) -> list[Event]:
    """특정 날짜에 발생한 이벤트만 시간 순서대로 반환합니다.

    scheduler.py가 "오늘 장 마감 후 체결 내역을 재생"할 때 이 함수로 오늘자 이벤트만 뽑아 씁니다.
    """
    target = target_date.isoformat()
    return [e for e in read_all_events(path) if e.event_date == target]


def apply_event(t: Decimal, event_type: str) -> Decimal:
    """이벤트 하나가 T값에 미치는 영향을 계산합니다 (설계도 2번 표를 그대로 구현).

    이 함수는 순수함수입니다 — 같은 (t, event_type) 입력이면 항상 같은 결과를 냅니다.
    """
    if event_type == EVENT_FULL_BUY:
        return t + Decimal(1)
    if event_type == EVENT_HALF_BUY:
        return t + Decimal("0.5")
    if event_type == EVENT_QUARTER_SELL:
        return t * Decimal("0.75")
    if event_type == EVENT_LIMIT_SELL_THEN_LOC_BUY_FULL:
        return t * Decimal("0.25") + Decimal(1)
    if event_type == EVENT_LIMIT_SELL_THEN_LOC_BUY_HALF:
        return t * Decimal("0.25") + Decimal("0.5")
    raise EventLogError(f"알 수 없는 event_type입니다: {event_type!r}")


def replay(t0: Decimal, events: list[Event]) -> Decimal:
    """T값 t0에서 시작해 events를 순서대로(리스트 순서 그대로) 적용한 최종 T값을 반환합니다.

    events는 반드시 실제 체결이 일어난 시간 순서(오래된 것 -> 최신)로 정렬되어 전달되어야
    합니다. read_all_events()/read_events_on_date()는 파일에 쓰여진 순서(=append된 순서=
    시간 순서)를 그대로 유지하므로 별도 정렬 없이 바로 넘기면 됩니다.
    """
    t = t0
    for event in events:
        t = apply_event(t, event.event_type)
    return t
