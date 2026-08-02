"""
config.py
=========
무한매수법 V4.0 자동매매 프로그램의 모든 환경설정 값을 한 곳에서 관리하는 모듈입니다.

왜 이 모듈이 필요한가?
- 원금, 분할수, 실투자/모의투자 여부, 키움 App Key/Secret 등은 코드에 하드코딩하면 안 되는
  값들입니다(특히 App Secret 같은 민감정보, 원금처럼 사용자마다 다른 값).
- 대신 프로젝트 루트의 `.env` 파일(환경변수)에서 값을 읽어와 하나의 불변(frozen) 설정 객체로
  변환합니다. 이렇게 하면 다른 모든 모듈은 `config.py`만 import해서 값을 쓰면 되고,
  "환경변수를 어디서 어떻게 읽는지"를 몰라도 됩니다.
- 설정값 검증(예: 분할수는 20 또는 40만 허용)도 이 모듈에서 프로그램 시작 시점에 한 번에
  끝내서, 잘못된 설정으로 인한 문제를 실행 초기에 바로 발견하게 합니다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

# python-dotenv: .env 파일의 KEY=VALUE 라인들을 os.environ에 주입해주는 라이브러리.
# 로컬 개발 시에는 .env 파일을 직접 읽고, Docker 운영 환경에서는 보통
# docker-compose의 environment/env_file 설정으로 이미 os.environ에 값이 들어있으므로
# .env 파일이 없어도(load_dotenv가 아무 것도 못 찾아도) 에러 없이 넘어갑니다.
from dotenv import load_dotenv

# 프로젝트 루트(=이 파일의 부모의 부모 디렉터리) 기준으로 .env를 찾습니다.
# infinite_buying_v4/config.py -> infinite_buying_v4/ -> 프로젝트 루트/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# 키움 REST API의 실투자/모의투자 각각의 REST/WebSocket 엔드포인트입니다.
# 공식 저장소(github.com/Kiwoom-Securities/Kiwoom-REST-API)의 .env.example 기준이며,
# 사용자가 임의로 바꿀 이유가 없는 값이라 환경변수가 아닌 코드 상수로 고정합니다.
_REST_BASE_URL = {
    "real": "https://api.kiwoom.com",
    "demo": "https://mockapi.kiwoom.com",
}
_WS_BASE_URL = {
    "real": "wss://api.kiwoom.com:10000",
    "demo": "wss://mockapi.kiwoom.com:10000",
}

# 무한매수법에서 허용하는 분할수는 20 또는 40 두 가지뿐입니다(설계도 1, 3, 4번).
_VALID_SPLIT_COUNTS = (20, 40)


class ConfigError(Exception):
    """환경설정 값이 없거나 잘못된 경우 발생시키는 예외.

    상태 파일과 마찬가지로(설계도 11번 "임의값으로 시작 금지"), 설정값도
    잘못됐는데 임의의 기본값으로 조용히 넘어가면 실제 돈이 걸린 자동매매에서
    치명적인 사고로 이어질 수 있습니다. 그래서 검증 실패 시 무조건 예외를
    던지고 프로그램을 시작하지 않습니다.
    """


def _get_env(name: str, *, required: bool = True, default: str | None = None) -> str | None:
    """환경변수 하나를 읽는 공용 헬퍼.

    required=True인데 값이 없으면 ConfigError를 던져서, "설정 누락"을
    프로그램 시작 시점에 바로 알 수 있게 합니다(나중에 매매 로직 중간에서
    NoneType 에러로 죽는 것보다 훨씬 안전합니다).
    """
    value = os.environ.get(name, default)
    if required and (value is None or value.strip() == ""):
        raise ConfigError(f"필수 환경변수 '{name}'가 설정되지 않았습니다. .env 파일을 확인하세요.")
    return value


def _get_decimal_env(name: str, *, required: bool = True, default: str | None = None) -> Decimal:
    """금액류 환경변수를 Decimal로 읽는 헬퍼.

    설계도 2번에 명시된 대로 금액/T값 계산에는 float 대신 Decimal을 사용해
    누적 반올림 오차를 방지합니다. 환경변수는 항상 문자열이므로 여기서 한 번에
    Decimal 변환 + 검증을 처리합니다.
    """
    raw = _get_env(name, required=required, default=default)
    if raw is None:
        # required=False이고 default도 None인 경우에만 여기 도달합니다.
        raise ConfigError(f"환경변수 '{name}'에 값이 없어 Decimal로 변환할 수 없습니다.")
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigError(f"환경변수 '{name}' 값 '{raw}'을(를) 숫자로 변환할 수 없습니다.") from exc


def _get_bool_env(name: str, *, default: bool) -> bool:
    """"true"/"false"/"1"/"0" 등 문자열을 bool로 변환하는 헬퍼."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


