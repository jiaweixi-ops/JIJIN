from __future__ import annotations

import argparse
import json

from app.config import get_settings
from app.db import SessionLocal
from app.services.field_operations import FieldOperationsService


def main() -> int:
    parser = argparse.ArgumentParser(description="V1.3 RC field-operation status/preflight")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("status", "preflight"),
        default="status",
    )
    args = parser.parse_args()

    settings = get_settings()
    with SessionLocal() as db:
        payload = FieldOperationsService(db, settings).status()

    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if args.command == "preflight" and not payload["deployment"]["ready"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
