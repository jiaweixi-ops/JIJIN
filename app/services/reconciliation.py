from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from sqlalchemy.orm import Session
from app.enums import ReconciliationStatus
from app.models import Reconciliation, ReconciliationDiff

@dataclass
class ReconciliationTolerance:
    cash_abs: Decimal = Decimal("0.01")
    share_abs: Decimal = Decimal("0.0001")
    nav_abs: Decimal = Decimal("0.00000001")

class ReconciliationService:
    def __init__(self, db: Session, tolerance: ReconciliationTolerance | None = None): self.db = db; self.tolerance = tolerance or ReconciliationTolerance()
    def create_run(self, account_id: str, run_date: date, source: str, revision: int = 1) -> Reconciliation:
        run = Reconciliation(account_id=account_id, reconcile_date=run_date, source=source, status=ReconciliationStatus.PENDING, revision=revision); self.db.add(run); self.db.flush(); return run
    def compare_decimal(self, run: Reconciliation, scope: str, key: str, expected: Decimal, actual: Decimal, tolerance: Decimal) -> bool:
        ok = abs(expected - actual) <= tolerance
        if not ok: self.db.add(ReconciliationDiff(reconciliation_id=run.id, scope=scope, key=key, expected=str(expected), actual=str(actual), tolerance=str(tolerance), blocking=True))
        return ok
    def finish(self, run: Reconciliation, all_ok: bool) -> Reconciliation:
        run.status = ReconciliationStatus.MATCHED if all_ok else ReconciliationStatus.BLOCKING; run.summary = {"all_ok": all_ok}; self.db.commit(); return run
