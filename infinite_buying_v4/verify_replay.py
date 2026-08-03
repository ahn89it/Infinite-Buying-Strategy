"""
verify_replay.py
==================
event_log.jsonl을 처음부터(현재 사이클 시작일부터) 다시 재생해서, 그 결과가 state
테이블에 저장된 현재 T값과 일치하는지 검증하는 CLI 도구입니다.

왜 필요한가?
- state.py/event_log.py 문서에는 "이벤트 로그를 재생하면 T값을 언제든 재검증할 수
  있다"고 적혀있지만, 그 재생을 실제로 실행하는 명령이 없으면 장애 상황에서 아무
  쓸모가 없습니다. 이 스크립트가 바로 그 "실제로 실행하는 절차"입니다.
- 운영 런북 문서의 "T값 불일치 복구" 절차가 이 스크립트를 전제로 작성되어 있습니다.

사용법 (프로젝트 루트에서):
    # 1) 읽기 전용 검증만 (기본값, 아무것도 쓰지 않음)
    python -m infinite_buying_v4.verify_replay

    # 2) 불일치 시 무엇으로 고칠지 미리보기 (여전히 아무것도 쓰지 않음)
    python -m infinite_buying_v4.verify_replay --repair

    # 3) 실제로 state.t를 재생 결과로 덮어써서 복구 (파괴적 작업 -- 반드시 --repair와 함께)
    python -m infinite_buying_v4.verify_replay --repair --apply

종료 코드: 0 = 일치(정상), 1 = 불일치 발견(복구 필요), 2 = 실행 자체가 실패(설정/DB 오류 등).
이 종료 코드로 헬스체크나 알림 스크립트에서 그대로 활용할 수 있습니다.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from decimal import Decimal

from infinite_buying_v4 import db
from infinite_buying_v4.config import load_config
from infinite_buying_v4.event_log import Event, EventLogError, read_all_events, replay
from infinite_buying_v4.state import State, StateError, load_state, save_state

logger = logging.getLogger("infinite_buying_v4.verify_replay")


def compute_replayed_t(events: list[Event], cycle_start_date: date) -> tuple[Decimal, list[Event]]:
    """전체 이벤트 로그에서 "현재 사이클"에 해당하는 이벤트만 추려 T=0부터 재생합니다.

    순수함수입니다(파일/DB 접근 없음) — 단위테스트가 쉽습니다. 사이클이 시작되면 T가
    0으로 리셋되므로(설계도 8번), event_date >= cycle_start_date인 이벤트만 대상으로
    삼습니다. event_log.jsonl은 여러 사이클에 걸친 전체 이력을 한 파일에 이어서
    담고 있으므로, 이 필터링이 없으면 이전 사이클의 이벤트까지 잘못 재생하게 됩니다.
    """
    cycle_events = [e for e in events if e.event_date >= cycle_start_date.isoformat()]
    replayed_t = replay(Decimal(0), cycle_events)
    return replayed_t, cycle_events


def _print_trace(cycle_events: list[Event]) -> None:
    from infinite_buying_v4.event_log import apply_event

    t = Decimal(0)
    print(f"{'날짜':<12} {'이벤트':<32} {'T(이전)':>14} -> {'T(이후)':<14}")
    print("-" * 80)
    for event in cycle_events:
        before = t
        t = apply_event(t, event.event_type)
        print(f"{event.event_date:<12} {event.event_type:<32} {str(before):>14} -> {str(t):<14}")


def run_verification(*, repair: bool, apply_fix: bool) -> int:
    try:
        config = load_config()
        conn = db.get_connection(config.db_path)
    except Exception as exc:  # noqa: BLE001 - CLI 진입점이므로 모든 실패를 종료코드로 변환
        print(f"[오류] 설정/DB 로드 실패: {exc}", file=sys.stderr)
        return 2

    try:
        state: State = load_state(conn)
    except StateError as exc:
        print(f"[오류] 상태 로드 실패: {exc}", file=sys.stderr)
        return 2

    try:
        events = read_all_events(config.event_log_path)
    except EventLogError as exc:
        print(f"[오류] 이벤트 로그 읽기 실패(파일 손상 의심): {exc}", file=sys.stderr)
        print("      운영 런북의 'JSONL 손상 복구' 절차를 참고하세요.", file=sys.stderr)
        return 2

    replayed_t, cycle_events = compute_replayed_t(events, state.cycle_start_date)

    print(f"사이클 ID: {state.cycle_id} (시작일: {state.cycle_start_date.isoformat()})")
    print(f"이 사이클의 이벤트 수: {len(cycle_events)} / 전체 이벤트 로그: {len(events)}")
    print()
    _print_trace(cycle_events)
    print()
    print(f"저장된 state.t : {state.t}")
    print(f"재생된 T       : {replayed_t}")

    if replayed_t == state.t:
        print("\n[일치] 저장된 T값이 이벤트 로그 재생 결과와 정확히 일치합니다.")
        return 0

    print("\n[불일치!] 저장된 T값과 이벤트 로그 재생 결과가 다릅니다.")
    print(f"          차이: {state.t - replayed_t} (저장값 - 재생값)")

    if not repair:
        print("\n--repair 옵션 없이 실행되어 아무것도 수정하지 않았습니다.")
        return 1

    print(f"\n[복구 미리보기] state.t를 {state.t} -> {replayed_t} 로 수정합니다.")
    if not apply_fix:
        print("--apply가 없어 실제로 반영하지 않았습니다. 정말 반영하려면 --repair --apply로 다시 실행하세요.")
        return 1

    from dataclasses import replace as dataclasses_replace

    fixed_state = dataclasses_replace(state, t=replayed_t)
    save_state(conn, fixed_state)
    print(f"[복구 완료] state.t를 {replayed_t}로 갱신했습니다.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="event_log.jsonl을 재생해 현재 사이클의 T값이 state와 일치하는지 검증합니다."
    )
    parser.add_argument(
        "--repair", action="store_true", help="불일치 시 무엇으로 고칠지 계산해서 보여줍니다(기본은 미반영)."
    )
    parser.add_argument(
        "--apply", action="store_true", help="--repair와 함께 사용 시, 계산된 값을 실제로 state에 반영합니다."
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    return run_verification(repair=args.repair, apply_fix=args.apply)


if __name__ == "__main__":
    sys.exit(main())
