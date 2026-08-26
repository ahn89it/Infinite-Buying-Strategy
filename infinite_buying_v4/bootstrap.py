"""
bootstrap.py
=============
무한매수법을 "완전히 신규로" 시작할 때 딱 1회 사람이 직접 실행하는 스크립트입니다.

왜 별도 스크립트로 분리했는가?
- state.py의 load_state()는 상태가 없으면 무조건 예외를 던지도록 설계했습니다
  (설계도 1번: "임의값으로 시작하지 않도록"). scheduler.py의 일상적인 자동 실행
  경로는 이 규칙을 절대 우회하지 않습니다.
- 그래서 "지금부터 이 계좌로 무한매수법을 시작한다"는 의사결정은, 자동으로 실행되는
  스케줄러가 아니라 사람이 이 스크립트를 명시적으로 실행하는 행위로만 이루어지게
  했습니다. 실수로 두 번 실행해도 state.bootstrap_new_state()가 "이미 상태가 있다"며
  예외를 던지므로 기존 데이터를 덮어쓰지 않습니다.

사용법 (프로젝트 루트에서):
    python -m infinite_buying_v4.bootstrap

.env에 APP_KEY, APP_SECRET, SPLIT_COUNT, PRINCIPAL 등이 이미 설정되어 있어야 합니다
(config.py 참고). 키움 모의투자는 해외주식을 지원하지 않으므로 이 프로젝트는 항상
실투자 API를 사용합니다 — 실주문 없이 먼저 점검하려면 .env에 DRY_RUN=true를
설정한 뒤 scheduler.py를 실행해 로그를 확인하세요(이 bootstrap 스크립트 자체는
주문을 내지 않으므로 DRY_RUN과 무관하게 항상 안전합니다). 실행하면:
    1) state 테이블에 T=0, 보유 0, 사이클 1번 상태를 생성하고
    2) cycle_summary에 사이클 1번의 "진행 중" 행을 만들고
    3) portfolio_summary 싱글턴 행을 초기화합니다.
"""

from __future__ import annotations

import logging
from datetime import date

from infinite_buying_v4 import db, trade_history
from infinite_buying_v4.config import load_config
from infinite_buying_v4.market_hours import now_et
from infinite_buying_v4.state import bootstrap_new_state, state_exists

logger = logging.getLogger("infinite_buying_v4.bootstrap")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    config = load_config()
    conn = db.get_connection(config.db_path)
    try:
        if state_exists(conn):
            raise SystemExit(
                "이미 상태가 존재합니다. 신규 시작은 상태가 전혀 없는 계좌에서만 허용됩니다. "
                "정말 처음부터 다시 시작하려면 기존 DB 파일을 별도로 백업/이동한 뒤 다시 실행하세요."
            )

        # 오늘(미국 동부시간 기준)을 사이클 시작일로 사용합니다. 이 프로젝트의 모든 "거래일"
        # 판단은 market_hours.py를 통해 미국 동부시간 기준으로 통일합니다.
        start_date = now_et().date()

        state = bootstrap_new_state(
            conn,
            split_count=config.split_count,
            principal=config.principal,
            start_date=start_date,
        )
        trade_history.open_cycle_summary(conn, cycle_id=state.cycle_id, start_date=start_date)
        trade_history.ensure_portfolio_summary(
            conn, strategy_start_date=start_date, initial_principal=config.principal
        )

        logger.info(
            "무한매수법 신규 시작 완료: split_count=%d, principal=%s, ticker=%s, dry_run=%s, start_date=%s",
            state.split_count,
            state.principal,
            config.ticker,
            config.dry_run,
            start_date.isoformat(),
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
