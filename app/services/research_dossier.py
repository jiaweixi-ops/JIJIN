from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.collection_models import (
    ResearchCollectedDocument,
    ResearchCollectionSource,
    ResearchDossier,
)
from app.config import Settings
from app.models import AuditLog, Fund
from app.research_models import ResearchInboxItem
from app.research_schemas import ResearchInboxCreate, ResearchMaterialIn
from app.services.research_pipeline import ResearchPipelineService


class ResearchDossierConflict(RuntimeError):
    pass


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ResearchDossierService:
    """Collapse automatically collected documents into one auditable fund/day research item."""

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.pipeline = ResearchPipelineService(db, settings)

    def _business_date(self, now_utc: datetime):
        return now_utc.astimezone(ZoneInfo(self.settings.timezone)).date()

    def _set_raw_status(
        self,
        document: ResearchCollectedDocument,
        status: str,
        reason: str,
        *,
        processed_at: datetime | None,
    ) -> None:
        document.status = status
        document.error = reason
        if not document.research_item_id:
            return
        raw = self.db.get(ResearchInboxItem, document.research_item_id)
        if raw is None or raw.status not in {"NEW", "FAILED", "DEFERRED"}:
            return
        raw.status = "SUPERSEDED" if status != "DEFERRED" else "DEFERRED"
        raw.last_error = reason
        raw.updated_at = processed_at or datetime.now(timezone.utc)
        if status != "DEFERRED":
            raw.processed_at = processed_at

    def _choose_documents(
        self,
        rows: list[tuple[ResearchCollectedDocument, ResearchCollectionSource]],
    ) -> tuple[
        list[tuple[ResearchCollectedDocument, ResearchCollectionSource]],
        list[tuple[ResearchCollectedDocument, ResearchCollectionSource, str]],
    ]:
        # Newest content wins when identical normalized content was syndicated.
        ordered = sorted(
            rows,
            key=lambda pair: (
                _as_utc(pair[0].published_at or pair[0].observed_at),
                _as_utc(pair[0].observed_at),
                pair[0].id,
            ),
            reverse=True,
        )
        unique: list[tuple[ResearchCollectedDocument, ResearchCollectionSource]] = []
        suppressed: list[tuple[ResearchCollectedDocument, ResearchCollectionSource, str]] = []
        seen_hashes: set[str] = set()
        for pair in ordered:
            document = pair[0]
            if document.content_sha256 in seen_hashes:
                suppressed.append((*pair, "DOSSIER_DUPLICATE_CONTENT"))
                continue
            seen_hashes.add(document.content_sha256)
            unique.append(pair)

        # Preserve source diversity first, then fill remaining slots by recency.
        first_per_source: list[tuple[ResearchCollectedDocument, ResearchCollectionSource]] = []
        remainder: list[tuple[ResearchCollectedDocument, ResearchCollectionSource]] = []
        seen_sources: set[str] = set()
        for pair in unique:
            source_id = pair[1].id
            if source_id not in seen_sources:
                first_per_source.append(pair)
                seen_sources.add(source_id)
            else:
                remainder.append(pair)
        candidates = first_per_source + remainder

        selected: list[tuple[ResearchCollectedDocument, ResearchCollectionSource]] = []
        total_chars = 0
        max_materials = self.settings.research_dossier_max_materials
        max_chars = self.settings.research_dossier_max_chars
        for pair in candidates:
            document = pair[0]
            content_chars = len(document.content)
            if len(selected) >= max_materials or total_chars + content_chars > max_chars:
                suppressed.append((*pair, "DOSSIER_LIMIT_EXCEEDED"))
                continue
            selected.append(pair)
            total_chars += content_chars
        return selected, suppressed

    def assemble_pending(
        self,
        *,
        now: datetime | None = None,
        actor_id: str | None = None,
    ) -> dict:
        now_utc = _as_utc(now or datetime.now(timezone.utc))
        business_date = self._business_date(now_utc)
        cutoff = now_utc - timedelta(hours=self.settings.research_dossier_lookback_hours)

        rows = self.db.execute(
            select(ResearchCollectedDocument, ResearchCollectionSource)
            .join(
                ResearchCollectionSource,
                ResearchCollectionSource.id == ResearchCollectedDocument.source_id,
            )
            .where(
                ResearchCollectedDocument.dossier_id.is_(None),
                ResearchCollectedDocument.observed_at <= now_utc,
                ResearchCollectedDocument.status.in_(["INGESTED", "DEFERRED"]),
            )
            .order_by(ResearchCollectedDocument.observed_at, ResearchCollectedDocument.id)
        ).all()

        summary = {
            "business_date": business_date.isoformat(),
            "eligible_documents": len(rows),
            "dossiers_created": 0,
            "documents_selected": 0,
            "documents_suppressed": 0,
            "documents_deferred": 0,
            "documents_stale": 0,
            "dossier_ids": [],
        }

        fresh_groups: dict[
            tuple[str, str],
            list[tuple[ResearchCollectedDocument, ResearchCollectionSource]],
        ] = defaultdict(list)
        for document, source in rows:
            observed = _as_utc(document.observed_at)
            if observed < cutoff:
                self._set_raw_status(
                    document,
                    "STALE",
                    "DOSSIER_STALE_MATERIAL",
                    processed_at=now_utc,
                )
                summary["documents_stale"] += 1
                continue
            fresh_groups[(source.account_id, source.fund_id)].append((document, source))
        self.db.commit()

        for (account_id, fund_id), group in fresh_groups.items():
            existing = self.db.scalar(
                select(ResearchDossier).where(
                    ResearchDossier.account_id == account_id,
                    ResearchDossier.fund_id == fund_id,
                    ResearchDossier.business_date == business_date,
                )
            )
            if existing is not None:
                for document, _source in group:
                    self._set_raw_status(
                        document,
                        "DEFERRED",
                        f"DOSSIER_ALREADY_EXISTS:{existing.id}",
                        processed_at=None,
                    )
                    summary["documents_deferred"] += 1
                self.db.commit()
                continue

            selected, suppressed = self._choose_documents(group)
            for document, _source, reason in suppressed:
                self._set_raw_status(
                    document,
                    "SUPPRESSED",
                    reason,
                    processed_at=now_utc,
                )
                summary["documents_suppressed"] += 1
            if not selected:
                self.db.commit()
                continue

            fund = self.db.get(Fund, fund_id)
            if fund is None:
                for document, _source in selected:
                    self._set_raw_status(
                        document,
                        "SUPPRESSED",
                        "DOSSIER_FUND_MISSING",
                        processed_at=now_utc,
                    )
                    summary["documents_suppressed"] += 1
                self.db.commit()
                continue

            fingerprints = sorted(document.fingerprint for document, _source in selected)
            digest = hashlib.sha256(
                f"{account_id}|{fund_id}|{business_date.isoformat()}|{'|'.join(fingerprints)}".encode(
                    "utf-8"
                )
            ).hexdigest()
            source_names = sorted({source.name for _document, source in selected})
            materials = [
                ResearchMaterialIn(
                    source_name=source.name,
                    source_url=document.canonical_url or source.feed_url,
                    published_at=(
                        _as_utc(document.published_at)
                        if document.published_at is not None
                        else None
                    ),
                    observed_at=_as_utc(document.observed_at),
                    content=document.content,
                )
                for document, source in selected
            ]
            inbox = self.pipeline.ingest(
                ResearchInboxCreate(
                    account_id=account_id,
                    fund_id=fund_id,
                    topic=(
                        f"多源研究汇总：{fund.name}（{len(materials)}条/{len(source_names)}来源）"
                    )[:300],
                    idempotency_key=f"dossier-{digest}",
                    materials=materials,
                ),
                actor_id=actor_id,
                now=now_utc,
            )

            dossier = ResearchDossier(
                account_id=account_id,
                fund_id=fund_id,
                business_date=business_date,
                status="ASSEMBLED",
                research_item_id=inbox.id,
                selected_document_ids=[document.id for document, _source in selected],
                suppressed_document_ids=[document.id for document, _source, _reason in suppressed],
                raw_research_item_ids=[
                    document.research_item_id
                    for document, _source in group
                    if document.research_item_id
                ],
                source_names=source_names,
                material_count=len(materials),
                source_count=len(source_names),
                total_chars=sum(len(document.content) for document, _source in selected),
                metadata_json={
                    "lookback_hours": self.settings.research_dossier_lookback_hours,
                    "max_materials": self.settings.research_dossier_max_materials,
                    "max_chars": self.settings.research_dossier_max_chars,
                    "digest": digest,
                },
                created_at=now_utc,
            )
            self.db.add(dossier)
            try:
                self.db.flush()
            except IntegrityError as exc:
                self.db.rollback()
                raise ResearchDossierConflict(
                    "research dossier was concurrently assembled for this account/fund/day"
                ) from exc

            for document, _source in selected:
                document.dossier_id = dossier.id
                self._set_raw_status(
                    document,
                    "DOSSIERED",
                    f"CONSOLIDATED_INTO:{inbox.id}",
                    processed_at=now_utc,
                )
            for document, _source, _reason in suppressed:
                document.dossier_id = dossier.id

            self.db.add(
                AuditLog(
                    actor_type="user" if actor_id else "system",
                    actor_id=actor_id,
                    action="research.dossier.assemble",
                    target_type="research_dossier",
                    target_id=dossier.id,
                    payload={
                        "research_item_id": inbox.id,
                        "account_id": account_id,
                        "fund_id": fund_id,
                        "business_date": business_date.isoformat(),
                        "material_count": dossier.material_count,
                        "source_count": dossier.source_count,
                        "selected_document_ids": dossier.selected_document_ids,
                        "suppressed_document_ids": dossier.suppressed_document_ids,
                    },
                )
            )
            self.db.commit()
            summary["dossiers_created"] += 1
            summary["documents_selected"] += len(selected)
            summary["dossier_ids"].append(dossier.id)

        return summary
