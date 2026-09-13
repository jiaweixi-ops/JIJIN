from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.collection_models import (
    ResearchCollectedDocument,
    ResearchCollectionRun,
    ResearchCollectionSource,
)
from app.collection_schemas import ResearchCollectionSourceCreate, ResearchCollectionSourcePatch
from app.config import Settings
from app.enums import AccountType
from app.models import Account, AuditLog, Fund
from app.research_schemas import ResearchInboxCreate, ResearchMaterialIn
from app.services.research_pipeline import ResearchPipelineService

ALLOWED_CONTENT_TYPES = {
    "application/atom+xml",
    "application/feed+json",
    "application/json",
    "application/rss+xml",
    "application/xml",
    "text/xml",
}


class ResearchCollectionError(RuntimeError):
    pass


class UnsafeCollectionURL(ValueError):
    pass


@dataclass(frozen=True)
class CollectionHTTPResult:
    status_code: int
    content: bytes
    content_type: str
    final_url: str
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class NormalizedFeedEntry:
    external_id: str
    title: str
    url: str | None
    published_at: datetime | None
    content: str


class CollectionFetcher(Protocol):
    def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> CollectionHTTPResult: ...


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)


def _html_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(value)
    parser.close()
    return "\n".join(parser.parts).strip()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_date(value: str | None) -> datetime | None:
    if not value or not value.strip():
        return None
    text = value.strip()
    try:
        return _as_utc(parsedate_to_datetime(text))
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return _as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _public_url_syntax(url: str, *, allow_http: bool) -> str:
    parsed = urlparse(url.strip())
    allowed_schemes = {"https"} | ({"http"} if allow_http else set())
    if parsed.scheme.lower() not in allowed_schemes:
        raise UnsafeCollectionURL("collection URL must use HTTPS")
    if not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeCollectionURL("collection URL must have a hostname and no embedded credentials")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise UnsafeCollectionURL("localhost collection targets are forbidden")
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None
    if ip is not None and not ip.is_global:
        raise UnsafeCollectionURL("private/link-local/reserved collection targets are forbidden")
    return url.strip()


