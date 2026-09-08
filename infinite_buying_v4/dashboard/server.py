"""
dashboard/server.py
====================
로컬 웹 대시보드 서버입니다 (설계도 12번).

- 별도 서버 구축 없이 가장 가벼운 방식으로: Flask로 정적 HTML 한 페이지(static/index.html)와
  JSON API(/api/dashboard) 하나만 제공합니다(설계도 12-1번).
- 데이터는 SQLite(trade_history/portfolio_summary)를 읽기 전용으로 조회하기만 합니다.
  실제 조회/집계 로직은 전부 data.py에 있고, 이 파일은 HTTP 라우팅만 담당합니다.
- scheduler.py(자동매매 프로세스)와 완전히 별도 프로세스로 실행합니다. 같은 SQLite
  파일을 동시에 읽기만 하므로 서로 간섭하지 않습니다(쓰기는 scheduler.py만 수행).

실행 방법:
    python -m infinite_buying_v4.dashboard.server
    (또는 Docker에 별도 서비스로 등록 — docker-compose.yml 참고)

브라우저에서 http://localhost:<DASHBOARD_PORT>/ 로 접속하면 됩니다. 기본적으로
0.0.0.0에 바인딩하므로, 같은 공유기 내부망의 다른 기기(스마트폰 등)에서도
"이 PC의 사설 IP:포트"로 접속할 수 있습니다(설계도 12-1번 "집 공유기 내부망에
열어두는 정도로 충분"). 외부 인터넷에 노출하거나 인증을 추가하는 것은 설계도가
명시한 범위 밖이므로 이 서버 자체에는 구현하지 않았습니다 — 필요하면 리버스
프록시(nginx 등)에서 처리하세요.
"""

from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask, Response, jsonify, send_from_directory

from infinite_buying_v4 import db
from infinite_buying_v4.config import Config, load_config
from infinite_buying_v4.dashboard.data import build_dashboard_payload
from infinite_buying_v4.market_hours import now_et

logger = logging.getLogger("infinite_buying_v4.dashboard")

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(config: Config | None = None) -> Flask:
    """Flask 앱을 생성합니다. config를 인자로 받아 테스트에서 임시 DB를 주입할 수 있게 했습니다."""
    config = config or load_config()
    app = Flask(__name__, static_folder=None)  # 정적 파일은 아래 라우트에서 직접 서빙

    @app.get("/")
    def index() -> Response:
        return send_from_directory(_STATIC_DIR, "index.html")

    @app.get("/api/dashboard")
    def api_dashboard() -> Response:
        # 요청마다 짧게 열고 닫는 읽기 전용 커넥션을 씁니다. scheduler.py의 장기 실행
        # 커넥션과는 별도 프로세스이므로 서로 간섭하지 않습니다(SQLite는 여러 프로세스의
        # 동시 읽기를 지원). 매번 스키마를 CREATE TABLE IF NOT EXISTS로 재확인하는 비용은
        # 개인용 대시보드의 폴링 주기(수십 초)에서는 무시할 수 있는 수준입니다.
        with db.connect(config.db_path) as conn:
            payload = build_dashboard_payload(conn, today=now_et().date())
        return jsonify(payload)

    return app


def main() -> None:
    config = load_config()
    from infinite_buying_v4.logging_setup import configure_logging

    configure_logging(config)  # 콘솔 + data/logs/app.log 파일에 동시 기록
    app = create_app(config)
    logger.info("대시보드 서버 시작: http://0.0.0.0:%d (Ctrl+C로 종료)", config.dashboard_port)
    app.run(host="0.0.0.0", port=config.dashboard_port)


if __name__ == "__main__":
    main()
