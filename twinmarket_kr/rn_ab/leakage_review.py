"""Sealed article-version leakage-review evidence for RN real-news inputs.

The runtime bundle proves which article versions may reach an agent.  This
module keeps two deliberately separate review ledgers:

* ``runtime_reviews``: exactly one *allow* decision for every bundle article;
* ``candidate_reviews``: rejected/masked scanner candidates retained for audit,
  including the documented end-of-day leakage counterexample.

Neither a scanner label nor a reviewer identifier is treated as a cryptographic
approval.  The artifact is an immutable evidence record whose canonical digest
is pinned by ``StudySpec``; a signed external approval authority remains a
separate Go gate.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from twinmarket_kr.rn_ab.news import Article, SealedNewsRegistry
from twinmarket_kr.rn_ab.resolver import PathSafetyError, assert_safe_input_file, canonical_sha256


class LeakageReviewError(RuntimeError):
    """Raised when a leakage-review approval artifact is malformed or stale."""


_ROOT_FIELDS = {
    "artifact_type",
    "version",
    "real_news_bundle_manifest_sha256",
    "calendar_event_registry_sha256",
    "stage_input_registry_canonical_sha256",
    "scanner",
    "runtime_reviews",
    "candidate_reviews",
}
_SCANNER_FIELDS = {"scanner_id", "scanner_version", "scanner_sha256", "executed_at"}
_RUNTIME_REVIEW_FIELDS = {
    "article_id",
    "payload_sha256",
    "version_sha256",
    "cutoff_version_sha256",
    "decision",
    "reason",
    "reviewer_id",
    "reviewer_credential_sha256",
    "reviewed_at",
}
_CANDIDATE_REVIEW_FIELDS = _RUNTIME_REVIEW_FIELDS | {"candidate_id", "scanner_result"}
_KNOWN_EOD_COUNTEREXAMPLE_ID = "news_20260427_섹터_0032"
_SHA256_LENGTH = 64


@dataclass(frozen=True)
class ArticleReview:
    """One non-agent-visible review decision for an immutable article version."""

    article_id: str
    payload_sha256: str
    version_sha256: str
    cutoff_version_sha256: str
    decision: str
    reason: str
    reviewer_id: str
    reviewer_credential_sha256: str
    reviewed_at: str

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        index: int,
        label: str,
    ) -> "ArticleReview":
        if not isinstance(value, Mapping) or set(value) != _RUNTIME_REVIEW_FIELDS:
            raise LeakageReviewError(f"{label}[{index}] has an invalid exact schema")
        decision = _decision(value["decision"], f"{label}[{index}].decision")
        return cls(
            article_id=_text(value["article_id"], f"{label}[{index}].article_id"),
            payload_sha256=_sha256(value["payload_sha256"], f"{label}[{index}].payload_sha256"),
            version_sha256=_sha256(value["version_sha256"], f"{label}[{index}].version_sha256"),
            cutoff_version_sha256=_sha256(
                value["cutoff_version_sha256"], f"{label}[{index}].cutoff_version_sha256"
            ),
            decision=decision,
            reason=_text(value["reason"], f"{label}[{index}].reason"),
            reviewer_id=_text(value["reviewer_id"], f"{label}[{index}].reviewer_id"),
            reviewer_credential_sha256=_sha256(
                value["reviewer_credential_sha256"],
                f"{label}[{index}].reviewer_credential_sha256",
            ),
            reviewed_at=_timestamp(value["reviewed_at"], f"{label}[{index}].reviewed_at"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "article_id": self.article_id,
            "payload_sha256": self.payload_sha256,
            "version_sha256": self.version_sha256,
            "cutoff_version_sha256": self.cutoff_version_sha256,
            "decision": self.decision,
            "reason": self.reason,
            "reviewer_id": self.reviewer_id,
            "reviewer_credential_sha256": self.reviewer_credential_sha256,
            "reviewed_at": self.reviewed_at,
        }


@dataclass(frozen=True)
class CandidateReview:
    """A scanner candidate retained even when it must never enter runtime."""

    candidate_id: str
    scanner_result: str
    review: ArticleReview

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, index: int) -> "CandidateReview":
        if not isinstance(value, Mapping) or set(value) != _CANDIDATE_REVIEW_FIELDS:
            raise LeakageReviewError(f"candidate_reviews[{index}] has an invalid exact schema")
        base = {key: value[key] for key in _RUNTIME_REVIEW_FIELDS}
        return cls(
            candidate_id=_text(value["candidate_id"], f"candidate_reviews[{index}].candidate_id"),
            scanner_result=_text(value["scanner_result"], f"candidate_reviews[{index}].scanner_result"),
            review=ArticleReview.from_mapping(base, index=index, label="candidate_reviews"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "candidate_id": self.candidate_id,
            "scanner_result": self.scanner_result,
            **self.review.to_dict(),
        }


@dataclass(frozen=True)
class ReviewScanner:
    """Versioned scanner provenance; scanner results are never agent-visible."""

    scanner_id: str
    scanner_version: str
    scanner_sha256: str
    executed_at: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ReviewScanner":
        if not isinstance(value, Mapping) or set(value) != _SCANNER_FIELDS:
            raise LeakageReviewError("Leakage-review scanner has an invalid exact schema")
        return cls(
            scanner_id=_text(value["scanner_id"], "scanner.scanner_id"),
            scanner_version=_text(value["scanner_version"], "scanner.scanner_version"),
            scanner_sha256=_sha256(value["scanner_sha256"], "scanner.scanner_sha256"),
            executed_at=_timestamp(value["executed_at"], "scanner.executed_at"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "scanner_id": self.scanner_id,
            "scanner_version": self.scanner_version,
            "scanner_sha256": self.scanner_sha256,
            "executed_at": self.executed_at,
        }


@dataclass(frozen=True)
class ArticleVersionLeakageReviewManifest:
    """Versioned review evidence for the exact bundle/calendar/stage context."""

    version: str
    real_news_bundle_manifest_sha256: str
    calendar_event_registry_sha256: str
    stage_input_registry_canonical_sha256: str
    scanner: ReviewScanner
    runtime_reviews: tuple[ArticleReview, ...]
    candidate_reviews: tuple[CandidateReview, ...]
    canonical_sha256: str

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        news: SealedNewsRegistry,
        expected_sha256: str,
        expected_calendar_event_registry_sha256: str,
        expected_stage_input_registry_canonical_sha256: str,
    ) -> "ArticleVersionLeakageReviewManifest":
        if not isinstance(value, Mapping) or set(value) != _ROOT_FIELDS:
            raise LeakageReviewError("Article leakage-review manifest has an invalid exact schema")
        if _text(value["artifact_type"], "artifact_type") != "article_version_leakage_review_manifest":
            raise LeakageReviewError("Article leakage-review manifest artifact_type is invalid")
        if _text(value["version"], "version") != "article-review-v2":
            raise LeakageReviewError("Article leakage-review manifest version is not approved")
        bundle_sha = _sha256(
            value["real_news_bundle_manifest_sha256"], "real_news_bundle_manifest_sha256"
        )
        if bundle_sha != news.bundle_sha256:
            raise LeakageReviewError("Leakage-review manifest is bound to a different real-news bundle")
        calendar_sha = _sha256(
            value["calendar_event_registry_sha256"], "calendar_event_registry_sha256"
        )
        if calendar_sha != _sha256(
            expected_calendar_event_registry_sha256,
            "expected calendar_event_registry_sha256",
        ):
            raise LeakageReviewError("Leakage-review manifest is bound to a different decision calendar")
        stage_sha = _sha256(
            value["stage_input_registry_canonical_sha256"],
            "stage_input_registry_canonical_sha256",
        )
        if stage_sha != _sha256(
            expected_stage_input_registry_canonical_sha256,
            "expected stage_input_registry_canonical_sha256",
        ):
            raise LeakageReviewError("Leakage-review manifest is bound to different stage inputs")
        raw_runtime = value["runtime_reviews"]
        raw_candidates = value["candidate_reviews"]
        if not isinstance(raw_runtime, list) or not raw_runtime:
            raise LeakageReviewError("Leakage-review runtime_reviews must be a non-empty ordered array")
        if not isinstance(raw_candidates, list) or not raw_candidates:
            raise LeakageReviewError("Leakage-review candidate_reviews must be a non-empty ordered array")
        result = cls(
            version="article-review-v2",
            real_news_bundle_manifest_sha256=bundle_sha,
            calendar_event_registry_sha256=calendar_sha,
            stage_input_registry_canonical_sha256=stage_sha,
            scanner=ReviewScanner.from_mapping(value["scanner"]),
            runtime_reviews=tuple(
                ArticleReview.from_mapping(raw, index=index, label="runtime_reviews")
                for index, raw in enumerate(raw_runtime)
            ),
            candidate_reviews=tuple(
                CandidateReview.from_mapping(raw, index=index)
                for index, raw in enumerate(raw_candidates)
            ),
            canonical_sha256=canonical_sha256(value),
        )
        if result.canonical_sha256 != _sha256(expected_sha256, "expected leakage-review manifest sha256"):
            raise LeakageReviewError("Leakage-review manifest canonical hash differs from StudySpec")
        result.assert_covers_bundle(news)
        result.assert_candidate_evidence()
        return result

    def assert_covers_bundle(self, news: SealedNewsRegistry) -> None:
        """Require one exact allow-review for every potentially visible article."""

        review_by_id: dict[str, ArticleReview] = {}
        for review in self.runtime_reviews:
            if review.article_id in review_by_id:
                raise LeakageReviewError("Leakage-review runtime_reviews has duplicate article IDs")
            review_by_id[review.article_id] = review
        article_ids = set(news.articles)
        if set(review_by_id) != article_ids:
            missing = sorted(article_ids - set(review_by_id))
            extra = sorted(set(review_by_id) - article_ids)
            raise LeakageReviewError(
                "Leakage-review coverage differs from the sealed article registry: "
                f"missing={missing[:3]} extra={extra[:3]}"
            )
        for article_id, article in news.articles.items():
            self._assert_matches_article(review_by_id[article_id], article)

    def assert_candidate_evidence(self) -> None:
        """Keep rejected/masked scanner evidence separate from runtime allow rows."""

        candidate_ids: set[str] = set()
        known_counterexample: CandidateReview | None = None
        for candidate in self.candidate_reviews:
            if candidate.candidate_id in candidate_ids:
                raise LeakageReviewError("Leakage-review candidate_reviews has duplicate candidate IDs")
            candidate_ids.add(candidate.candidate_id)
            if candidate.candidate_id == _KNOWN_EOD_COUNTEREXAMPLE_ID:
                known_counterexample = candidate
        if known_counterexample is None:
            raise LeakageReviewError(
                "Leakage-review candidate evidence omits the documented EOD counterexample"
            )
        if known_counterexample.review.decision not in {"mask", "reject"}:
            raise LeakageReviewError("Documented EOD counterexample must be masked or rejected")

    @staticmethod
    def _assert_matches_article(review: ArticleReview, article: Article) -> None:
        if review.decision != "allow":
            raise LeakageReviewError("A masked/rejected article may not be present in the runtime bundle")
        if (
            review.payload_sha256 != article.payload_sha256
            or review.version_sha256 != article.version_sha256
            or review.cutoff_version_sha256 != article.cutoff_version_sha256
        ):
            raise LeakageReviewError("Leakage-review row does not bind the exact runtime article version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": "article_version_leakage_review_manifest",
            "version": self.version,
            "real_news_bundle_manifest_sha256": self.real_news_bundle_manifest_sha256,
            "calendar_event_registry_sha256": self.calendar_event_registry_sha256,
            "stage_input_registry_canonical_sha256": self.stage_input_registry_canonical_sha256,
            "scanner": self.scanner.to_dict(),
            "runtime_reviews": [review.to_dict() for review in self.runtime_reviews],
            "candidate_reviews": [candidate.to_dict() for candidate in self.candidate_reviews],
        }


def load_article_version_leakage_review_manifest(
    path: Path | str,
    *,
    expected_sha256: str,
    news: SealedNewsRegistry,
    expected_calendar_event_registry_sha256: str,
    expected_stage_input_registry_canonical_sha256: str,
    allowed_root: Path | str,
) -> ArticleVersionLeakageReviewManifest:
    """Load the sole allowed review artifact from the sealed input root."""

    try:
        safe_path = assert_safe_input_file(path, allowed_root=allowed_root)
        payload = json.loads(safe_path.read_text(encoding="utf-8"))
    except (PathSafetyError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LeakageReviewError(f"Article leakage-review manifest cannot be read safely: {exc}") from exc
    return ArticleVersionLeakageReviewManifest.from_mapping(
        payload,
        news=news,
        expected_sha256=expected_sha256,
        expected_calendar_event_registry_sha256=expected_calendar_event_registry_sha256,
        expected_stage_input_registry_canonical_sha256=(
            expected_stage_input_registry_canonical_sha256
        ),
    )


def _decision(value: Any, label: str) -> str:
    decision = _text(value, label).lower()
    if decision not in {"allow", "mask", "reject"}:
        raise LeakageReviewError(f"{label} must be allow/mask/reject")
    return decision


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LeakageReviewError(f"{label} must be a non-empty string")
    return value.strip()


def _sha256(value: Any, label: str) -> str:
    text = _text(value, label).lower()
    if len(text) != _SHA256_LENGTH or any(character not in "0123456789abcdef" for character in text):
        raise LeakageReviewError(f"{label} must be a SHA-256 hex digest")
    return text


def _timestamp(value: Any, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LeakageReviewError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise LeakageReviewError(f"{label} must include an explicit timezone")
    return parsed.isoformat()


__all__ = [
    "ArticleReview",
    "ArticleVersionLeakageReviewManifest",
    "CandidateReview",
    "LeakageReviewError",
    "ReviewScanner",
    "load_article_version_leakage_review_manifest",
]