@dataclass(frozen=True)
class Config:
    """프로그램 전역에서 공유하는 불변 설정 객체.

    frozen=True로 선언해 생성 이후에는 값을 바꿀 수 없게 만듭니다. 매매 로직
    중간에 실수로 설정값이 바뀌는 사고를 원천 차단하기 위함입니다.
    """

    # --- 키움 REST API 인증/환경 ---
    kiwoom_mode: Literal["real", "demo"]  # "real"=실투자, "demo"=모의투자
    app_key: str
    app_secret: str
    api_base_url: str
    ws_base_url: str

    # --- 무한매수법 전략 파라미터 ---
    ticker: str  # 매매 대상 종목코드 (기본 TQQQ)
    exchange_code: str  # 키움 REST의 거래소구분 코드 (NA=AMEX, ND=NASDAQ, NY=NYSE)
    split_count: int  # 분할수 (20 또는 40). 기본값 40.
    principal: Decimal  # 총 원금(USD). 사용자가 .env에서 직접 설정.
    compound_on_restart: bool  # 사이클 재시작 시 복리(True) / 단리(False)

    # --- 저장 경로 ---
    db_path: Path  # SQLite DB 파일 경로 (state, trade_history 등)
    event_log_path: Path  # 이벤트 로그 JSONL 파일 경로 (T값 재생용)

    # --- 대시보드 ---
    dashboard_port: int

    def masked_app_secret(self) -> str:
        """로그에 App Secret 전체를 남기면 안 되므로, 마스킹된 값만 노출하는 헬퍼."""
        if len(self.app_secret) <= 4:
            return "*" * len(self.app_secret)
        return f"{self.app_secret[:2]}{'*' * (len(self.app_secret) - 4)}{self.app_secret[-2:]}"


def load_config() -> Config:
    """환경변수를 읽고 검증하여 Config 객체를 생성합니다.

    이 함수는 프로그램(scheduler.py, dashboard/server.py 등) 시작점에서
    딱 한 번 호출하고, 이후에는 반환된 Config 객체를 계속 재사용하는 것을
    권장합니다(환경변수를 매번 다시 읽지 않도록).
    """
    kiwoom_mode = _get_env("KIWOOM_MODE", required=False, default="demo")
    if kiwoom_mode not in ("real", "demo"):
        raise ConfigError(
            f"KIWOOM_MODE는 'real' 또는 'demo'만 허용됩니다 (입력값: '{kiwoom_mode}')."
        )

    # real/demo 모드에 따라 App Key/Secret 환경변수 이름이 다릅니다.
    # (키움 공식 가이드: 운영과 모의투자는 서로 다른 키를 발급받아야 함)
    if kiwoom_mode == "real":
        app_key = _get_env("APP_KEY")
        app_secret = _get_env("APP_SECRET")
    else:
        app_key = _get_env("APP_KEY_MOCK")
        app_secret = _get_env("APP_SECRET_MOCK")

    split_count_raw = _get_env("SPLIT_COUNT", required=False, default="40")
    try:
        split_count = int(split_count_raw)
    except ValueError as exc:
        raise ConfigError(f"SPLIT_COUNT는 정수여야 합니다 (입력값: '{split_count_raw}').") from exc
    if split_count not in _VALID_SPLIT_COUNTS:
        raise ConfigError(
            f"SPLIT_COUNT는 {_VALID_SPLIT_COUNTS} 중 하나여야 합니다 (입력값: {split_count})."
        )

    principal = _get_decimal_env("PRINCIPAL")
    if principal <= 0:
        raise ConfigError(f"PRINCIPAL(원금)은 0보다 커야 합니다 (입력값: {principal}).")

    compound_on_restart = _get_bool_env("COMPOUND_ON_RESTART", default=True)

    ticker = _get_env("TICKER", required=False, default="TQQQ")
    exchange_code = _get_env("EXCHANGE_CODE", required=False, default="ND")
    if exchange_code not in ("NA", "ND", "NY"):
        raise ConfigError(
            f"EXCHANGE_CODE는 'NA'(AMEX)/'ND'(NASDAQ)/'NY'(NYSE) 중 하나여야 합니다 "
            f"(입력값: '{exchange_code}')."
        )

    db_path_raw = _get_env("DB_PATH", required=False, default=str(PROJECT_ROOT / "data" / "infinite_buying.db"))
    event_log_path_raw = _get_env(
        "EVENT_LOG_PATH", required=False, default=str(PROJECT_ROOT / "data" / "event_log.jsonl")
    )

    dashboard_port_raw = _get_env("DASHBOARD_PORT", required=False, default="8000")
    try:
        dashboard_port = int(dashboard_port_raw)
    except ValueError as exc:
        raise ConfigError(
            f"DASHBOARD_PORT는 정수여야 합니다 (입력값: '{dashboard_port_raw}')."
        ) from exc

    return Config(
        kiwoom_mode=kiwoom_mode,  # type: ignore[arg-type]  # 위에서 real/demo로 이미 검증함
        app_key=app_key,  # type: ignore[arg-type]
        app_secret=app_secret,  # type: ignore[arg-type]
        api_base_url=_REST_BASE_URL[kiwoom_mode],
        ws_base_url=_WS_BASE_URL[kiwoom_mode],
        ticker=ticker,  # type: ignore[arg-type]
        exchange_code=exchange_code,
        split_count=split_count,
        principal=principal,
        compound_on_restart=compound_on_restart,
        db_path=Path(db_path_raw),
        event_log_path=Path(event_log_path_raw),
        dashboard_port=dashboard_port,
    )
