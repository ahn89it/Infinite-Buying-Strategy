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
  같은 로그를 남깁니다. 파일이 무한정 커지지 않도록 일정 크기(5MB)마다 회전(rotate)
  하고 최근 5개 파일만 보관합니다.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from infinite_buying_v4.config import Config

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_MAX_BYTES = 5_000_000  # 로그 파일 1개당 최대 약 5MB
_BACKUP_COUNT = 5  # app.log, app.log.1, ... app.log.5 까지 보관 (그 이전 것은 자동 삭제)


def configure_logging(config: Config) -> None:
    """루트 로거에 콘솔 핸들러 + 회전 파일 핸들러를 설정합니다.

    로그 파일은 `config.db_path`와 같은 데이터 디렉터리 아래 `logs/app.log`에
    저장됩니다(SQLite DB, 이벤트 로그와 마찬가지로 `data/` 볼륨에 함께 영속화되도록).

    여러 진입점에서 중복 호출해도(예: __main__.py가 대시보드/스케줄러를 한 프로세스에서
    함께 띄우는 경우) 핸들러가 계속 누적되지 않도록, 설정 전에 기존 핸들러를 제거합니다.
    """
    log_dir = config.db_path.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "app.log"

    formatter = logging.Formatter(_LOG_FORMAT)

    file_handler = RotatingFileHandler(log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)
