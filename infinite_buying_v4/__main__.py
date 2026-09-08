"""
__main__.py
============
`python -m infinite_buying_v4` 하나로 자동매매 스케줄러와 대시보드를 함께
띄우는 통합 실행 진입점입니다.

원래는 두 프로세스를 각각 따로 띄워야 했습니다:
    python -m infinite_buying_v4.scheduler           (터미널 1)
    python -m infinite_buying_v4.dashboard.server     (터미널 2)

Docker(docker-compose.yml)는 애초에 이 둘을 별도 컨테이너로 분리해서 실행합니다
(한쪽이 죽어도 다른 쪽에 영향이 없고, 서로 다른 재시작 정책을 줄 수 있도록 —
이런 격리가 필요하면 계속 Docker를 쓰세요). 하지만 Docker 없이 로컬 PC에서 직접
실행할 때는 매번 터미널을 두 개 띄우는 게 번거로우므로, 이 모듈이 그 대안을
제공합니다: 대시보드를 백그라운드 스레드로 띄운 뒤, 같은 프로세스에서 스케줄러를
계속 실행합니다. 터미널 창 하나에서 Ctrl+C를 한 번 누르면 스케줄러와 대시보드가
함께 종료됩니다(대시보드 스레드가 daemon=True라 메인 스레드가 끝나면 자동 정리됨).

사용법:
    python -m infinite_buying_v4
"""

from __future__ import annotations

import logging
import threading

from infinite_buying_v4 import scheduler as scheduler_module
from infinite_buying_v4.config import Config, load_config
from infinite_buying_v4.dashboard.server import create_app
from infinite_buying_v4.logging_setup import configure_logging
from infinite_buying_v4.notifier import LogNotifier

logger = logging.getLogger("infinite_buying_v4.main")


def _start_dashboard_thread(config: Config) -> threading.Thread:
    """대시보드(Flask)를 백그라운드 데몬 스레드로 띄웁니다.

    daemon=True로 만들어서, 메인 스레드(스케줄러)가 Ctrl+C 등으로 종료되면 이
    스레드도 프로세스와 함께 자동으로 정리됩니다 — 별도 종료 처리가 필요 없습니다.
    """
    app = create_app(config)

    def _run() -> None:
        # use_reloader=False: Flask의 코드 변경 자동 재시작 기능은 별도 자식
        # 프로세스를 새로 띄우는 방식이라 스레드 안에서는 쓸 수 없습니다(원래도
        # 개발 편의 기능일 뿐). threaded=True: 스케줄러가 백그라운드에서 계속
        # 도는 동안에도 대시보드가 여러 요청(브라우저 폴링 등)을 동시에 처리할 수
        # 있게 합니다.
        app.run(host="0.0.0.0", port=config.dashboard_port, use_reloader=False, threaded=True)

    thread = threading.Thread(target=_run, name="dashboard", daemon=True)
    thread.start()
    return thread


def main() -> None:
    config = load_config()
    configure_logging(config)

    logger.info("대시보드를 백그라운드로 시작합니다: http://localhost:%d", config.dashboard_port)
    _start_dashboard_thread(config)

    logger.info("자동매매 스케줄러를 시작합니다 (Ctrl+C를 누르면 대시보드까지 함께 종료됩니다).")
    notifier = LogNotifier()
    scheduler_module.start_scheduler(config, notifier)


if __name__ == "__main__":
    main()
