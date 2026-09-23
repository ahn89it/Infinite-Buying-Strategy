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
- 이 함수는 콘솔 출력은 그대로 유지하면서, 동시에 파일(`data/logs/app.log`)에도
  같은 로그를 남깁니다. **날짜(KST 자정)마다 파일을 새로 나눕니다**(2026-09-24
  변경 — 예전에는 크기(5MB) 기준으로만 회전해서 하루치 로그가 여러 날과 뒤섞이거나
  반대로 하루치가 여러 파일로 쪼개졌습니다. 로그를 날짜별로 주고받거나 분석할 때
  "그날 파일 하나만" 보면 되도록 바꿨습니다).

왜 KST 자정 기준인가(호스트 시스템 시간대와 무관하게 고정): 이 프로젝트는 노트북마다
시스템 시간대가 다를 수 있고(운영 로그를 보면 컨테이너 타임스탬프가 UTC였음), 운영자는
항상 KST로 로그를 읽습니다(운영 런북의 "실행 시간표"도 KST가 1순위). 그래서
`TimedRotatingFileHandler`의 `when="midnight"`(호스트 로컬시각 자정 기준, 호스트마다
달라질 수 있음)를 그대로 쓰는 대신, `utc=True` + `atTime=15:00`으로 "UTC 15:00마다
회전"하도록 고정했습니다 — KST는 서머타임이 없는 UTC+9 고정 오프셋이라, UTC 15:00은
언제나 정확히 KST 00:00과 같습니다(market_hours.py가 미국 서머타임 때문에 ET를
zoneinfo로 다루는 것과 반대로, KST는 서머타임이 없어 이렇게 고정 오프셋 계산으로 충분).
"""

from __future__ import annotations

import logging
from datetime import time
from logging.handlers import TimedRotatingFileHandler

from infinite_buying_v4.config import Config

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_ROTATE_AT_UTC = time(15, 0)  # UTC 15:00 = KST 00:00 (KST는 서머타임 없는 UTC+9 고정)
_BACKUP_COUNT = 30  # app.log(오늘자) + app.log.YYYY-MM-DD 형식으로 최근 30일치 보관


def configure_logging(config: Config) -> None:
    """루트 로거에 콘솔 핸들러 + 날짜별 회전 파일 핸들러를 설정합니다.

    로그 파일은 `config.db_path`와 같은 데이터 디렉터리 아래 `logs/app.log`에
    저장됩니다(SQLite DB, 이벤트 로그와 마찬가지로 `data/` 볼륨에 함께 영속화되도록).
    오늘자 로그는 항상 `app.log`이고, KST 자정이 지나면 그 시점까지의 내용이
    `app.log.2026-09-23`처럼 날짜가 붙은 파일로 이름이 바뀌면서 `app.log`가 새로
    시작됩니다.

    여러 진입점에서 중복 호출해도(예: __main__.py가 대시보드/스케줄러를 한 프로세스에서
    함께 띄우는 경우) 핸들러가 계속 누적되지 않도록, 설정 전에 기존 핸들러를 제거합니다.
    """
    log_dir = config.db_path.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "app.log"

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
