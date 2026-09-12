import pytest

from app.domain.order_state import InvalidTransition, StaleOrderVersion, assert_version, transition
from app.enums import OrderEventType, OrderStatus


def test_happy_path_transitions():
    status = OrderStatus.SUGGESTED
    for event, expected in [
        (OrderEventType.SEND_TO_RISK, OrderStatus.PENDING_RISK),
        (OrderEventType.RISK_PASS, OrderStatus.PENDING_CONFIRM),
        (OrderEventType.USER_APPROVE, OrderStatus.APPROVED),
        (OrderEventType.SUBMIT_OK, OrderStatus.SUBMITTED),
        (OrderEventType.MARK_IN_TRANSIT, OrderStatus.IN_TRANSIT),
        (OrderEventType.FULL_CONFIRM, OrderStatus.CONFIRMED),
    ]:
        status = transition(status, event).to_status
        assert status == expected


def test_old_card_version_rejected():
    with pytest.raises(StaleOrderVersion):
        assert_version(3, 2)


def test_invalid_transition_rejected():
    with pytest.raises(InvalidTransition):
        transition(OrderStatus.CONFIRMED, OrderEventType.USER_MODIFY)
