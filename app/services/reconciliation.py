from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import ReconciliationStatus
from app.models import AuditLog, Reconciliation, ReconciliationDiff


@dataclass
class ReconciliationTolerance:
    cash_abs: Decimal = Decimal("0.01")
    share_abs: Decimal = Decimal("0.0001")
    nav_abs: Decimal = Decimal("0.00000001")


class ReconciliationService:
    def __init__(
        self,
        db: Session,
        tolerance: ReconciliationTolerance | None = None,
    ):
        self.db = db
        self.tolerance = tolerance or ReconciliationTolerance()

    def create_run(
        self,
        account_id: str,
        run_date: date,
        source: str,
        revision: int = 1,
    ) -> Reconciliation:
        run = Reconciliation(
            account_id=account_id,
            reconcile_date=run_date,
            source=source,
            status=ReconciliationStatus.PENDING,
            revision=revision,
        )
        self.db.add(run)
        self.db.flush()
        return run

    def compare_decimal(
        self,
        run: Reconciliation,
        scope: str,
        key: str,
        expected: Decimal,
        actual: Decimal,
        tolerance: Decimal,
    ) -> bool:
        ok = abs(expected - actual) <= tolerance
        if not ok:
            self.db.add(
                ReconciliationDiff(
                    reconciliation_id=run.id,
                    scope=scope,
                    key=key,
                    expected=str(expected),
                    actual=str(actual),
                    tolerance=str(tolerance),
                    blocking=True,
                )
            )
        return ok

    def finish(self, run: Reconciliation, all_ok: bool) -> Reconciliation:
        run.status = ReconciliationStatus.MATCHED if all_ok else ReconciliationStatus.BLOCKING
        run.summary = {"all_ok": all_ok}
        self.db.commit()
        return run

    def resolve_diff(
        self,
        diff_id: str,
        resolved_by: str,
        note: str,
    ) -> Reconciliation:
        diff = self.db.get(ReconciliationDiff, diff_id)
        if not diff:
            raise KeyError(diff_id)
        if diff.resolved:
            run = self.db.get(Reconciliation, diff.reconciliation_id)
            if not run:
                raise KeyError(diff.reconciliation_id)
            return run

        diff.resolved = True
        diff.resolution_note = note
        run = self.db.get(Reconciliation, diff.reconciliation_id)
        if not run:
            raise KeyError(diff.reconciliation_id)

        self.db.add(
            AuditLog(
                actor_type="user",
                actor_id=resolved_by,
                action="reconciliation.resolve_diff",
                target_type="reconciliation_diff",
                target_id=diff.id,
                payload={"note": note, "reconciliation_id": run.id},
            )
        )
        self.db.flush()

        remaining = self.db.scalar(
            select(ReconciliationDiff)
            .where(
                ReconciliationDiff.reconciliation_id == run.id,
                ReconciliationDiff.blocking.is_(True),
                ReconciliationDiff.resolved.is_(False),
            )
            .limit(1)
        )
        if remaining is None:
            run.status = ReconciliationStatus.RESOLVED
            run.summary = {**(run.summary or {}), "resolved": True}
        self.db.commit()
        return run
