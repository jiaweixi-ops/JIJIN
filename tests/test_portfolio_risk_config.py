from __future__ import annotations

import pytest

from app.config import Settings


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_single_fund_weight", 0),
        ("max_single_fund_weight", 1.01),
        ("max_daily_trade_ratio", 0),
        ("max_daily_trade_ratio", 1.01),
        ("max_portfolio_drawdown", 0),
        ("max_portfolio_drawdown", 1.01),
        ("max_consecutive_loss_days", 0),
    ],
)
def test_runtime_validation_rejects_invalid_portfolio_guardrails(field, value):
    settings = Settings(app_env="test", **{field: value})

    with pytest.raises(RuntimeError):
        settings.validate_runtime()


def test_runtime_validation_accepts_default_portfolio_guardrails():
    Settings(app_env="test").validate_runtime()
