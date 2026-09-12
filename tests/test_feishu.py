from decimal import Decimal

from app.services.feishu import CommandParser


def test_command_parser():
    parser = CommandParser()
    assert parser.parse("金额加1000").value == Decimal("1000")
    assert parser.parse("卖一半").ratio == Decimal("0.5")
    assert parser.parse("现在就交易").intent == "simulate_now"
    assert parser.parse("为什么买这个").intent == "why"
