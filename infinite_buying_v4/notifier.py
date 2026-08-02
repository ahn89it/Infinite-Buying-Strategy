"""
notifier.py
============
자동매매 중단/이상 상황을 사람에게 알리는 알림 인터페이스입니다 (설계도 11번:
"체결 내역과 로컬 상태 불일치 감지 시 자동매매 중단 + 알림(텔레그램/이메일 등)").

현재 이 파일에는 실제 알림 채널(텔레그램 봇, 이메일 SMTP 등) 구현체가 들어있지 않습니다.
사용자가 어떤 채널을 쓸지 아직 정하지 않았기 때문에, 지금은 "인터페이스 + 로그 기록용
기본 구현체(LogNotifier)"만 두었습니다. scheduler.py를 비롯한 다른 모든 모듈은 반드시
NotifierBase 타입에만 의존하도록 작성되어 있으므로, 나중에 TelegramNotifier나
EmailNotifier를 이 파일에 추가하기만 하면 다른 코드를 전혀 건드리지 않고 알림 채널을
확장할 수 있습니다.

    # 나중에 이런 식으로 추가하면 됩니다 (지금은 구현하지 않음):
    # class TelegramNotifier(NotifierBase):
    #     def __init__(self, bot_token: str, chat_id: str) -> None: ...
    #     def notify(self, level, title, message) -> None: ...
"""

from __future__ import annotations

import abc
import logging
from enum import Enum

logger = logging.getLogger("infinite_buying_v4.notifier")


class NotifyLevel(str, Enum):
    """알림의 심각도. scheduler.py가 상황에 맞는 레벨을 선택해서 호출합니다."""

    INFO = "INFO"  # 정상적인 진행 상황 보고 (예: 오늘 주문 몇 건 제출함)
    WARNING = "WARNING"  # 주의가 필요하지만 자동매매를 즉시 멈추지는 않는 상황
    CRITICAL = "CRITICAL"  # 자동매매를 즉시 중단해야 하는 심각한 문제
    # (설계도 11번: 체결/상태 불일치, 반복적인 주문 실패, 상태 파일 손상 등)


class NotifierBase(abc.ABC):
    """모든 알림 채널 구현체가 따라야 하는 공통 인터페이스."""

    @abc.abstractmethod
    def notify(self, level: NotifyLevel, title: str, message: str) -> None:
        """알림 1건을 전송(또는 기록)합니다. 구현체가 전송에 실패하더라도, 이 메서드를
        호출한 쪽(scheduler.py)의 자동매매 중단 로직 자체는 반드시 계속 진행되어야
        하므로, 구현체 내부에서 예외를 삼키고 로컬 로그에 남기는 방식을 권장합니다
        (알림 전송 실패가 "자동매매를 중단하지 못하는" 사고로 이어지면 안 됨).
        """

    def notify_critical(self, title: str, message: str) -> None:
        """CRITICAL 알림 전송의 축약형. 설계도 11번 "자동매매 중단 + 알림" 상황에서 사용합니다."""
        self.notify(NotifyLevel.CRITICAL, title, message)

    def notify_warning(self, title: str, message: str) -> None:
        self.notify(NotifyLevel.WARNING, title, message)

    def notify_info(self, title: str, message: str) -> None:
        self.notify(NotifyLevel.INFO, title, message)


class LogNotifier(NotifierBase):
    """실제 채널(텔레그램/이메일) 구현 전까지 사용하는 기본 알림 구현체.

    표준 파이썬 logging으로만 기록합니다. Docker 환경에서는 컨테이너 로그(stdout)로
    나가므로 `docker logs`로 확인할 수 있지만, 사람이 실시간으로 알림을 놓치지 않으려면
    반드시 실제 채널 구현체(TelegramNotifier 등)로 교체해야 합니다. CRITICAL 알림은
    운영 중 놓치면 안 되므로, LogNotifier를 계속 쓰는 것은 임시 방편(placeholder)임을
    명확히 인지하고 사용하세요.
    """

    _LEVEL_TO_LOGGING = {
        NotifyLevel.INFO: logging.INFO,
        NotifyLevel.WARNING: logging.WARNING,
        NotifyLevel.CRITICAL: logging.CRITICAL,
    }

    def notify(self, level: NotifyLevel, title: str, message: str) -> None:
        logging_level = self._LEVEL_TO_LOGGING[level]
        logger.log(logging_level, "[%s] %s - %s", level.value, title, message)
