from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import DataQualityLevel
from app.fund_data_models import FundDataConnector, FundDataObservation, FundDataSyncRun
from app.models import AuditLog, DataQuality, DataSource, Fund, NavConfirm
from app.services.data_ingestion import FundDataIngestionService, FundRuleSnapshot
from app.services.research_collection import _assert_public_dns, _public_url_syntax


class FundDataSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class FundDataHTTPResult:
    status_code: int
    body: bytes
    final_url: str
    etag: str | None = None
    last_modified: str | None = None


class FundDataFetcher(Protocol):
    def fetch(self, connector: FundDataConnector) -> FundDataHTTPResult: ...


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: Any, *, fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid observed_at: {value!r}") from exc
    return _utc(parsed)


def _parse_decimal(value: Any, *, name: str, allow_none: bool = False) -> Decimal | None:
    if value is None and allow_none:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid decimal for {name}") from exc
    if result < 0:
        raise ValueError(f"{name} must be nonnegative")
    return result


class SafeFundDataFetcher:
    """Fetch only explicitly registered public JSON endpoints."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def fetch(self, connector: FundDataConnector) -> FundDataHTTPResult:
        current_url = _public_url_syntax(
            connector.endpoint_url,
            allow_http=self.settings.fund_data_allow_http,
        )
        headers = {"Accept": "application/json", "User-Agent": "JIJIN-FundData/1.3"}
        if connector.etag:
            headers["If-None-Match"] = connector.etag
        if connector.last_modified:
            headers["If-Modified-Since"] = connector.last_modified
        if connector.auth_env_key:
            secret = os.getenv(connector.auth_env_key, "")
            if not secret:
                raise FundDataSyncError(
                    f"missing configured fund-data secret env: {connector.auth_env_key}"
                )
            headers[connector.auth_header_name or "Authorization"] = secret

        with httpx.Client(
            timeout=self.settings.fund_data_timeout_seconds,
            follow_redirects=False,
        ) as client:
            for redirect_count in range(self.settings.fund_data_max_redirects + 1):
                _assert_public_dns(
                    current_url,
                    allow_http=self.settings.fund_data_allow_http,
                )
                with client.stream("GET", current_url, headers=headers) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if redirect_count >= self.settings.fund_data_max_redirects:
                            raise FundDataSyncError("fund-data redirect limit exceeded")
                        location = response.headers.get("location")
                        if not location:
                            raise FundDataSyncError("fund-data redirect missing Location")
                        current_url = urljoin(current_url, location)
                        continue
                    if response.status_code == 304:
                        return FundDataHTTPResult(
                            status_code=304,
                            body=b"",
                            final_url=current_url,
                            etag=response.headers.get("etag") or connector.etag,
                            last_modified=(
                                response.headers.get("last-modified") or connector.last_modified
                            ),
                        )
                    response.raise_for_status()
                    media_type = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if media_type != "application/json" and not media_type.endswith("+json"):
                        raise FundDataSyncError(
                            f"unsupported fund-data Content-Type: {media_type or '<missing>'}"
                        )
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > self.settings.fund_data_max_bytes:
                            raise FundDataSyncError("fund-data response exceeds byte limit")
                        chunks.append(chunk)
                    return FundDataHTTPResult(
                        status_code=response.status_code,
                        body=b"".join(chunks),
                        final_url=current_url,
                        etag=response.headers.get("etag"),
                        last_modified=response.headers.get("last-modified"),
                    )
        raise FundDataSyncError("fund-data fetch terminated unexpectedly")


class FundDataSyncService:
    ADAPTER = "STANDARD_JSON_V1"

    def __init__(
        self,
        db: Session,
        settings: Settings,
        *,
        fetcher: FundDataFetcher | None = None,
    ):
        self.db = db
        self.settings = settings
        self.fetcher = fetcher or SafeFundDataFetcher(settings)
        self.rules = FundDataIngestionService(db, settings)
        self.tz = ZoneInfo(settings.timezone)

    def _source(self, name: str) -> DataSource:
        source = self.db.scalar(select(DataSource).where(DataSource.name == name))
        if source is None:
            raise KeyError(f"data source not found: {name}")
        if not source.enabled:
            raise ValueError(f"data source disabled: {name}")
        return source

    def create_connector(
        self,
        *,
        name: str,
        source_name: str,
        endpoint_url: str,
        adapter: str = ADAPTER,
        auth_header_name: str | None = None,
        auth_env_key: str | None = None,
        enabled: bool = True,
        actor_id: str | None = None,
    ) -> FundDataConnector:
        self._source(source_name)
        if adapter != self.ADAPTER:
            raise ValueError("unsupported fund-data adapter")
        endpoint_url = _public_url_syntax(
            endpoint_url,
            allow_http=self.settings.fund_data_allow_http,
        )
        if bool(auth_header_name) != bool(auth_env_key):
            raise ValueError("auth_header_name and auth_env_key must be configured together")
        now = datetime.now(timezone.utc)
        connector = FundDataConnector(
            name=name.strip(),
            source_name=source_name,
            adapter=adapter,
            endpoint_url=endpoint_url,
            auth_header_name=auth_header_name,
            auth_env_key=auth_env_key,
            enabled=enabled,
            last_error="",
            created_at=now,
            updated_at=now,
        )
        self.db.add(connector)
        self.db.flush()
        self.db.add(
            AuditLog(
                actor_type="user" if actor_id else "system",
                actor_id=actor_id,
                action="fund_data.connector.create",
                target_type="fund_data_connector",
                target_id=connector.id,
                payload={
                    "name": connector.name,
                    "source_name": source_name,
                    "adapter": adapter,
                    "endpoint_url": endpoint_url,
                    "auth_env_key": auth_env_key,
                },
            )
        )
        self.db.commit()
        return connector

    @staticmethod
    def _payload_hash(row: dict[str, Any], observed_at: datetime) -> str:
        payload = {"observed_at": _utc(observed_at).isoformat(), "row": row}
        normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _fund(self, row: dict[str, Any], source: DataSource) -> Fund:
        code = str(row.get("code") or "").strip()
        if not code:
            raise ValueError("fund code is required")
        fund = self.db.scalar(select(Fund).where(Fund.code == code))
        profile = dict(row.get("profile") or {})
        if fund is None:
            name = str(row.get("name") or "").strip()
            if not name:
                raise ValueError("fund name is required for a new fund")
            fund = Fund(
                code=code,
                name=name,
                share_class=str(row.get("share_class") or "C")[:8],
                board=str(profile.get("board") or "其他")[:100],
                category=str(profile.get("category") or "OTHER")[:50],
                risk_level=int(profile.get("risk_level") or 3),
                currency=str(profile.get("currency") or "CNY")[:8],
                sales_platform=str(profile.get("sales_platform") or "default")[:100],
                trading_calendar=str(profile.get("trading_calendar") or "CN")[:32],
                metadata_json={},
            )
            self.db.add(fund)
            self.db.flush()
        previous_priority = int((fund.metadata_json or {}).get("profile_source_priority", 10_000))
        if source.priority <= previous_priority:
            if row.get("name"):
                fund.name = str(row["name"])[:200]
            if row.get("share_class"):
                fund.share_class = str(row["share_class"])[:8]
            for key, attr, limit in [
                ("board", "board", 100),
                ("category", "category", 50),
                ("currency", "currency", 8),
                ("sales_platform", "sales_platform", 100),
                ("trading_calendar", "trading_calendar", 32),
            ]:
                if profile.get(key) is not None:
                    setattr(fund, attr, str(profile[key])[:limit])
            if profile.get("risk_level") is not None:
                fund.risk_level = int(profile["risk_level"])
            fund.metadata_json = {
                **(fund.metadata_json or {}),
                **dict(row.get("metadata") or {}),
                "profile_source": source.name,
                "profile_source_priority": source.priority,
            }
        return fund

    def _ingest_rules(
        self,
        fund: Fund,
        row: dict[str, Any],
        source: DataSource,
        observed_at: datetime,
    ) -> None:
        rules = row.get("rules")
        if rules is None:
            return
        if not isinstance(rules, dict):
            raise ValueError("rules must be an object")
        self.rules.ingest_fund_rules(
            fund,
            FundRuleSnapshot(
                source_name=source.name,
                observed_at=observed_at,
                subscription_open=bool(rules.get("subscription_open", False)),
                redemption_open=bool(rules.get("redemption_open", False)),
                purchase_limit=_parse_decimal(
                    rules.get("purchase_limit"), name="purchase_limit", allow_none=True
                ),
                fee_version=str(rules.get("fee_version") or ""),
                fee_version_changed_unresolved=bool(
                    rules.get("fee_version_changed_unresolved", False)
                ),
                announcement_fetch_ok=bool(rules.get("announcement_fetch_ok", True)),
                missing_critical_fields=[
                    str(item) for item in rules.get("missing_critical_fields", [])
                ],
            ),
            actor_id=None,
        )

    def _ingest_nav(
        self,
        fund: Fund,
        row: dict[str, Any],
        source: DataSource,
        observed_at: datetime,
    ) -> None:
        nav = row.get("nav")
        if nav is None:
            return
        if not isinstance(nav, dict):
            raise ValueError("nav must be an object")
        status = str(nav.get("status") or "").upper()
        if status not in {"PENDING", "ESTIMATED", "PROVISIONAL", "CONFIRMED"}:
            raise ValueError("unsupported nav status")
        nav_date_raw = nav.get("nav_date")
        if not nav_date_raw:
            raise ValueError("nav_date is required")
        try:
            nav_date = date.fromisoformat(str(nav_date_raw))
        except ValueError as exc:
            raise ValueError("invalid nav_date") from exc
        nav_observed_at = _parse_datetime(nav.get("observed_at"), fallback=observed_at)
        if nav_date > nav_observed_at.astimezone(self.tz).date():
            raise ValueError("future NAV is forbidden")
        value = _parse_decimal(nav.get("value"), name="nav")
        if value is None or value <= 0:
            raise ValueError("nav must be positive")

        if status != "CONFIRMED":
            self.db.add(
                DataQuality(
                    entity_type="fund",
                    entity_id=fund.id,
                    field_name="nav_status",
                    level=DataQualityLevel.YELLOW,
                    observed_at=nav_observed_at,
                    source=source.name,
                    details={
                        "nav_date": nav_date.isoformat(),
                        "nav": str(value),
                        "status": status,
                        "settlement_eligible": False,
                    },
                )
            )
            return

        existing = self.db.scalar(
            select(NavConfirm).where(
                NavConfirm.fund_id == fund.id,
                NavConfirm.nav_date == nav_date,
            )
        )
        effective = True
        conflict = False
        existing_nav = str(existing.nav) if existing is not None else None
        existing_source_name = existing.source if existing is not None else None
        if existing is not None and Decimal(existing.nav) != value:
            conflict = True
            existing_source = self.db.scalar(
                select(DataSource).where(DataSource.name == existing.source)
            )
            existing_priority = existing_source.priority if existing_source else 10_000
            effective = source.priority < existing_priority

        if existing is None:
            existing = NavConfirm(
                fund_id=fund.id,
                nav_date=nav_date,
                nav=value,
                confirmed=True,
                source=source.name,
                observed_at=nav_observed_at,
            )
            self.db.add(existing)
        elif effective:
            existing.nav = value
            existing.confirmed = True
            existing.source = source.name
            existing.observed_at = nav_observed_at

        self.db.add(
            DataQuality(
                entity_type="fund",
                entity_id=fund.id,
                field_name="nav_conflict",
                level=DataQualityLevel.RED if conflict else DataQualityLevel.GREEN,
                observed_at=nav_observed_at,
                source=source.name,
                details={
                    "nav_date": nav_date.isoformat(),
                    "conflict": conflict,
                    "incoming_nav": str(value),
                    "existing_nav": existing_nav,
                    "incoming_source": source.name,
                    "existing_source": existing_source_name,
                    "effective": effective,
                },
            )
        )
        self.db.add(
            DataQuality(
                entity_type="fund",
                entity_id=fund.id,
                field_name="nav_status",
                level=DataQualityLevel.RED if conflict else DataQualityLevel.GREEN,
                observed_at=nav_observed_at,
                source=source.name,
                details={
                    "nav_date": nav_date.isoformat(),
                    "nav": str(value),
                    "status": "CONFIRMED",
                    "settlement_eligible": not conflict,
                    "effective": effective,
                },
            )
        )

    def _ingest_row(
        self,
        connector: FundDataConnector,
        run: FundDataSyncRun,
        source: DataSource,
        row: dict[str, Any],
        default_observed_at: datetime,
    ) -> tuple[bool, str]:
        observed_at = _parse_datetime(row.get("observed_at"), fallback=default_observed_at)
        payload_hash = self._payload_hash(row, observed_at)
        duplicate = self.db.scalar(
            select(FundDataObservation).where(
                FundDataObservation.connector_id == connector.id,
                FundDataObservation.payload_hash == payload_hash,
            )
        )
        if duplicate is not None:
            return False, "duplicate"
        code = str(row.get("code") or "").strip()
        observation = FundDataObservation(
            connector_id=connector.id,
            run_id=run.id,
            source_name=source.name,
            fund_code=code or "<missing>",
            payload_hash=payload_hash,
            observed_at=observed_at,
            payload=row,
            accepted=False,
            rejection_reason="",
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(observation)
        self.db.flush()
        try:
            with self.db.begin_nested():
                fund = self._fund(row, source)
                self._ingest_rules(fund, row, source, observed_at)
                self._ingest_nav(fund, row, source, observed_at)
                self.db.flush()
            observation.fund_id = fund.id
            observation.accepted = True
            return True, "accepted"
        except Exception as exc:
            observation.rejection_reason = str(exc)
            return False, str(exc)

    def sync_connector(
        self,
        connector_id: str,
        *,
        actor_id: str | None = None,
    ) -> FundDataSyncRun:
        connector = self.db.get(FundDataConnector, connector_id)
        if connector is None:
            raise KeyError(connector_id)
        if not connector.enabled:
            raise ValueError("fund-data connector is disabled")
        if connector.adapter != self.ADAPTER:
            raise ValueError("unsupported fund-data adapter")
        source = self._source(connector.source_name)
        now = datetime.now(timezone.utc)
        run = FundDataSyncRun(
            connector_id=connector.id,
            status="RUNNING",
            fetched_count=0,
            ingested_count=0,
            rejected_count=0,
            summary={},
            error="",
            started_at=now,
        )
        connector.last_attempt_at = now
        connector.updated_at = now
        self.db.add(run)
        self.db.commit()

        try:
            result = self.fetcher.fetch(connector)
            run.http_status = result.status_code
            connector.etag = result.etag
            connector.last_modified = result.last_modified
            if result.status_code == 304:
                run.status = "NOT_MODIFIED"
                run.finished_at = datetime.now(timezone.utc)
                connector.last_success_at = run.finished_at
                connector.last_error = ""
                self.db.commit()
                return run
            try:
                payload = json.loads(result.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FundDataSyncError("fund-data response is not valid UTF-8 JSON") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("funds"), list):
                raise FundDataSyncError("STANDARD_JSON_V1 requires an object with a funds array")
            default_observed_at = _parse_datetime(
                payload.get("observed_at"), fallback=datetime.now(timezone.utc)
            )
            rows = payload["funds"]
            if len(rows) > self.settings.fund_data_max_funds_per_sync:
                raise FundDataSyncError("fund-data payload exceeds fund-count limit")
            run.fetched_count = len(rows)
            errors: list[dict[str, str]] = []
            duplicates = 0
            for item in rows:
                if not isinstance(item, dict):
                    run.rejected_count += 1
                    errors.append({"code": "<invalid>", "error": "fund row must be an object"})
                    continue
                accepted, message = self._ingest_row(
                    connector, run, source, item, default_observed_at
                )
                if accepted:
                    run.ingested_count += 1
                elif message == "duplicate":
                    duplicates += 1
                else:
                    run.rejected_count += 1
                    errors.append({"code": str(item.get("code") or ""), "error": message})
                self.db.flush()
            run.summary = {"duplicates": duplicates, "errors": errors[:50]}
            run.status = "PARTIAL" if run.rejected_count else "SUCCEEDED"
            run.finished_at = datetime.now(timezone.utc)
            connector.last_success_at = run.finished_at
            connector.last_error = "" if not errors else f"{len(errors)} fund row(s) rejected"
            connector.updated_at = run.finished_at
            self.db.add(
                AuditLog(
                    actor_type="user" if actor_id else "system",
                    actor_id=actor_id,
                    action="fund_data.connector.sync",
                    target_type="fund_data_connector",
                    target_id=connector.id,
                    payload={
                        "run_id": run.id,
                        "status": run.status,
                        "fetched": run.fetched_count,
                        "ingested": run.ingested_count,
                        "rejected": run.rejected_count,
                        "duplicates": duplicates,
                    },
                )
            )
            self.db.commit()
            return run
        except Exception as exc:
            self.db.rollback()
            connector = self.db.get(FundDataConnector, connector_id)
            run = self.db.get(FundDataSyncRun, run.id)
            finished = datetime.now(timezone.utc)
            if connector is not None:
                connector.last_error = str(exc)
                connector.last_attempt_at = now
                connector.updated_at = finished
            if run is not None:
                run.status = "FAILED"
                run.error = str(exc)
                run.finished_at = finished
            self.db.commit()
            if run is None:
                raise
            return run

    def sync_enabled(self) -> dict[str, int]:
        connectors = self.db.scalars(
            select(FundDataConnector)
            .where(FundDataConnector.enabled.is_(True))
            .order_by(FundDataConnector.name)
            .limit(self.settings.fund_data_connector_batch_size)
        ).all()
        summary = {"connectors": len(connectors), "succeeded": 0, "partial": 0, "failed": 0}
        for connector in connectors:
            run = self.sync_connector(connector.id)
            if run.status in {"SUCCEEDED", "NOT_MODIFIED"}:
                summary["succeeded"] += 1
            elif run.status == "PARTIAL":
                summary["partial"] += 1
            else:
                summary["failed"] += 1
        return summary
