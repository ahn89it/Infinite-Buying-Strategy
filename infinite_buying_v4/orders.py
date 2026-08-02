"""
orders.py
=========
normal_mode.py와 reverse_mode.py가 공통으로 사용하는 "주문 의도(OrderIntent)" 자료구조를
담은 작은 공유 모듈입니다.

왜 별도 파일로 분리했는가?
- normal_mode.py와 reverse_mode.py는 서로의 내부 구현을 몰라야 하는 독립된 모듈입니다
  (일반모드/리버스모드는 서로 다른 상황에서만 활성화되고 동시에 실행되지 않음).
  하지만 둘 다 "주문을 생성해서 kiwoom_adapter.py에 넘긴다"는 동일한 인터페이스가
  필요하므로, 그 인터페이스(OrderIntent)만 이 작은 공유 모듈에 두고 둘 다 여기서
  가져다 씁니다. 이렇게 하면 reverse_mode.py가 normal_mode.py를 import하거나 그
  반대로 import하는 순환 의존을 피할 수 있습니다.
- kiwoom_adapter.py는 이 OrderIntent를 받아서 실제 키움 REST API 파라미터
  (trde_tp 코드 등)로 변환·제출하는 책임만 집니다. "얼마에 몇 주를 왜 사는지"를
  결정하는 전략 로직과 "어떻게 API를 호출하는지"를 결정하는 연동 로직을 분리하기
  위한 경계입니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

Side = Literal["BUY", "SELL"]
# LOC: 지정가 종가주문(Limit-on-Close), LIMIT: 일반 지정가, MOC: 시장가 종가주문(Market-on-Close).
# 키움 REST API의 trde_tp 코드(00/30/33 등)로의 변환은 kiwoom_adapter.py가 담당합니다.
OrderKind = Literal["LOC", "LIMIT", "MOC"]


@dataclass(frozen=True)
class OrderIntent:
    """"이런 주문을 내고 싶다"는 의도를 표현하는 불변 값 객체.

    아직 증권사에 제출되지 않은, 순수한 계산 결과입니다. 실제 제출/체결/재시도는
    kiwoom_adapter.py의 몫입니다.
    """

    side: Side
    order_kind: OrderKind
    price: Decimal | None  # LOC/LIMIT는 필수. MOC는 시장가이므로 None.
    qty: int
    purpose: str  # trade_history.py의 BUY_TYPE_*/SELL_TYPE_* 상수 (기록 시 그대로 사용)
    is_decoy: bool = False  # True면 "큰수 매수" 같은 우회용 미끼 주문 (설계도 5-1번)

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"주문 수량(qty)은 1 이상이어야 합니다 (입력값: {self.qty}).")
        if self.order_kind in ("LOC", "LIMIT") and self.price is None:
            raise ValueError(f"{self.order_kind} 주문은 price가 반드시 있어야 합니다.")
        if self.order_kind == "MOC" and self.price is not None:
            raise ValueError("MOC 주문은 시장가이므로 price를 지정할 수 없습니다.")
