from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import AccountType, DataQualityLevel, OrderSide
from app.models import Account, AuditLog, Fund
from app.research_models import ResearchEvidence, ResearchInboxItem
from app.research_schemas import ResearchInboxCreate
from app.schemas import DecisionPlan, OrderCreate, ResearchPacket
from app.services.ai_gateway import ModelGateway
from app.services.data_quality import DataQualityGate
from app.services.decision_engine import DecisionEngine
from app.services.order_service import OrderService

PIPELINE_VERSION = "v1.3-phase2"
TERMINAL_RESEARCH_STATUSES = {"PROCESSED"}
AUTO_RETRY_STATUSES = {"NEW", "FAILED"}
SPECIAL_RETRY_ABSTAIN_REASONS = {"DATA_QUALITY_RED", "CIO_UNAVAILABLE"}


class ResearchIdempotencyConflict(ValueError):
    pass


class ResearchProcessingConflict(RuntimeError):
    pass


class ResearchMaxAttemptsExceeded(RuntimeError):
    pass


class ResearchPipelineService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.quality_gate = DataQualityGate(settings)
        self.order_service = OrderService(db, settings)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _payload_hash(payload: ResearchInboxCreate) -> str:
        canonical = json.dumps(
            payload.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _material_rows(payload: ResearchInboxCreate) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for material in payload.materials:
            row = material.model_dump(mode="json")
            row["content_sha256"] = hashlib.sha256(
                material.content.encode("utf-8")
            ).hexdigest()
            rows.append(row)
        return rows

    def _audit(
        self,
        action: str,
        item_id: str,
        *,
        actor_id: str | None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.db.add(
            AuditLog(
                actor_type="user" if actor_id else "system",
                actor_id=actor_id,
                action=action,
                target_type="research_inbox",
                target_id=item_id,
                payload=payload or {},
            )
        )

    def ingest(
        self,
        payload: ResearchInboxCreate,
        *,
        actor_id: str | None,
        now: datetime | None = None,
    ) -> ResearchInboxItem:
        now_utc = self._as_utc(now or datetime.now(timezone.utc))
        account = self.db.get(Account, payload.account_id)
        if account is None:
            raise KeyError("account not found")
        if account.account_type != AccountType.SIMULATION or not account.enabled:
            raise ValueError("research pipeline only targets enabled simulation accounts")
        fund = self.db.get(Fund, payload.fund_id)
        if fund is None:
            raise KeyError("fund not found")

        total_chars = sum(len(material.content) for material in payload.materials)
        if total_chars > self.settings.ai_max_source_chars:
            raise ValueError(
                f"research materials exceed AI_MAX_SOURCE_CHARS={self.settings.ai_max_source_chars}"
            )
        for material in payload.materials:
            observed_at = self._as_utc(material.observed_at)
            if observed_at > now_utc + timedelta(minutes=5):
                raise ValueError("research material observed_at is in the future")
            if material.published_at is not None:
                published_at = self._as_utc(material.published_at)
                if published_at > observed_at + timedelta(minutes=5):
                    raise ValueError("research material published_at is after observed_at")

        payload_hash = self._payload_hash(payload)
        existing = self.db.scalar(
            select(ResearchInboxItem).where(
                ResearchInboxItem.account_id == payload.account_id,
                ResearchInboxItem.idempotency_key == payload.idempotency_key,
            )
        )
        if existing is not None:
            if existing.payload_hash != payload_hash:
                raise ResearchIdempotencyConflict(
                    "research idempotency key was already used with different content"
                )
            return existing

        item = ResearchInboxItem(
            account_id=payload.account_id,
            fund_id=payload.fund_id,
            topic=payload.topic,
            idempotency_key=payload.idempotency_key,
            payload_hash=payload_hash,
            pipeline_version=PIPELINE_VERSION,
            status="NEW",
            attempt=0,
            created_by=actor_id,
            materials=self._material_rows(payload),
            python_metrics=payload.python_metrics,
            quality_snapshot={},
            research_packet={},
            structured_packet={},
            decision_plan={},
            candidate_order_ids=[],
            last_error="",
            created_at=now_utc,
            updated_at=now_utc,
        )
        self.db.add(item)
        try:
            self.db.flush()
        except IntegrityError:
            self.db.rollback()
            raced = self.db.scalar(
                select(ResearchInboxItem).where(
                    ResearchInboxItem.account_id == payload.account_id,
                    ResearchInboxItem.idempotency_key == payload.idempotency_key,
                )
            )
            if raced is not None and raced.payload_hash == payload_hash:
                return raced
            raise ResearchIdempotencyConflict(
                "research idempotency key was concurrently used with different content"
            )

        self._audit(
            "research.inbox.ingest",
            item.id,
            actor_id=actor_id,
            payload={
                "fund_id": item.fund_id,
                "account_id": item.account_id,
                "payload_hash": item.payload_hash,
                "material_count": len(item.materials),
            },
        )
        self.db.commit()
        return item

    def _claim(
        self,
        item: ResearchInboxItem,
        *,
        now_utc: datetime,
        manual: bool,
    ) -> ResearchInboxItem:
        if item.status in TERMINAL_RESEARCH_STATUSES:
            return item
        if (
            not manual
            and item.status == "FAILED"
            and item.attempt >= self.settings.research_pipeline_max_attempts
        ):
            raise ResearchMaxAttemptsExceeded(
                f"research item reached automatic retry limit={self.settings.research_pipeline_max_attempts}"
            )
        if item.status == "PROCESSING" and item.started_at is not None:
            started_at = self._as_utc(item.started_at)
            if now_utc - started_at < timedelta(
                minutes=self.settings.research_processing_stale_minutes
            ):
                raise ResearchProcessingConflict("research item is already being processed")

        old_status = item.status
        old_attempt = item.attempt
        result = self.db.execute(
            update(ResearchInboxItem)
            .where(
                ResearchInboxItem.id == item.id,
                ResearchInboxItem.status == old_status,
                ResearchInboxItem.attempt == old_attempt,
            )
            .values(
                status="PROCESSING",
                attempt=old_attempt + 1,
                started_at=now_utc,
                last_error="",
                updated_at=now_utc,
            )
        )
        if result.rowcount != 1:
            self.db.rollback()
            raise ResearchProcessingConflict("research item changed while being claimed")
        self.db.commit()
        claimed = self.db.get(ResearchInboxItem, item.id)
        if claimed is None:
            raise RuntimeError("research item disappeared after claim")
        return claimed

    @staticmethod
    def _material_prompt(item: ResearchInboxItem) -> str:
        sources = []
        for material in item.materials:
            sources.append(
                {
                    "source_name": material.get("source_name"),
                    "source_url": material.get("source_url"),
                    "published_at": material.get("published_at"),
                    "observed_at": material.get("observed_at"),
                    "content_sha256": material.get("content_sha256"),
                    "content": material.get("content"),
                }
            )
        return json.dumps(
            {"research_item_id": item.id, "trusted_materials": sources},
            ensure_ascii=False,
        )

    def _validate_structured_evidence(
        self,
        item: ResearchInboxItem,
        packet: ResearchPacket,
        *,
        now_utc: datetime,
    ) -> None:
        source_names = {
            str(material.get("source_name", "")).strip()
            for material in item.materials
            if str(material.get("source_name", "")).strip()
        }
        source_urls = {
            str(material.get("source_url", "")).strip()
            for material in item.materials
            if str(material.get("source_url", "")).strip()
        }
        seen_ids: set[str] = set()
        for fact in packet.facts:
            evidence_id = fact.evidence_id.strip()
            if not evidence_id:
                raise ValueError("structured evidence has a blank evidence_id")
            if evidence_id in seen_ids:
                raise ValueError(f"duplicate evidence_id: {evidence_id}")
            seen_ids.add(evidence_id)

            if fact.source_url:
                if fact.source_url.strip() not in source_urls:
                    raise ValueError(
                        f"evidence {evidence_id} references a source_url not present in the trusted inbox"
                    )
            elif fact.source_name.strip() not in source_names:
                raise ValueError(
                    f"evidence {evidence_id} references a source_name not present in the trusted inbox"
                )

            observed_at = self._as_utc(fact.observed_at)
            if observed_at > now_utc + timedelta(minutes=5):
                raise ValueError(f"evidence {evidence_id} observed_at is in the future")
            if fact.published_at is not None:
                published_at = self._as_utc(fact.published_at)
                if published_at > observed_at + timedelta(minutes=5):
                    raise ValueError(
                        f"evidence {evidence_id} published_at is after observed_at"
                    )

    def _persist_evidence(
        self,
        item: ResearchInboxItem,
        packet: ResearchPacket,
        *,
        now_utc: datetime,
    ) -> None:
        existing = {
            row.evidence_id
            for row in self.db.scalars(
                select(ResearchEvidence).where(
                    ResearchEvidence.research_item_id == item.id
                )
            ).all()
        }
        for fact in packet.facts:
            if fact.evidence_id in existing:
                continue
            self.db.add(
                ResearchEvidence(
                    research_item_id=item.id,
                    fund_id=item.fund_id,
                    evidence_id=fact.evidence_id,
                    claim=fact.claim,
                    source_name=fact.source_name,
                    source_url=fact.source_url,
                    published_at=(
                        self._as_utc(fact.published_at)
                        if fact.published_at is not None
                        else None
                    ),
                    observed_at=self._as_utc(fact.observed_at),
                    direction=fact.direction,
                    horizon=fact.horizon,
                    confidence=fact.confidence,
                    is_counter_evidence=fact.is_counter_evidence,
                    created_at=now_utc,
                )
            )

    def _quality_snapshot(self, fund: Fund, now_utc: datetime) -> tuple[Any, dict[str, Any]]:
        quality = self.quality_gate.evaluate_fund(self.db, fund, now=now_utc)
        snapshot = {
            "research_quality": quality.research_quality.value,
            "settlement_eligibility": quality.settlement_eligibility,
            "reasons": list(quality.reasons),
            "captured_at": now_utc.isoformat(),
        }
        return quality, snapshot

    @staticmethod
    def _candidate_key(
        item: ResearchInboxItem,
        plan: DecisionPlan,
        index: int,
        action: Any,
    ) -> str:
        canonical_action = json.dumps(
            action.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(
            f"{item.id}|{plan.decision_id}|{index}|{canonical_action}".encode("utf-8")
        ).hexdigest()[:40]
        return f"research-{digest}"

    def _materialize_candidate_orders(
        self,
        item: ResearchInboxItem,
        plan: DecisionPlan,
        fund: Fund,
        quality_snapshot: dict[str, Any],
        *,
        now_utc: datetime,
        actor_id: str | None,
    ) -> list[str]:
        for action in plan.actions:
            if action.action == OrderSide.CONVERT:
                raise ValueError("CONVERT_NOT_SUPPORTED: research pipeline cannot create conversions")

        trade_actions = [
            action for action in plan.actions if action.action in {OrderSide.BUY, OrderSide.SELL}
        ]
        if len(trade_actions) > 1:
            raise ValueError("one research inbox item may create at most one trade candidate")
        if not trade_actions:
            return []

        persisted_ids = {
            row.evidence_id
            for row in self.db.scalars(
                select(ResearchEvidence).where(
                    ResearchEvidence.research_item_id == item.id
                )
            ).all()
        }
        action = trade_actions[0]
        if action.fund_code != fund.code:
            raise ValueError(
                f"CIO action fund_code={action.fund_code!r} does not match inbox fund {fund.code!r}"
            )
        if not action.evidence_ids or not set(action.evidence_ids).issubset(persisted_ids):
            raise ValueError("CIO trade action does not reference persisted evidence")

        key = self._candidate_key(item, plan, 0, action)
        order = self.order_service.create(
            OrderCreate(
                account_id=item.account_id,
                fund_id=fund.id,
                side=action.action,
                amount=action.amount,
                shares=action.shares,
                ratio=action.ratio,
                reason=action.reason,
                evidence_ids=action.evidence_ids,
                idempotency_key=key,
            ),
            now=now_utc,
        )
        order.data_snapshot = {
            **(order.data_snapshot or {}),
            "research_item_id": item.id,
            "research_payload_hash": item.payload_hash,
            "research_pipeline_version": item.pipeline_version,
            "decision_id": plan.decision_id,
            "decision_as_of": self._as_utc(plan.as_of).isoformat(),
            "evidence_ids": list(action.evidence_ids),
            **quality_snapshot,
        }
        self.db.add(
            AuditLog(
                actor_type="user" if actor_id else "system",
                actor_id=actor_id,
                action="research.candidate_order.create",
                target_type="order",
                target_id=order.id,
                payload={
                    "research_item_id": item.id,
                    "decision_id": plan.decision_id,
                    "idempotency_key": key,
                },
            )
        )
        self.db.commit()
        return [order.id]

    def _mark_failed(
        self,
        item_id: str,
        exc: Exception,
        *,
        actor_id: str | None,
        now_utc: datetime,
    ) -> None:
        self.db.rollback()
        item = self.db.get(ResearchInboxItem, item_id)
        if item is None:
            return
        item.status = "FAILED"
        item.last_error = f"{type(exc).__name__}: {exc}"
        item.updated_at = now_utc
        self._audit(
            "research.pipeline.failed",
            item.id,
            actor_id=actor_id,
            payload={"error": item.last_error, "attempt": item.attempt},
        )
        self.db.commit()

    def process(
        self,
        item_id: str,
        *,
        quota_subject: str,
        request_id: str | None,
        actor_id: str | None,
        now: datetime | None = None,
        manual: bool = True,
        gateway: ModelGateway | None = None,
    ) -> ResearchInboxItem:
        now_utc = self._as_utc(now or datetime.now(timezone.utc))
        item = self.db.get(ResearchInboxItem, item_id)
        if item is None:
            raise KeyError(item_id)
        if item.status in TERMINAL_RESEARCH_STATUSES:
            return item
        item = self._claim(item, now_utc=now_utc, manual=manual)

        try:
            account = self.db.get(Account, item.account_id)
            fund = self.db.get(Fund, item.fund_id)
            if account is None or fund is None:
                raise ValueError("research target account/fund no longer exists")
            if account.account_type != AccountType.SIMULATION or not account.enabled:
                raise ValueError("research target account is not an enabled simulation account")

            quality, quality_snapshot = self._quality_snapshot(fund, now_utc)
            item.quality_snapshot = quality_snapshot
            item.updated_at = now_utc
            self.db.commit()

            actual_gateway = gateway or ModelGateway(
                self.settings,
                self.db,
                quota_subject=quota_subject,
                request_id=request_id,
            )
            engine = DecisionEngine(actual_gateway)

            if quality.research_quality == DataQualityLevel.RED:
                blocked_research = ResearchPacket(
                    topic=item.topic,
                    as_of=now_utc,
                    facts=[],
                    conflicts=[],
                    missing_information=quality.reasons or ["DATA_QUALITY_RED"],
                )
                plan = engine.decide(
                    blocked_research,
                    item.python_metrics or {},
                    DataQualityLevel.RED,
                )
                item.decision_plan = plan.model_dump(mode="json")
                item.status = "BLOCKED"
                item.processed_at = now_utc
                item.last_error = "DATA_QUALITY_RED"
                item.updated_at = now_utc
                self._audit(
                    "research.pipeline.blocked",
                    item.id,
                    actor_id=actor_id,
                    payload={"reason": "DATA_QUALITY_RED", **quality_snapshot},
                )
                self.db.commit()
                return item

            if item.decision_plan:
                previous_plan = DecisionPlan.model_validate(item.decision_plan)
                if previous_plan.abstain_reason in SPECIAL_RETRY_ABSTAIN_REASONS:
                    item.decision_plan = {}
                    item.processed_at = None
                    self.db.commit()

            if item.research_packet:
                research = ResearchPacket.model_validate(item.research_packet)
            else:
                research = engine.research(item.topic, self._material_prompt(item))
                item.research_packet = research.model_dump(mode="json")
                item.updated_at = now_utc
                self.db.commit()

            if item.structured_packet:
                structured = ResearchPacket.model_validate(item.structured_packet)
            else:
                structured = engine.structure(research)
                self._validate_structured_evidence(item, structured, now_utc=now_utc)
                self._persist_evidence(item, structured, now_utc=now_utc)
                item.structured_packet = structured.model_dump(mode="json")
                item.updated_at = now_utc
                self.db.commit()

            if item.decision_plan:
                plan = DecisionPlan.model_validate(item.decision_plan)
            else:
                plan = engine.decide(
                    structured,
                    item.python_metrics or {},
                    quality.research_quality,
                )
                item.decision_plan = plan.model_dump(mode="json")
                item.updated_at = now_utc
                self.db.commit()

            if plan.abstain_reason in SPECIAL_RETRY_ABSTAIN_REASONS:
                item.status = "BLOCKED"
                item.processed_at = now_utc
                item.last_error = plan.abstain_reason
                item.updated_at = now_utc
                self._audit(
                    "research.pipeline.blocked",
                    item.id,
                    actor_id=actor_id,
                    payload={"reason": plan.abstain_reason},
                )
                self.db.commit()
                return item

            candidate_ids = self._materialize_candidate_orders(
                item,
                plan,
                fund,
                quality_snapshot,
                now_utc=now_utc,
                actor_id=actor_id,
            )
            item = self.db.get(ResearchInboxItem, item.id)
            if item is None:
                raise RuntimeError("research item disappeared before finalize")
            item.candidate_order_ids = candidate_ids
            item.status = "PROCESSED"
            item.processed_at = now_utc
            item.last_error = ""
            item.updated_at = now_utc
            self._audit(
                "research.pipeline.processed",
                item.id,
                actor_id=actor_id,
                payload={
                    "decision_id": plan.decision_id,
                    "candidate_order_ids": candidate_ids,
                    "evidence_count": len(structured.facts),
                },
            )
            self.db.commit()
            return item
        except Exception as exc:
            self._mark_failed(item.id, exc, actor_id=actor_id, now_utc=now_utc)
            raise

    def process_pending(
        self,
        *,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        now_utc = self._as_utc(now or datetime.now(timezone.utc))
        batch_size = limit or self.settings.research_pipeline_batch_size
        items = self.db.scalars(
            select(ResearchInboxItem)
            .where(
                ResearchInboxItem.status.in_(list(AUTO_RETRY_STATUSES)),
                ResearchInboxItem.attempt < self.settings.research_pipeline_max_attempts,
            )
            .order_by(ResearchInboxItem.created_at, ResearchInboxItem.id)
            .limit(batch_size)
        ).all()
        summary: dict[str, Any] = {
            "selected": len(items),
            "processed": 0,
            "blocked": 0,
            "failed": 0,
            "candidate_orders": 0,
            "items": [],
        }
        for item in items:
            try:
                result = self.process(
                    item.id,
                    quota_subject="system",
                    request_id=f"operational-research-{item.id}-{now_utc.date().isoformat()}",
                    actor_id=None,
                    now=now_utc,
                    manual=False,
                )
                if result.status == "PROCESSED":
                    summary["processed"] += 1
                    summary["candidate_orders"] += len(result.candidate_order_ids or [])
                elif result.status == "BLOCKED":
                    summary["blocked"] += 1
                summary["items"].append(
                    {
                        "research_item_id": result.id,
                        "status": result.status,
                        "candidate_order_ids": result.candidate_order_ids or [],
                    }
                )
            except Exception as exc:
                summary["failed"] += 1
                summary["items"].append(
                    {
                        "research_item_id": item.id,
                        "status": "FAILED",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        return summary
