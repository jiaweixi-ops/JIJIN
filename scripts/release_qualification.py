from __future__ import annotations

import argparse
import json

from app.config import get_settings
from app.db import SessionLocal
from app.services.release_qualification import ReleaseQualificationService


def _view(row) -> dict:
    return {
        "id": row.id,
        "release_version": row.release_version,
        "mode": row.mode,
        "status": row.status,
        "suite_version": row.suite_version,
        "observed_start_date": (
            row.observed_start_date.isoformat() if row.observed_start_date else None
        ),
        "observed_end_date": row.observed_end_date.isoformat() if row.observed_end_date else None,
        "calendar_days": row.calendar_days,
        "min_business_days": row.min_business_days,
        "enabled_account_count": row.enabled_account_count,
        "blocker_count": row.blocker_count,
        "checks": row.checks,
        "blockers": row.blockers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run V1.3 release qualification gates")
    parser.add_argument("mode", choices=["engineering", "field", "scheduled"])
    args = parser.parse_args()
    settings = get_settings()
    settings.validate_runtime()

    with SessionLocal() as db:
        service = ReleaseQualificationService(db, settings)
        if args.mode == "engineering":
            rows = [service.run_engineering_rehearsal()]
        elif args.mode == "field":
            rows = [service.run_field_gate()]
        else:
            rows = service.scheduled_check()

    payload = [_view(row) for row in rows]
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    final = rows[-1]
    if final.mode == "ENGINEERING_REHEARSAL":
        return 0 if final.status == "ENGINEERING_READY" else 1
    return 0 if final.status == "RELEASE_READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
