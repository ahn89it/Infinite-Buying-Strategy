"""
logging_setup.py
=================
모든 실행 진입점(scheduler.py, dashboard/server.py, bootstrap.py, __main__.py)이
공유하는 로깅 설정입니다.

왜 필요한가?
- 지금까지 각 진입점은 `logging.basicConfig()`로 콘솔에만 로그를 남겼습니다. 터미널
  창을 닫거나 컴퓨터를 재시작하면 그 로그는 그대로 사라집니다. 자동매매처럼 "오늘
  무슨 일이 있었는지"를 나중에 다시 확인해야 하는 프로그램에서는 콘솔 출력만으로
  부족합니다(거래 자체는 SQLite/이벤트 로그에 남지만, "몇 시에 무슨 판단을 했는지"
  같은 실행 로그는 별도로 저장해야 함).
- 이 함수는 콘솔 출력은 그대로 유지하면서, 동시에 파일(`data/logs/<component>.log`)에도
  같은 로그를 남깁니다. **날짜(KST 자정)마다 파일을 새로 나눕니다**(2026-09-24 추가).

왜 KST 자정 기준인가(호스트 시스템 시간대와 무관하게 고정): 이 프로젝트는 노트북마다
시스템 시간대가 다를 수 있고(운영 로그를 보면 컨테이너 타임스탬프가 UTC였음), 운영자는
항상 KST로 로그를 읽습니다(운영 런북의 "실행 시간표"도 KST가 1순위). 그래서
`TimedRotatingFileHandler`의 `when="midnight"`(호스트 로컬시각 자정 기준, 호스트마다
달라질 수 있음)를 그대로 쓰는 대신, `utc=True` + `atTime=15:00`으로 "UTC 15:00마다
회전"하도록 고정했습니다 — KST는 서머타임이 없는 UTC+9 고정 오프셋이라, UTC 15:00은
언제나 정확히 KST 00:00과 같습니다(market_hours.py가 미국 서머타임 때문에 ET를
zoneinfo로 다루는 것과 반대로, KST는 서머타임이 없어 이렇게 고정 오프셋 계산으로 충분).

**왜 호출부마다 다른 `component` 이름(=다른 파일)을 꼭 지정해야 하는가 (2026-10-06
치명적 버그 수정)**: `scheduler.py`와 `dashboard/server.py`는 docker-compose에서
**서로 다른 컨테이너(=서로 다른 OS 프로세스)**로 실행됩니다. 예전에는 두 프로세스가
전부 똑같이 `data/logs/app.log` 하나를 가리키는 **별도의 `TimedRotatingFileHandler`
인스턴스**를 각자 만들었습니다 — `TimedRotatingFileHandler`/`RotatingFileHandler`는
파이썬 공식 문서가 명시하듯 "같은 파일을 여러 프로세스가 동시에 쓰는 상황"에는 안전하지
않습니다(회전 시 파일을 rename하는데, 두 프로세스가 서로 다른 시점에 각자 회전을
시도하면서 서로의 파일 핸들이 "회전되어 이름이 바뀐 옛 파일"이나 "아직 존재하지 않는
새 파일"을 향하게 되는 경쟁 상태가 생김). 실제로 이로 인해 로그 파일에 수만 바이트의
NUL(`\x00`) 바이트가 끼어들거나, 특정 날짜 파일에 다음날 로그까지 섞여 들어가는 손상이
발생했고, 그 결과 "스케줄러가 갑자기 조용해졌다"처럼 보이는 상황(실제로는 스케줄러가
계속 실행 중이었는데 그 로그가 깨진 파일 어딘가에 묻혀 있었거나 사라진 것)이 여러 번
재현됐습니다. 지금은 프로세스(=호출부)마다 **완전히 다른 파일**에 쓰도록 강제해서,
한 프로세스의 쓰기/회전이 다른 프로세스의 파일에 절대 영향을 주지 않습니다 — 이 방식은
공식 로깅 쿡북이 권장하는 "프로세스마다 별도 로그 파일" 해법입니다. `__main__.py`
(대시보드+스케줄러를 한 프로세스 안에서 함께 띄우는 모드)는 실제로 프로세스가 하나뿐이라
하나의 파일을 공유해도 안전합니다.
"""

from __future__ import annotations

import logging
from datetime import time
from logging.handlers import TimedRotatingFileHandler

from infinite_buying_v4.config import Config

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_ROTATE_AT_UTC = time(15, 0)  # UTC 15:00 = KST 00:00 (KST는 서머타임 없는 UTC+9 고정)
_BACKUP_COUNT = 30  # <component>.log(오늘자) + <component>.log.YYYY-MM-DD 형식으로 최근 30일치 보관


def configure_logging(config: Config, component: str) -> None:
    """루트 로거에 콘솔 핸들러 + 날짜별 회전 파일 핸들러를 설정합니다.

    `component`: 이 프로세스를 식별하는 짧은 이름(예: "scheduler", "dashboard",
    "bootstrap", "app"). 로그 파일은 `config.db_path`와 같은 데이터 디렉터리 아래
    `logs/<component>.log`에 저장됩니다. **반드시 프로세스마다 서로 다른 값을
    넘겨야 합니다** — 같은 파일을 여러 프로세스가 동시에 쓰면 로그가 손상됩니다
    (위 모듈 docstring의 2026-10-06 버그 수정 설명 참고). 오늘자 로그는 항상
    `<component>.log`이고, KST 자정이 지나면 그 시점까지의 내용이
    `<component>.log.2026-09-23`처럼 날짜가 붙은 파일로 이름이 바뀌면서
    `<component>.log`가 새로 시작됩니다.

    여러 진입점에서 중복 호출해도(예: __main__.py가 대시보드/스케줄러를 한 프로세스에서
    함께 띄우는 경우) 핸들러가 계속 누적되지 않도록, 설정 전에 기존 핸들러를 제거합니다.
    """
    log_dir = config.db_path.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{component}.log"

    formatter = logging.Formatter(_LOG_FORMAT)

    file_handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        atTime=_ROTATE_AT_UTC,
        utc=True,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)