def _assert_public_dns(url: str, *, allow_http: bool) -> None:
    checked = _public_url_syntax(url, allow_http=allow_http)
    hostname = urlparse(checked).hostname
    if hostname is None:
        raise UnsafeCollectionURL("collection URL hostname missing")
    try:
        addresses = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ResearchCollectionError(f"collection hostname cannot be resolved: {hostname}") from exc
    if not addresses:
        raise ResearchCollectionError(f"collection hostname has no addresses: {hostname}")
    for row in addresses:
        address = row[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise UnsafeCollectionURL("collection hostname resolved to an invalid address") from exc
        if not ip.is_global:
            raise UnsafeCollectionURL(
                f"collection hostname resolved to non-public address: {hostname}"
            )


class SafeHTTPCollectionFetcher:
    def __init__(self, settings: Settings):
        self.settings = settings

    def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> CollectionHTTPResult:
        current_url = url
        headers = {
            "Accept": "application/rss+xml, application/atom+xml, application/feed+json, application/json, application/xml, text/xml",
            "User-Agent": "JIJIN-ResearchCollector/1.3",
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        with httpx.Client(
            timeout=self.settings.research_collection_timeout_seconds,
            follow_redirects=False,
        ) as client:
            for redirect_count in range(self.settings.research_collection_max_redirects + 1):
                _assert_public_dns(
                    current_url,
                    allow_http=self.settings.research_collection_allow_http,
                )
                try:
                    with client.stream("GET", current_url, headers=headers) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if redirect_count >= self.settings.research_collection_max_redirects:
                                raise ResearchCollectionError("collection redirect limit exceeded")
                            location = response.headers.get("location")
                            if not location:
                                raise ResearchCollectionError("collection redirect missing Location")
                            current_url = urljoin(current_url, location)
                            continue
                        if response.status_code == 304:
                            return CollectionHTTPResult(
                                status_code=304,
                                content=b"",
                                content_type="",
                                final_url=current_url,
                                etag=response.headers.get("etag") or etag,
                                last_modified=response.headers.get("last-modified") or last_modified,
                            )
                        response.raise_for_status()
                        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        if media_type not in ALLOWED_CONTENT_TYPES:
                            raise ResearchCollectionError(
                                f"unsupported collection Content-Type: {media_type or '<missing>'}"
                            )
                        chunks: list[bytes] = []
                        total = 0
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > self.settings.research_collection_max_bytes:
                                raise ResearchCollectionError("collection response exceeds byte limit")
                            chunks.append(chunk)
                        return CollectionHTTPResult(
                            status_code=response.status_code,
                            content=b"".join(chunks),
                            content_type=media_type,
                            final_url=current_url,
                            etag=response.headers.get("etag"),
                            last_modified=response.headers.get("last-modified"),
                        )
                except httpx.HTTPError as exc:
                    raise ResearchCollectionError(f"collection HTTP failure: {exc}") from exc
        raise ResearchCollectionError("collection fetch terminated unexpectedly")


def _element_text(element: ET.Element, names: tuple[str, ...]) -> str:
    for child in element.iter():
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local in names and child.text and child.text.strip():
            return child.text.strip()
    return ""


def _entry_content(element: ET.Element) -> str:
    candidates: list[str] = []
    for child in element.iter():
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local in {"encoded", "content", "description", "summary"}:
            raw = "".join(child.itertext()).strip()
            if raw:
                candidates.append(raw)
    if not candidates:
        return ""
    value = max(candidates, key=len)
    text = _html_to_text(value)
    return text or html.unescape(value).strip()


def _entry_link(element: ET.Element) -> str | None:
    for child in element.iter():
        if child.tag.rsplit("}", 1)[-1].lower() != "link":
            continue
        href = (child.attrib.get("href") or "").strip()
        if href:
            rel = (child.attrib.get("rel") or "alternate").lower()
            if rel in {"alternate", ""}:
                return href
        if child.text and child.text.strip():
            return child.text.strip()
    return None


def parse_rss_or_atom(content: bytes) -> list[NormalizedFeedEntry]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ResearchCollectionError(f"invalid RSS/Atom XML: {exc}") from exc

    elements = [
        element
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}
    ]
    result: list[NormalizedFeedEntry] = []
    for element in elements:
        title = _element_text(element, ("title",)) or "未命名研究材料"
        link = _entry_link(element)
        external_id = _element_text(element, ("guid", "id")) or link or ""
        published = _parse_date(
            _element_text(element, ("pubdate", "published", "updated", "date")) or None
        )
        body = _entry_content(element) or title
        if not external_id:
            external_id = hashlib.sha256(
                f"{title}|{published.isoformat() if published else ''}|{body}".encode("utf-8")
            ).hexdigest()
        result.append(
            NormalizedFeedEntry(
                external_id=external_id[:512],
                title=title[:500],
                url=link,
                published_at=published,
                content=body,
            )
        )
    return result


def parse_json_feed(content: bytes) -> list[NormalizedFeedEntry]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchCollectionError(f"invalid JSON feed: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ResearchCollectionError("JSON feed must contain an items array")
    result: list[NormalizedFeedEntry] = []
    for raw in payload["items"]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "未命名研究材料").strip()[:500]
        link = str(raw.get("url") or raw.get("external_url") or "").strip() or None
        body_raw = str(
            raw.get("content_text")
            or raw.get("content_html")
            or raw.get("summary")
            or title
        )
        body = _html_to_text(body_raw) or body_raw.strip()
        published = _parse_date(
            str(raw.get("date_published") or raw.get("date_modified") or "") or None
        )
        external_id = str(raw.get("id") or link or "").strip()
        if not external_id:
            external_id = hashlib.sha256(
                f"{title}|{published.isoformat() if published else ''}|{body}".encode("utf-8")
            ).hexdigest()
        result.append(
            NormalizedFeedEntry(
                external_id=external_id[:512],
                title=title,
                url=link,
                published_at=published,
                content=body,
            )
        )
    return result


class ResearchCollectionService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.pipeline = ResearchPipelineService(db, settings)

    def _audit(
        self,
        action: str,
        target_id: str,
        *,
        actor_id: str | None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.db.add(
            AuditLog(
                actor_type="user" if actor_id else "system",
                actor_id=actor_id,
                action=action,
                target_type="research_collection_source",
                target_id=target_id,
                payload=payload or {},
            )
        )

    def create_source(
        self,
        payload: ResearchCollectionSourceCreate,
        *,
        actor_id: str | None,
        now: datetime | None = None,
    ) -> ResearchCollectionSource:
        now_utc = _as_utc(now or datetime.now(timezone.utc))
        account = self.db.get(Account, payload.account_id)
        fund = self.db.get(Fund, payload.fund_id)
        if account is None:
            raise KeyError("account not found")
        if fund is None:
            raise KeyError("fund not found")
        if account.account_type != AccountType.SIMULATION or not account.enabled:
            raise ValueError("collection source must target an enabled simulation account")
        feed_url = _public_url_syntax(
            payload.feed_url,
            allow_http=self.settings.research_collection_allow_http,
        )
        source = ResearchCollectionSource(
            account_id=payload.account_id,
            fund_id=payload.fund_id,
            name=payload.name,
            adapter=payload.adapter,
            feed_url=feed_url,
            topic_prefix=payload.topic_prefix,
            enabled=payload.enabled,
            created_by=actor_id,
            last_error="",
            created_at=now_utc,
            updated_at=now_utc,
        )
        self.db.add(source)
        try:
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            raise ValueError("collection source already exists for this account/fund/feed URL") from exc
        self._audit(
            "research.collection_source.create",
            source.id,
            actor_id=actor_id,
            payload={"adapter": source.adapter, "feed_url": source.feed_url},
        )
        self.db.commit()
        return source

    def patch_source(
        self,
        source_id: str,
        payload: ResearchCollectionSourcePatch,
        *,
        actor_id: str | None,
        now: datetime | None = None,
    ) -> ResearchCollectionSource:
        source = self.db.get(ResearchCollectionSource, source_id)
        if source is None:
            raise KeyError(source_id)
        changes = payload.model_dump(exclude_unset=True)
        if "feed_url" in changes:
            changes["feed_url"] = _public_url_syntax(
                changes["feed_url"],
                allow_http=self.settings.research_collection_allow_http,
            )
            source.etag = None
            source.last_modified = None
        for key, value in changes.items():
            setattr(source, key, value)
        source.updated_at = _as_utc(now or datetime.now(timezone.utc))
        self._audit(
            "research.collection_source.patch",
            source.id,
            actor_id=actor_id,
            payload={"changed_fields": sorted(changes)},
        )
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise ValueError("collection source conflicts with an existing source") from exc
        return source

    def _parse(self, source: ResearchCollectionSource, response: CollectionHTTPResult) -> list[NormalizedFeedEntry]:
        if source.adapter == "rss":
            return parse_rss_or_atom(response.content)
        if source.adapter == "json_feed":
            return parse_json_feed(response.content)
        raise ResearchCollectionError(f"unsupported collection adapter: {source.adapter}")

    @staticmethod
    def _canonical_article_url(value: str | None) -> str | None:
        if not value:
            return None
        parsed = urlparse(value.strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        return value.strip()

    def collect_source(
        self,
        source_id: str,
        *,
        trigger: str = "manual",
        actor_id: str | None = None,
        now: datetime | None = None,
        fetcher: CollectionFetcher | None = None,
    ) -> ResearchCollectionRun:
        now_utc = _as_utc(now or datetime.now(timezone.utc))
        source = self.db.get(ResearchCollectionSource, source_id)
        if source is None:
            raise KeyError(source_id)
        run = ResearchCollectionRun(
            source_id=source.id,
            trigger=trigger,
            status="RUNNING",
            started_at=now_utc,
        )
        self.db.add(run)
        self.db.commit()

        if not source.enabled:
            run.status = "SKIPPED"
            run.error = "SOURCE_DISABLED"
            run.finished_at = now_utc
            source.last_checked_at = now_utc
            source.updated_at = now_utc
            self.db.commit()
            return run

        actual_fetcher = fetcher or SafeHTTPCollectionFetcher(self.settings)
        try:
            response = actual_fetcher.fetch(
                source.feed_url,
                etag=source.etag,
                last_modified=source.last_modified,
            )
            run.http_status = response.status_code
            source.last_checked_at = now_utc
            if response.etag:
                source.etag = response.etag
            if response.last_modified:
                source.last_modified = response.last_modified

            if response.status_code == 304:
                source.last_success_at = now_utc
                source.last_error = ""
                source.updated_at = now_utc
                run.status = "SUCCEEDED"
                run.finished_at = now_utc
                self.db.commit()
                return run

            entries = self._parse(source, response)[: self.settings.research_collection_max_items_per_source]
            run.fetched_count = len(entries)
            for entry in entries:
                content = entry.content.strip()
                if not content:
                    run.rejected_count += 1
                    continue
                if len(content) > min(self.settings.ai_max_source_chars, 100_000):
                    run.rejected_count += 1
                    continue
                published_at = _as_utc(entry.published_at) if entry.published_at else None
                if published_at and published_at > now_utc + timedelta(minutes=5):
                    run.rejected_count += 1
                    continue

                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                fingerprint = hashlib.sha256(
                    f"{entry.external_id}|{content_hash}".encode("utf-8")
                ).hexdigest()
                existing = self.db.scalar(
                    select(ResearchCollectedDocument.id).where(
                        ResearchCollectedDocument.source_id == source.id,
                        ResearchCollectedDocument.fingerprint == fingerprint,
                    )
                )
                if existing is not None:
                    run.duplicate_count += 1
                    continue

                article_url = self._canonical_article_url(entry.url) or source.feed_url
                topic = f"{source.topic_prefix}：{entry.title}"[:300]
                inbox = self.pipeline.ingest(
                    ResearchInboxCreate(
                        account_id=source.account_id,
                        fund_id=source.fund_id,
                        topic=topic,
                        idempotency_key=f"collect-{fingerprint}",
                        materials=[
                            ResearchMaterialIn(
                                source_name=source.name,
                                source_url=article_url,
                                published_at=published_at,
                                observed_at=now_utc,
                                content=content,
                            )
                        ],
                    ),
                    actor_id=actor_id,
                    now=now_utc,
                )
                document = ResearchCollectedDocument(
                    source_id=source.id,
                    research_item_id=inbox.id,
                    external_id=entry.external_id,
                    fingerprint=fingerprint,
                    content_sha256=content_hash,
                    title=entry.title,
                    canonical_url=article_url,
                    published_at=published_at,
                    observed_at=now_utc,
                    content=content,
                    status="INGESTED",
                    error="",
                    created_at=now_utc,
                )
                self.db.add(document)
                try:
                    self.db.commit()
                    run.ingested_count += 1
                except IntegrityError:
                    self.db.rollback()
                    run = self.db.get(ResearchCollectionRun, run.id)
                    source = self.db.get(ResearchCollectionSource, source.id)
                    if run is None or source is None:
                        raise ResearchCollectionError("collection state disappeared after dedupe race")
                    run.duplicate_count += 1

            run = self.db.get(ResearchCollectionRun, run.id)
            source = self.db.get(ResearchCollectionSource, source.id)
            if run is None or source is None:
                raise ResearchCollectionError("collection state disappeared before finalization")
            source.last_success_at = now_utc
            source.last_error = ""
            source.updated_at = now_utc
            run.status = "SUCCEEDED"
            run.finished_at = now_utc
            self._audit(
                "research.collection_source.collect",
                source.id,
                actor_id=actor_id,
                payload={
                    "run_id": run.id,
                    "fetched": run.fetched_count,
                    "ingested": run.ingested_count,
                    "duplicates": run.duplicate_count,
                    "rejected": run.rejected_count,
                },
            )
            self.db.commit()
            return run
        except Exception as exc:
            self.db.rollback()
            run = self.db.get(ResearchCollectionRun, run.id)
            source = self.db.get(ResearchCollectionSource, source.id)
            message = f"{type(exc).__name__}: {exc}"
            if run is not None:
                run.status = "FAILED"
                run.error = message
                run.finished_at = now_utc
            if source is not None:
                source.last_checked_at = now_utc
                source.last_error = message
                source.updated_at = now_utc
            self.db.commit()
            if isinstance(exc, (ResearchCollectionError, UnsafeCollectionURL)):
                raise
            raise ResearchCollectionError(message) from exc

    def collect_enabled(
        self,
        *,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        batch_size = limit or self.settings.research_collection_batch_size
        sources = self.db.scalars(
            select(ResearchCollectionSource)
            .where(ResearchCollectionSource.enabled.is_(True))
            .order_by(ResearchCollectionSource.updated_at, ResearchCollectionSource.id)
            .limit(batch_size)
        ).all()
        summary: dict[str, Any] = {
            "selected": len(sources),
            "succeeded": 0,
            "failed": 0,
            "ingested": 0,
            "duplicates": 0,
            "rejected": 0,
            "runs": [],
        }
        for source in sources:
            try:
                run = self.collect_source(
                    source.id,
                    trigger="scheduler",
                    actor_id=None,
                    now=now,
                )
                summary["succeeded"] += int(run.status == "SUCCEEDED")
                summary["ingested"] += run.ingested_count
                summary["duplicates"] += run.duplicate_count
                summary["rejected"] += run.rejected_count
                summary["runs"].append({"source_id": source.id, "run_id": run.id, "status": run.status})
            except Exception as exc:
                summary["failed"] += 1
                summary["runs"].append(
                    {"source_id": source.id, "status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}
                )
        return summary
