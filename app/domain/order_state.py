from __future__ import annotations

from dataclasses import dataclass
from app.enums import OrderEventType, OrderStatus

class InvalidTransition(ValueError):
    pass

class StaleOrderVersion(ValueError):
    pass

TRANSITIONS = {
    (OrderStatus.SUGGESTED, OrderEventType.SEND_TO_RISK): OrderStatus.PENDING_RISK,
    (OrderStatus.PENDING_RISK, OrderEventType.RISK_PASS): OrderStatus.PENDING_CONFIRM,
    (OrderStatus.PENDING_RISK, OrderEventType.RISK_REJECT): OrderStatus.RISK_REJECTED,
    (OrderStatus.PENDING_CONFIRM, OrderEventType.USER_MODIFY): OrderStatus.MODIFIED,
    (OrderStatus.MODIFIED, OrderEventType.USER_MODIFY): OrderStatus.MODIFIED,
    (OrderStatus.MODIFIED, OrderEventType.SEND_TO_RISK): OrderStatus.PENDING_RISK,
    (OrderStatus.PENDING_CONFIRM, OrderEventType.USER_APPROVE): OrderStatus.APPROVED,
    (OrderStatus.APPROVED, OrderEventType.SUBMIT_OK): OrderStatus.SUBMITTED,
    (OrderStatus.APPROVED, OrderEventType.SUBMIT_FAIL): OrderStatus.EXECUTION_FAILED,
    (OrderStatus.EXECUTION_FAILED, OrderEventType.SEND_TO_RISK): OrderStatus.PENDING_RISK,
    (OrderStatus.SUBMITTED, OrderEventType.MARK_IN_TRANSIT): OrderStatus.IN_TRANSIT,
    (OrderStatus.SUBMITTED, OrderEventType.PARTIAL_CONFIRM): OrderStatus.PARTIALLY_CONFIRMED,
    (OrderStatus.SUBMITTED, OrderEventType.FULL_CONFIRM): OrderStatus.CONFIRMED,
    (OrderStatus.IN_TRANSIT, OrderEventType.PARTIAL_CONFIRM): OrderStatus.PARTIALLY_CONFIRMED,
    (OrderStatus.IN_TRANSIT, OrderEventType.FULL_CONFIRM): OrderStatus.CONFIRMED,
    (OrderStatus.PARTIALLY_CONFIRMED, OrderEventType.PARTIAL_CONFIRM): OrderStatus.PARTIALLY_CONFIRMED,
    (OrderStatus.PARTIALLY_CONFIRMED, OrderEventType.FULL_CONFIRM): OrderStatus.CONFIRMED,
}
EXPIRABLE_STATES = {OrderStatus.SUGGESTED, OrderStatus.PENDING_RISK, OrderStatus.PENDING_CONFIRM, OrderStatus.MODIFIED, OrderStatus.APPROVED}
CANCELLABLE_STATES = EXPIRABLE_STATES | {OrderStatus.EXECUTION_FAILED}
MANUAL_FILL_STATES = {OrderStatus.SUBMITTED, OrderStatus.IN_TRANSIT, OrderStatus.PARTIALLY_CONFIRMED, OrderStatus.EXECUTION_FAILED}

@dataclass(frozen=True)
class TransitionResult:
    from_status: OrderStatus
    to_status: OrderStatus

def transition(status: OrderStatus, event: OrderEventType) -> TransitionResult:
    if event == OrderEventType.DUPLICATE_CALLBACK:
        return TransitionResult(status, status)
    if event == OrderEventType.EXPIRE and status in EXPIRABLE_STATES:
        return TransitionResult(status, OrderStatus.EXPIRED)
    if event == OrderEventType.CANCEL and status in CANCELLABLE_STATES:
        return TransitionResult(status, OrderStatus.CANCELLED)
    if event == OrderEventType.MANUAL_FILL and status in MANUAL_FILL_STATES:
        return TransitionResult(status, OrderStatus.MANUAL_RECONCILED)
    key = (status, event)
    if key not in TRANSITIONS:
        raise InvalidTransition(f"{status} cannot handle {event}")
    return TransitionResult(status, TRANSITIONS[key])

def assert_version(current_version: int, expected_version: int) -> None:
    if current_version != expected_version:
        raise StaleOrderVersion(f"订单版本已过期：当前 v{current_version}，请求 v{expected_version}。请使用最新卡片。")
