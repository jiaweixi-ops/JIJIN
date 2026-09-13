from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.dossiers import router as dossier_router
from app.collection_models import (
    ResearchCollectedDocument,
    ResearchCollectionSource,
    ResearchDossier,
)
from app.collection_schemas import ResearchCollectionSourceCreate
from app.config import Settings
from app.db import get_db
from app.enums import AccountType, Role
from app.models import Account, Fund, User
from app.research_models import ResearchInboxItem
from app.services.research_collection import CollectionHTTPResult, ResearchCollectionService
from app.services.research_dossier import ResearchDossierService
from app.services.research_pipeline import ResearchPipelineService, ResearchProcessingConflict

NOW = datetime(2026, 9, 14, 5, 0, tzinfo=timezone.utc)


def _settings(**overrides) -> Settings:
    values = {
        "app_env": "test",
        "timezone": "Asia/Shanghai",
        "ai_max_source_chars": 100_000,
        "research_collection_timeout_seconds": 1,
        "research_collection_max_bytes": 100_000,
        "research_collection_max_redirects": 1,
        "research_collection_max_items_per_source": 10,
        "research_collection_batch_size": 10,
        "research_collection_allow_http": False,
        "research_dossier_lookback_hours": 72,
        "research_dossier_max_materials": 12,
        "research_dossier_max_chars": 80_000,
    }
    values.update(overrides)
    return Settings(**values)


def _seed(db):
    user = User(display_name="dossier-admin", role=Role.ADMIN, active=True)
    db.add(user)
    db.flush()
    account = Account(
        user_id=user.id,
        name="dossier-sim",
        account_type=AccountType.SIMULATION,
        available_cash=Decimal("100000"),
        enabled=True,
    )
    fund = Fund(code="D001", name="多源研究基金", share_class="C")
    db.add_all([account, fund])
    db.commit()
    return user, account, fund


def _rss(external_id: str, title: str, content: str) -> bytes:
    return f"""<?xml version='1.0' encoding='utf-8'?>
<rss version='2.0'><channel><title>Research</title><item>
<guid>{external_id}</guid><title>{title}</title>
<link>https://example.com/articles/{external_id}</link>
<pubDate>Mon, 14 Sep 2026 04:00:00 +0000</pubDate>
<description><![CDATA[<p>{content}</p>]]></description>
</item></channel></rss>""".encode()


class FakeFetcher:
    def __init__(self, payload: bytes):
        self.payload = payload

    def fetch(self, url: str, *, etag: str | None = None, last_modified: str | None = None):
        del etag, last_modified
        return CollectionHTTPResult(
            status_code=200,
            content=self.payload,
            content_type="application/rss+xml",
            final_url=url,
        )


def _make_source(
    service: ResearchCollectionService,
    account: Account,
    fund: Fund,
    user: User,
    *,
    name: str,
    suffix: str,
) -> ResearchCollectionSource:
    return service.create_source(
        ResearchCollectionSourceCreate(
            account_id=account.id,
            fund_id=fund.id,
            name=name,
            adapter="rss",
            feed_url=f"https://example.com/{suffix}.xml",
            topic_prefix=name,
        ),
        actor_id=user.id,
        now=NOW,
    )


def _collect(
    service: ResearchCollectionService,
    source: ResearchCollectionSource,
    user: User,
    *,
    external_id: str,
    title: str,
    content: str,
    now: datetime = NOW,
):
    return service.collect_source(
        source.id,
        actor_id=user.id,
        now=now,
        fetcher=FakeFetcher(_rss(external_id, title, content)),
    )


