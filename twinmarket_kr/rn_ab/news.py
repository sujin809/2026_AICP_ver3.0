"""Sealed real-news bundle validation for the RN Community AB baseline.

This module intentionally validates identity, provenance, and delivery as
separate facts.  A missing tenth article can be an accepted shortage; a row
swap, fake marker, duplicate payload, or cutoff violation never is.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


class NewsBundleError(RuntimeError):
    pass


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_FAKE_FIELD_RE = re.compile(r"(?:^|_)(?:fake|synthetic|injection|false_claim)(?:_|$)", re.IGNORECASE)
_SYNTHETIC_MARKER_RE = re.compile(
    r"(?:\[\s*fake\s*\]|\b(?:fake|synthetic|fabricated|invented|fictitious)\b|"
    r"(?:가짜|합성|조작된|허위\s*뉴스))",
    re.IGNORECASE,
)
_LEAKAGE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"당일\s*(?:종가|고가|저가)",
        r"장\s*마감(?:\s*후|값)?",
        r"개인(?:투자자)?\s*(?:순매수|순매도|순거래)",
        r"individuals?\s*(?:net\s*)?(?:buy|sell|flow)",
        r"final\s*(?:close|high|low)",
    )
)


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def article_payload_sha256(value: Mapping[str, Any]) -> str:
    """Hash the exact article-version projection that may enter a real-news slot."""
    required = {
        "article_id",
        "title",
        "summary",
        "published_at",
        "observed_at",
        "last_modified_at",
        "source_url",
        "source",
        "raw_body_sha256",
        "version_sha256",
        "cutoff_version_sha256",
    }
    if set(value) != required:
        raise NewsBundleError("Article payload hash input has an invalid exact schema")
    return canonical_sha256(dict(value))


def fake_registry_sha256(*, known_fake_ids: Iterable[str], known_fake_payload_hashes: Iterable[str]) -> str:
    """Hash the immutable fake-isolation closure recorded with a clean bundle."""
    return canonical_sha256(
        {
            "known_fake_ids": list(known_fake_ids),
            "known_fake_payload_hashes": list(known_fake_payload_hashes),
        }
    )


def bundle_content_sha256(value: Mapping[str, Any]) -> str:
    """Hash a bundle body excluding its non-self-referential bundle digest field."""
    body = dict(value)
    body.pop("bundle_sha256", None)
    return canonical_sha256(body)


@dataclass(frozen=True)
class Article:
    article_id: str
    payload_sha256: str
    title: str
    summary: str
    published_at: str
    observed_at: str
    last_modified_at: str | None
    source_url: str
    source: str
    raw_body_sha256: str
    version_sha256: str
    cutoff_version_sha256: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Article":
        _reject_fake_fields(value, "article")
        required = {
            "article_id",
            "payload_sha256",
            "title",
            "summary",
            "published_at",
            "observed_at",
            "last_modified_at",
            "source_url",
            "source",
            "raw_body_sha256",
            "version_sha256",
            "cutoff_version_sha256",
        }
        if set(value) != required:
            raise NewsBundleError(
                f"Article fields must exactly equal provenance schema; missing={sorted(required - set(value))} "
                f"unknown={sorted(set(value) - required)}"
            )
        result = cls(
            article_id=_text(value["article_id"], "article_id"),
            payload_sha256=_sha(value["payload_sha256"], "payload_sha256"),
            title=_text(value["title"], "title"),
            summary=_text(value["summary"], "summary"),
            published_at=_timestamp(value["published_at"], "published_at"),
            observed_at=_timestamp(value["observed_at"], "observed_at"),
            last_modified_at=_optional_timestamp(value["last_modified_at"], "last_modified_at"),
            source_url=_text(value["source_url"], "source_url"),
            source=_text(value["source"], "source"),
            raw_body_sha256=_sha(value["raw_body_sha256"], "raw_body_sha256"),
            version_sha256=_sha(value["version_sha256"], "version_sha256"),
            cutoff_version_sha256=_sha(
                value["cutoff_version_sha256"], "cutoff_version_sha256"
            ),
        )
        if _parse_timestamp(result.observed_at, "observed_at") < _parse_timestamp(
            result.published_at, "published_at"
        ):
            raise NewsBundleError("article observed_at may not precede published_at")
        if result.last_modified_at is not None:
            modified = _parse_timestamp(result.last_modified_at, "last_modified_at")
            if modified < _parse_timestamp(result.published_at, "published_at"):
                raise NewsBundleError("article last_modified_at may not precede published_at")
            if modified > _parse_timestamp(result.observed_at, "observed_at"):
                raise NewsBundleError("article last_modified_at may not follow observed_at snapshot")
        expected_payload_sha = article_payload_sha256(
            {
                "article_id": result.article_id,
                "title": result.title,
                "summary": result.summary,
                "published_at": result.published_at,
                "observed_at": result.observed_at,
                "last_modified_at": result.last_modified_at,
                "source_url": result.source_url,
                "source": result.source,
                "raw_body_sha256": result.raw_body_sha256,
                "version_sha256": result.version_sha256,
                "cutoff_version_sha256": result.cutoff_version_sha256,
            }
        )
        if result.payload_sha256 != expected_payload_sha:
            raise NewsBundleError("Article payload_sha256 does not bind its exact version projection")
        if not re.fullmatch(r"https?://[^\s]+", result.source_url):
            raise NewsBundleError("article source_url must be an http(s) URL")
        _reject_synthetic_marker(result.article_id, field="article_id")
        _reject_synthetic_marker(result.title, field="article title")
        _reject_synthetic_marker(result.summary, field="article summary")
        _scan_text(result.title, field="article title")
        _scan_text(result.summary, field="article summary")
        return result

    def stage_projection(self, *, news_depth: int = 1) -> dict[str, str]:
        """Return the exact agent-visible projection for one news depth.

        D0 is the approved headline-only tier, so the summary key is absent
        rather than present with an empty/redacted value.  D1 and D2 receive
        the same sealed event title+summary projection.  D2's separate recent
        search permission is intentionally not synthesized from this event
        bundle; it needs its own sealed registry before a live run can be GO.
        """
        depth = _news_depth(news_depth)
        projection = {
            "article_id": self.article_id,
            "payload_sha256": self.payload_sha256,
            "title": self.title,
            "published_at": self.published_at,
        }
        if depth >= 1:
            projection["summary"] = self.summary
        return projection


@dataclass(frozen=True)
class NewsSlot:
    event_id: str
    slot_ordinal: int
    article_id: str
    payload_sha256: str


class SealedNewsRegistry:
    """An immutable event-to-real-article map plus accepted shortage records."""

    def __init__(
        self,
        *,
        bundle_sha256: str,
        target_real_count: int,
        articles: Mapping[str, Article],
        slots: Iterable[NewsSlot],
        accepted_shortages: Mapping[str, Mapping[str, Any]],
        fake_registry_sha256_value: str,
        known_fake_ids: Iterable[str] = (),
        known_fake_payload_hashes: Iterable[str] = (),
    ) -> None:
        self.bundle_sha256 = _sha(bundle_sha256, "bundle_sha256")
        if isinstance(target_real_count, bool) or not isinstance(target_real_count, int) or target_real_count < 1:
            raise NewsBundleError("target_real_count must be a positive integer")
        self.target_real_count = target_real_count
        self.articles = dict(articles)
        self.slots_by_event: dict[str, tuple[NewsSlot, ...]] = {}
        rows_by_event: dict[str, list[NewsSlot]] = {}
        for slot in slots:
            rows_by_event.setdefault(slot.event_id, []).append(slot)
        for event_id, rows in rows_by_event.items():
            ordered = tuple(sorted(rows, key=lambda row: row.slot_ordinal))
            self._validate_slot_group(event_id, ordered, accepted_shortages)
            self.slots_by_event[event_id] = ordered
        self.accepted_shortages = {str(key): dict(value) for key, value in accepted_shortages.items()}
        fake_id_rows = [_text(value, "known fake ID") for value in known_fake_ids]
        fake_hash_rows = [_sha(value, "known fake payload hash") for value in known_fake_payload_hashes]
        if len(fake_id_rows) != len(set(fake_id_rows)) or len(fake_hash_rows) != len(set(fake_hash_rows)):
            raise NewsBundleError("Known fake registry contains duplicate IDs or payload hashes")
        if _sha(fake_registry_sha256_value, "fake_registry_sha256") != fake_registry_sha256(
            known_fake_ids=fake_id_rows,
            known_fake_payload_hashes=fake_hash_rows,
        ):
            raise NewsBundleError("Fake-isolation registry hash does not bind its recorded closure")
        fake_ids = set(fake_id_rows)
        fake_hashes = set(fake_hash_rows)
        overlap_ids = fake_ids & set(self.articles)
        overlap_hashes = fake_hashes & {article.payload_sha256 for article in self.articles.values()}
        if overlap_ids or overlap_hashes:
            raise NewsBundleError("Clean real-news registry overlaps known fake IDs or payload hashes")
        self.known_fake_ids = tuple(fake_id_rows)
        self.known_fake_payload_hashes = tuple(fake_hash_rows)
        self.fake_registry_sha256 = _sha(fake_registry_sha256_value, "fake_registry_sha256")
        shortage_events = {
            event_id
            for event_id, slot_rows in self.slots_by_event.items()
            if len(slot_rows) != self.target_real_count
        }
        if set(self.accepted_shortages) != shortage_events:
            raise NewsBundleError("Accepted shortage records must exactly cover and only cover short events")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SealedNewsRegistry":
        _reject_fake_fields(
            value,
            "real-news bundle",
            allowed={
                "fake_news_per_event",
                "known_fake_ids",
                "known_fake_payload_hashes",
                "fake_registry_sha256",
            },
        )
        required = {
            "artifact_type",
            "bundle_sha256",
            "stock_code",
            "target_real_news_per_event",
            "fake_news_per_event",
            "articles",
            "slots",
            "accepted_shortages",
            "known_fake_ids",
            "known_fake_payload_hashes",
            "fake_registry_sha256",
        }
        if set(value) != required:
            raise NewsBundleError("Real-news bundle manifest has an invalid exact schema")
        if value["artifact_type"] != "real_news_bundle_manifest":
            raise NewsBundleError("Real-news bundle has wrong artifact_type")
        if (
            value["stock_code"] != "005930"
            or isinstance(value["fake_news_per_event"], bool)
            or value["fake_news_per_event"] != 0
        ):
            raise NewsBundleError("RN bundle must be 005930 and fake_news_per_event=0")
        if not isinstance(value["articles"], list) or not isinstance(value["slots"], list):
            raise NewsBundleError("Articles and slots must be arrays")
        if _sha(value["bundle_sha256"], "bundle_sha256") != bundle_content_sha256(value):
            raise NewsBundleError("bundle_sha256 does not bind the exact bundle content")
        articles: dict[str, Article] = {}
        for raw in value["articles"]:
            if not isinstance(raw, Mapping):
                raise NewsBundleError("Article registry entry must be an object")
            article = Article.from_mapping(raw)
            if article.article_id in articles:
                raise NewsBundleError("Article registry has duplicate article IDs")
            articles[article.article_id] = article
        slots: list[NewsSlot] = []
        for raw in value["slots"]:
            if not isinstance(raw, Mapping) or set(raw) != {
                "event_id", "slot_ordinal", "article_id", "payload_sha256"
            }:
                raise NewsBundleError("News slot must use exact event/ordinal/article/hash schema")
            if isinstance(raw["slot_ordinal"], bool) or not isinstance(raw["slot_ordinal"], int):
                raise NewsBundleError("slot_ordinal must be an integer")
            slots.append(
                NewsSlot(
                    event_id=_text(raw["event_id"], "slot event_id"),
                    slot_ordinal=int(raw["slot_ordinal"]),
                    article_id=_text(raw["article_id"], "slot article_id"),
                    payload_sha256=_sha(raw["payload_sha256"], "slot payload_sha256"),
                )
            )
        shortages = value["accepted_shortages"]
        if not isinstance(shortages, Mapping):
            raise NewsBundleError("accepted_shortages must be an object indexed by event ID")
        fake_ids = value["known_fake_ids"]
        fake_hashes = value["known_fake_payload_hashes"]
        if not isinstance(fake_ids, list) or not isinstance(fake_hashes, list):
            raise NewsBundleError("known fake registries must be arrays")
        return cls(
            bundle_sha256=_sha(value["bundle_sha256"], "bundle_sha256"),
            target_real_count=value["target_real_news_per_event"],
            articles=articles,
            slots=slots,
            accepted_shortages=shortages,
            fake_registry_sha256_value=value["fake_registry_sha256"],
            known_fake_ids=fake_ids,
            known_fake_payload_hashes=fake_hashes,
        )

    def validate_delivery(
        self,
        *,
        event_id: str,
        delivered: Iterable[Mapping[str, Any]],
        cutoff_timestamp: str,
        news_depth: int = 1,
    ) -> tuple[dict[str, str], ...]:
        """Return the exact safe STB projection or reject a swapped/different payload."""
        depth = _news_depth(news_depth)
        expected = self.slots_by_event.get(event_id)
        if expected is None:
            raise NewsBundleError(f"Event is absent from sealed news slot map: {event_id}")
        delivered_rows = list(delivered)
        if len(delivered_rows) != len(expected):
            raise NewsBundleError("Delivered real-news count differs from the sealed slot map")
        projections: list[dict[str, str]] = []
        cutoff = _parse_timestamp(cutoff_timestamp, "event cutoff timestamp")
        seen_ids: set[str] = set()
        seen_hashes: set[str] = set()
        for expected_slot, raw in zip(expected, delivered_rows, strict=True):
            if not isinstance(raw, Mapping):
                raise NewsBundleError("Delivered news item must be an object")
            _reject_fake_fields(raw, "delivered news item")
            expected_fields = {"article_id", "payload_sha256", "title", "published_at"}
            if depth >= 1:
                expected_fields.add("summary")
            if set(raw) != expected_fields:
                raise NewsBundleError("Delivered news item violates the agent-visible projection schema")
            article_id = _text(raw["article_id"], "delivered article_id")
            payload_hash = _sha(raw["payload_sha256"], "delivered payload_sha256")
            article = self.articles.get(article_id)
            if article is None or article_id != expected_slot.article_id:
                raise NewsBundleError("Delivered article is outside the sealed clean slot")
            if payload_hash != expected_slot.payload_sha256 or payload_hash != article.payload_sha256:
                raise NewsBundleError("Delivered article payload hash differs from frozen article version")
            if article_id in seen_ids or payload_hash in seen_hashes:
                raise NewsBundleError("Delivered event contains duplicate real article or payload")
            seen_ids.add(article_id)
            seen_hashes.add(payload_hash)
            projection = article.stage_projection(news_depth=depth)
            if projection != {key: str(raw[key]) for key in projection}:
                raise NewsBundleError("Delivered text/timestamp differs from frozen article projection")
            if _parse_timestamp(projection["published_at"], "article published_at") > cutoff:
                raise NewsBundleError("Delivered article is later than the sealed event cutoff")
            if _parse_timestamp(article.observed_at, "article observed_at") > cutoff:
                raise NewsBundleError("Delivered article snapshot was observed after the sealed event cutoff")
            if (
                article.last_modified_at is not None
                and _parse_timestamp(article.last_modified_at, "article last_modified_at") > cutoff
            ):
                raise NewsBundleError("Delivered article version was modified after the sealed event cutoff")
            projections.append(projection)
        return tuple(projections)

    def _validate_slot_group(
        self,
        event_id: str,
        slots: tuple[NewsSlot, ...],
        accepted_shortages: Mapping[str, Mapping[str, Any]],
    ) -> None:
        if not slots:
            raise NewsBundleError("Every registered event needs at least one real article")
        if [slot.slot_ordinal for slot in slots] != list(range(1, len(slots) + 1)):
            raise NewsBundleError("News slots must have contiguous 1-based ordinals")
        article_ids = [slot.article_id for slot in slots]
        hashes = [slot.payload_sha256 for slot in slots]
        if len(article_ids) != len(set(article_ids)) or len(hashes) != len(set(hashes)):
            raise NewsBundleError("News slot article IDs/payload hashes must be unique per event")
        for slot in slots:
            article = self.articles.get(slot.article_id)
            if article is None or article.payload_sha256 != slot.payload_sha256:
                raise NewsBundleError("News slot references an unknown or mismatched article payload")
        if len(slots) == self.target_real_count:
            if event_id in accepted_shortages:
                raise NewsBundleError("Complete event must not have a shortage exception")
            return
        shortage = accepted_shortages.get(event_id)
        if len(slots) > self.target_real_count or not isinstance(shortage, Mapping):
            raise NewsBundleError("Non-target real-news count requires an accepted shortage record")
        required = {
            "target_real_count",
            "selected_safe_count",
            "serialized_count",
            "delivered_real_count",
            "actual_real_count",
            "missing_real_count",
            "coverage_status",
            "ordered_article_ids",
            "ordered_payload_sha256",
        }
        if set(shortage) != required:
            raise NewsBundleError("Shortage record violates its exact immutable schema")
        if (
            shortage["target_real_count"] != self.target_real_count
            or shortage["selected_safe_count"] != len(slots)
            or shortage["serialized_count"] != len(slots)
            or shortage["delivered_real_count"] != len(slots)
            or shortage["actual_real_count"] != len(slots)
            or shortage["missing_real_count"] != self.target_real_count - len(slots)
            or shortage["coverage_status"] != "shortage_accepted"
            or list(shortage["ordered_article_ids"]) != article_ids
            or list(shortage["ordered_payload_sha256"]) != hashes
        ):
            raise NewsBundleError("Shortage record does not exactly match its frozen delivered bundle")


def load_sealed_news_registry(
    path: Path | str,
    *,
    expected_file_sha256: str | None = None,
    expected_bundle_sha256: str | None = None,
    allowed_root: Path | str | None = None,
) -> SealedNewsRegistry:
    """Load a registry only from a pinned regular file with no symlink ancestry."""
    if expected_file_sha256 is None and expected_bundle_sha256 is None:
        raise NewsBundleError(
            "A sealed news registry requires an expected file hash or canonical bundle hash"
        )
    source = Path(os.path.abspath(os.fspath(path)))
    _assert_regular_non_symlink_path(source, allowed_root=allowed_root)
    raw_bytes = source.read_bytes()
    actual_file_hash = hashlib.sha256(raw_bytes).hexdigest()
    if expected_file_sha256 is not None and actual_file_hash != _sha(
        expected_file_sha256, "expected_file_sha256"
    ):
        raise NewsBundleError("News registry file hash changed after preflight")
    try:
        payload = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise NewsBundleError("News registry is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise NewsBundleError("News registry root must be an object")
    registry = SealedNewsRegistry.from_mapping(payload)
    if expected_bundle_sha256 is not None and registry.bundle_sha256 != _sha(
        expected_bundle_sha256, "expected_bundle_sha256"
    ):
        raise NewsBundleError("News registry canonical bundle hash differs from the sealed study hash")
    return registry


def _assert_regular_non_symlink_path(source: Path, *, allowed_root: Path | str | None) -> None:
    if allowed_root is not None:
        root = Path(os.path.abspath(os.fspath(allowed_root)))
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise NewsBundleError("News registry is outside its allowed input root") from exc
        lower_bound = root
    else:
        lower_bound = Path(source.anchor)
    cursor = source
    while True:
        try:
            mode = cursor.lstat().st_mode
        except FileNotFoundError as exc:
            raise NewsBundleError("News registry path does not exist") from exc
        if stat.S_ISLNK(mode):
            raise NewsBundleError("News registry paths may not contain symbolic links")
        if cursor == lower_bound:
            break
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    if not source.is_file():
        raise NewsBundleError("News registry must be a regular file")


def _reject_fake_fields(
    value: Mapping[str, Any], label: str, *, allowed: set[str] | None = None
) -> None:
    allowed = allowed or set()
    offenders = [
        str(key)
        for key in value
        if _FAKE_FIELD_RE.search(str(key)) and str(key) not in allowed
    ]
    if offenders:
        raise NewsBundleError(f"{label} contains fake/synthetic metadata fields: {sorted(offenders)}")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NewsBundleError(f"{label} must be a non-empty string")
    return value.strip()


def _news_depth(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in {0, 1, 2}:
        raise NewsBundleError("news_depth must be one of 0, 1, or 2")
    return value


def _sha(value: Any, label: str) -> str:
    text = _text(value, label).lower()
    if not _SHA256_RE.fullmatch(text):
        raise NewsBundleError(f"{label} must be a SHA-256 hex digest")
    return text


def _timestamp(value: Any, label: str) -> str:
    return _parse_timestamp(value, label).isoformat()


def _optional_timestamp(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _timestamp(value, label)


def _parse_timestamp(value: Any, label: str) -> datetime:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NewsBundleError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise NewsBundleError(f"{label} must include a timezone offset")
    return parsed


def _reject_synthetic_marker(value: str, *, field: str) -> None:
    normalized = _normalized_visible_text(value)
    if _SYNTHETIC_MARKER_RE.search(normalized):
        raise NewsBundleError(f"{field} contains a synthetic/fake marker")


def _scan_text(text: str, *, field: str) -> None:
    normalized = _normalized_visible_text(text)
    if any(pattern.search(normalized) for pattern in _LEAKAGE_PATTERNS):
        raise NewsBundleError(f"{field} contains a prohibited EOD/target leakage pattern")


def _normalized_visible_text(value: str) -> str:
    """Canonicalize invisible formatting before every visible-text safety scan."""
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(character for character in normalized if unicodedata.category(character) != "Cf")
    return " ".join(normalized.split())
