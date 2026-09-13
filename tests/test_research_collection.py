from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.collection import router as collection_router
from app.collection_models import ResearchCollectedDocument, ResearchCollectionSource
from app.collection_schemas import ResearchCollectionSourceCreate
from app.config import Settings
from app.db import get_db
from app.enums import AccountType, Role
from app.models import Account, Fund, User
from app.research_models import ResearchInboxItem
from app.services.research_collection import (
    CollectionHTTPResult,
    ResearchCollectionService,
    UnsafeCollectionURL,
    parse_json_feed,
)

NOW = datetime(2026, 9, 13, 5, 0, tzinfo=timezone.utc)
RSS = b"""<?xml version='1.0' encoding='utf-8'?>
<rss version='2.0'>
  <channel>
    <title>Official Fund Research</title>
    <item>
      <guid>article-001</guid>
      <title>Fund allocation update</title>
      <link>https://example.com/articles/001</link>
      <pubDate>Sun, 13 Sep 2026 04:00:00 +0000</pubDate>
      <description><![CDATA[<p>Official research material A.</p>]]></description>
    </item>
  </channel>
</rss>
"""


def _settings() -> Settings:
    return Settings(
        app_env="test",
        research_collection_timeout_seconds=1,
        research_collection_max_bytes=100_000,
        research_collection_max_redirects=1,
        research_collection_max_items_per_source=10,
        research_collection_batch_size=10,
        research_collection_allow_http=False,
        ai_max_source_chars=100_000,
    )


def _seed(db):
    user = User(display_name="collector-admin", role=Role.ADMIN, active=True)
    db.add(user)
    db.flush()
    account = Account(
        user_id=user.id,
        name="collection-sim",
        account_type=AccountType.SIMULATION,
        available_cash=Decimal("100000"),
        enabled=True,
    )
    fund = Fund(code="C001", name="采集测试基金", share_class="C")
    db.add_all([account, fund])
    db.commit()
    return user, account, fund


class FakeFetcher:
    def __init__(self, result: CollectionHTTPResult):
        self.result = result
        self.calls: list[tuple[str, str | None, str | None]] = []

    def fetch(self, url: str, *, etag: str | None = None, last_modified: str | None = None):
        self.calls.append((url, etag, last_modified))
        return self.result


def _source_payload(account: Account, fund: Fund) -> ResearchCollectionSourceCreate:
    return ResearchCollectionSourceCreate(
        account_id=account.id,
        fund_id=fund.id,
        name="official-feed",
        adapter="rss",
        feed_url="https://example.com/feed.xml",
        topic_prefix="官方研究",
    )


def test_rss_collection_creates_durable_document_and_idempotent_inbox(db):
    settings = _settings()
    user, account, fund = _seed(db)
    service = ResearchCollectionService(db, settings)
    source = service.create_source(_source_payload(account, fund), actor_id=user.id, now=NOW)
    fetcher = FakeFetcher(
        CollectionHTTPResult(
            status_code=200,
            content=RSS,
            content_type="application/rss+xml",
            final_url=source.feed_url,
            etag='"v1"',
            last_modified="Sun, 13 Sep 2026 04:30:00 GMT",
        )
    )

    run = service.collect_source(source.id, actor_id=user.id, now=NOW, fetcher=fetcher)

    assert run.status == "SUCCEEDED"
    assert run.fetched_count == 1
    assert run.ingested_count == 1
    assert run.duplicate_count == 0
    assert db.scalar(select(func.count(ResearchCollectedDocument.id))) == 1
    assert db.scalar(select(func.count(ResearchInboxItem.id))) == 1
    document = db.scalar(select(ResearchCollectedDocument))
    inbox = db.scalar(select(ResearchInboxItem))
    assert document.research_item_id == inbox.id
    assert document.content == "Official research material A."
    assert inbox.status == "NEW"
    assert inbox.materials[0]["source_name"] == "official-feed"
    assert inbox.materials[0]["source_url"] == "https://example.com/articles/001"

    replay = service.collect_source(source.id, actor_id=user.id, now=NOW, fetcher=fetcher)
    assert replay.status == "SUCCEEDED"
    assert replay.ingested_count == 0
    assert replay.duplicate_count == 1
    assert db.scalar(select(func.count(ResearchCollectedDocument.id))) == 1
    assert db.scalar(select(func.count(ResearchInboxItem.id))) == 1


def test_conditional_not_modified_creates_no_new_inbox(db):
    settings = _settings()
    user, account, fund = _seed(db)
    service = ResearchCollectionService(db, settings)
    source = service.create_source(_source_payload(account, fund), actor_id=user.id, now=NOW)
    source.etag = '"v1"'
    db.commit()
    fetcher = FakeFetcher(
        CollectionHTTPResult(
            status_code=304,
            content=b"",
            content_type="",
            final_url=source.feed_url,
            etag='"v1"',
        )
    )

    run = service.collect_source(source.id, actor_id=user.id, now=NOW, fetcher=fetcher)

    assert run.status == "SUCCEEDED"
    assert run.http_status == 304
    assert run.fetched_count == 0
    assert db.scalar(select(func.count(ResearchInboxItem.id))) == 0
    assert fetcher.calls[0][1] == '"v1"'


def test_collection_source_rejects_private_and_plain_http_targets(db):
    settings = _settings()
    user, account, fund = _seed(db)
    service = ResearchCollectionService(db, settings)

    for url in ("https://127.0.0.1/feed", "http://example.com/feed"):
        payload = _source_payload(account, fund).model_copy(update={"feed_url": url})
        with pytest.raises(UnsafeCollectionURL):
            service.create_source(payload, actor_id=user.id, now=NOW)

    assert db.scalar(select(func.count(ResearchCollectionSource.id))) == 0


def test_json_feed_parser_normalizes_html_and_dates():
    entries = parse_json_feed(
        b'{"version":"https://jsonfeed.org/version/1.1","items":[{"id":"j1","url":"https://example.com/j1","title":"JSON item","content_html":"<p>Hello <b>fund</b></p>","date_published":"2026-09-13T04:00:00Z"}]}'
    )
    assert len(entries) == 1
    assert entries[0].external_id == "j1"
    assert entries[0].content == "Hello\nfund"
    assert entries[0].published_at == datetime(2026, 9, 13, 4, 0, tzinfo=timezone.utc)


def test_collection_endpoints_require_internal_auth(db):
    app = FastAPI()
    app.include_router(collection_router)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)

    assert client.get("/research/collection/sources").status_code == 401
    assert client.post("/research/collection/sources/missing/collect").status_code == 401