def test_two_sources_become_one_dossier_and_one_new_research_item(db):
    settings = _settings()
    user, account, fund = _seed(db)
    collection = ResearchCollectionService(db, settings)
    source_a = _make_source(collection, account, fund, user, name="基金公司", suffix="official")
    source_b = _make_source(collection, account, fund, user, name="研究机构", suffix="research")
    _collect(collection, source_a, user, external_id="a1", title="官方观点", content="官方材料 A")
    _collect(collection, source_b, user, external_id="b1", title="机构观点", content="机构材料 B")

    raw_ids = set(db.scalars(select(ResearchInboxItem.id)).all())
    assert len(raw_ids) == 2

    summary = ResearchDossierService(db, settings).assemble_pending(now=NOW, actor_id=user.id)

    assert summary["dossiers_created"] == 1
    assert summary["documents_selected"] == 2
    dossier = db.scalar(select(ResearchDossier))
    assert dossier.material_count == 2
    assert dossier.source_count == 2
    assert dossier.source_names == ["基金公司", "研究机构"]

    consolidated = db.get(ResearchInboxItem, dossier.research_item_id)
    assert consolidated.status == "NEW"
    assert len(consolidated.materials) == 2
    assert db.scalar(select(func.count(ResearchInboxItem.id))) == 3

    raw_rows = db.scalars(select(ResearchInboxItem).where(ResearchInboxItem.id.in_(raw_ids))).all()
    assert {row.status for row in raw_rows} == {"SUPERSEDED"}
    assert all(row.processed_at is not None for row in raw_rows)

    documents = db.scalars(select(ResearchCollectedDocument)).all()
    assert {row.status for row in documents} == {"DOSSIERED"}
    assert {row.dossier_id for row in documents} == {dossier.id}


def test_syndicated_identical_content_is_suppressed_before_ai(db):
    settings = _settings()
    user, account, fund = _seed(db)
    collection = ResearchCollectionService(db, settings)
    source_a = _make_source(collection, account, fund, user, name="来源A", suffix="source-a")
    source_b = _make_source(collection, account, fund, user, name="来源B", suffix="source-b")
    _collect(collection, source_a, user, external_id="a2", title="转载A", content="完全相同的材料")
    _collect(collection, source_b, user, external_id="b2", title="转载B", content="完全相同的材料")

    summary = ResearchDossierService(db, settings).assemble_pending(now=NOW)

    assert summary["dossiers_created"] == 1
    assert summary["documents_selected"] == 1
    assert summary["documents_suppressed"] == 1
    dossier = db.scalar(select(ResearchDossier))
    assert dossier.material_count == 1
    assert len(dossier.suppressed_document_ids) == 1
    rows = db.scalars(select(ResearchCollectedDocument)).all()
    assert sorted(row.status for row in rows) == ["DOSSIERED", "SUPPRESSED"]


def test_later_same_day_document_is_deferred_to_next_dossier_cycle(db):
    settings = _settings()
    user, account, fund = _seed(db)
    collection = ResearchCollectionService(db, settings)
    source = _make_source(collection, account, fund, user, name="连续来源", suffix="continuous")
    _collect(collection, source, user, external_id="first", title="第一条", content="第一条材料")
    ResearchDossierService(db, settings).assemble_pending(now=NOW)

    later = datetime(2026, 9, 14, 6, 0, tzinfo=timezone.utc)
    _collect(
        collection,
        source,
        user,
        external_id="later",
        title="后续材料",
        content="同日稍晚出现的新材料",
        now=later,
    )
    summary = ResearchDossierService(db, settings).assemble_pending(now=later)

    assert summary["dossiers_created"] == 0
    assert summary["documents_deferred"] == 1
    assert db.scalar(select(func.count(ResearchDossier.id))) == 1
    deferred = db.scalar(
        select(ResearchCollectedDocument).where(ResearchCollectedDocument.external_id == "later")
    )
    assert deferred.status == "DEFERRED"
    raw = db.get(ResearchInboxItem, deferred.research_item_id)
    assert raw.status == "DEFERRED"


def test_raw_collected_research_cannot_bypass_dossier(db):
    settings = _settings()
    user, account, fund = _seed(db)
    collection = ResearchCollectionService(db, settings)
    source = _make_source(collection, account, fund, user, name="防绕过来源", suffix="guard")
    _collect(collection, source, user, external_id="guard-1", title="待汇总", content="等待 dossier")
    raw = db.scalar(select(ResearchInboxItem))

    pipeline = ResearchPipelineService(db, settings)
    summary = pipeline.process_pending(now=NOW)
    assert summary["selected"] == 0
    assert db.get(ResearchInboxItem, raw.id).status == "NEW"

    with pytest.raises(ResearchProcessingConflict, match="dossier"):
        pipeline.process(
            raw.id,
            quota_subject="user-1",
            request_id="raw-bypass",
            actor_id=user.id,
            now=NOW,
        )
    assert db.get(ResearchInboxItem, raw.id).attempt == 0


def test_dossier_api_requires_internal_auth(db):
    app = FastAPI()
    app.include_router(dossier_router)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)

    assert client.get("/research/dossiers").status_code == 401
    assert client.post("/research/dossiers/assemble").status_code == 401
