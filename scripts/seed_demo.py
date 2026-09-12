from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from app.config import get_settings
from app.db import SessionLocal, verify_schema_current
from app.enums import AccountType, Role
from app.models import (
    Account,
    DataSource,
    DisclaimerAcceptance,
    FeeRule,
    Fund,
    NavConfirm,
    User,
    UserRiskProfile,
)
from app.services.data_ingestion import FundDataIngestionService, FundRuleSnapshot


def main():
    verify_schema_current()
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        user = User(display_name="demo", role=Role.ADMIN, feishu_open_id="demo-open-id")
        db.add(user)
        db.flush()
        db.add(
            UserRiskProfile(
                user_id=user.id,
                questionnaire_version="v1",
                risk_level=4,
                score=80,
                effective_at=now,
                expires_at=now + timedelta(days=365),
                raw_answers={},
            )
        )
        db.add(DisclaimerAcceptance(user_id=user.id, version="v1.2.2", accepted_at=now))
        account = Account(
            user_id=user.id,
            name="100万模拟盘",
            account_type=AccountType.SIMULATION,
            available_cash=Decimal("1000000"),
        )
        db.add(account)
        fund = Fund(
            code="DEMO001",
            name="示例半导体基金C",
            board="半导体",
            share_class="C",
            cut_off_time=time(15, 0),
            fee_version="demo-v1",
            fee_version_effective_at=now,
            min_holding_days=7,
            risk_level=4,
        )
        db.add(fund)
        db.flush()
        db.add(
            FeeRule(
                fund_id=fund.id,
                version="demo-v1",
                fee_type="subscription",
                rate=Decimal("0"),
                active=True,
            )
        )
        db.add(
            FeeRule(
                fund_id=fund.id,
                version="demo-v1",
                fee_type="redemption",
                min_days=0,
                max_days=6,
                rate=Decimal("0.015"),
                active=True,
            )
        )
        db.add(
            FeeRule(
                fund_id=fund.id,
                version="demo-v1",
                fee_type="redemption",
                min_days=7,
                rate=Decimal("0"),
                active=True,
            )
        )
        db.add(
            NavConfirm(
                fund_id=fund.id,
                nav_date=now.date(),
                nav=Decimal("1.0000"),
                confirmed=True,
                source="demo-official",
                observed_at=now,
            )
        )
        db.add(
            DataSource(
                name="demo-official",
                priority=10,
                sla_seconds=86400,
                enabled=True,
            )
        )
        db.flush()
        FundDataIngestionService(db, get_settings()).ingest_fund_rules(
            fund,
            FundRuleSnapshot(
                source_name="demo-official",
                observed_at=now,
                subscription_open=True,
                redemption_open=True,
                purchase_limit=Decimal("100000"),
                fee_version="demo-v1",
                announcement_fetch_ok=True,
            ),
            actor_id=user.id,
        )
        db.commit()
        print("seeded", user.id, account.id, fund.id)


if __name__ == "__main__":
    main()
