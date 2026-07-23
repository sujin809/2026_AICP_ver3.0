"""Isolated, append-only community service for the RN Community AB path.

The legacy community modules intentionally remain untouched.  This module is
the only RN path that turns a PM board into next-AM evidence.  In particular it
does not let a caller supply an arbitrary exposure, profile, Best payload, or
claim identifier: those facts are derived from frozen posts and the
``PaperMemoryStore`` run/condition namespace.

Raw community text is retained in the append-only observation ledger, but it
is returned only by :meth:`interpretation_payloads` for the one permitted
community-interpretation stage.  STB callers receive server-owned claim IDs
and exposure lineage via :meth:`stb_evidence_rows` instead of raw post text.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date as calendar_date
from datetime import datetime, time as clock_time, timedelta
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from twinmarket_kr.db.connection import connect
from twinmarket_kr.rn_ab.memory import (
    RN_AUXILIARY_STAGE_SCHEMA_VERSIONS,
    PaperMemoryStore,
    scientific_sha256,
)
from twinmarket_kr.rn_ab.spec import RN_COMM_OFF, RN_COMM_ON

if TYPE_CHECKING:  # Avoid a runtime dependency on the resolver for a small service.
    from twinmarket_kr.rn_ab.resolver import ResolvedStudyManifest


class CommunityContractError(RuntimeError):
    """Raised when a community payload violates the sealed RN contract."""


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$")
_POST_TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_CLAIM_STANCES = frozenset({"bullish", "bearish", "neutral", "uncertain"})
_REACTIONS = frozenset({"like", "unlike", "none"})
_PRIVATE_PROFILE_KEYS = frozenset(
    {
        "agent_id",
        "author_agent_id",
        "stable_agent_id",
        "portfolio",
        "portfolio_summary",
        "holdings",
        "cash",
        "recent_trade",
        "recent_trades",
        "trade_history",
        "orders",
        "order_history",
        "fill",
        "fills",
        "private_belief",
        "belief_summary",
        "action_reason",
        "view_change",
    }
)
_PROFILE_FIELDS = frozenset(
    {"schema_version", "public_badges", "public_direction", "public_reliability_score"}
)
_COMMUNITY_POLICY_FIELDS = frozenset(
    {
        "best_k",
        "best_selection_policy",
        "permissions_from_cohort_depth_map",
        "depth1_selective_read_cap",
        "depth2_selective_read_cap",
        "best_payload",
        "visibility",
    }
)
_PHASE_REGISTRY_FIELDS = frozenset({"phase_id", "date", "after_event_id"})
_TIMING_POLICY_FIELDS = frozenset(
    {
        "timezone",
        "pm_phase_not_before",
        "pm_phase_not_after",
        "next_am_delivery_not_before",
        "next_am_delivery_not_after",
    }
)
_MAX_BODY_CHARS = 8_000
_MAX_TITLE_CHARS = 300
_MAX_CLAIM_CHARS = 2_000
_POST_CONTENT_VERSION = "rn-community-post-v1"
_POST_TRACE_INPUT_FIELDS = frozenset(
    {
        "phase_id",
        "event_id",
        "turn",
        "date",
        "author_agent_id",
        "eligibility_status",
        "posting_status",
        "post_id",
        "ltb_id",
        "ltb_sha256",
        "view_change_id",
        "view_change_sha256",
        "fill_id",
        "prompt_template_sha256",
        "prompt_values_sha256",
        "logical_call_id",
        "accepted_response_sha256",
        "title_sha256",
        "body_sha256",
    }
)
_EARLIEST_PM_COMMIT = clock_time(15, 30, 0)
_LATEST_NEXT_AM_DELIVERY = clock_time(9, 0, 0)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise CommunityContractError("Community payload is not canonical JSON") from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _content_version_sha256(*, title: str, body: str, body_sha256: str, post_type: str) -> str:
    """Server-owned immutable version identity for one frozen post payload."""

    return _sha256(
        {
            "content_version": _POST_CONTENT_VERSION,
            "title": title,
            "body": body,
            "body_sha256": body_sha256,
            "post_type": post_type,
        }
    )


def _text(value: Any, label: str, *, max_chars: int | None = None, preserve: bool = True) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CommunityContractError(f"{label} must be a non-empty string")
    if max_chars is not None and len(value) > max_chars:
        raise CommunityContractError(f"{label} exceeds its sealed length cap")
    return value if preserve else value.strip()


def _identifier(value: Any, label: str) -> str:
    text = _text(value, label, max_chars=160, preserve=False)
    if not _IDENTIFIER_RE.fullmatch(text):
        raise CommunityContractError(f"{label} contains an unsafe identifier")
    return text


def _sha(value: Any, label: str) -> str:
    text = _text(value, label, max_chars=64, preserve=False).lower()
    if not _SHA256_RE.fullmatch(text):
        raise CommunityContractError(f"{label} must be a SHA-256 hex digest")
    return text


def _strict_int(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CommunityContractError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise CommunityContractError(f"{label} must be >= {minimum}")
    return value


def _exact_mapping(value: Any, *, required: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CommunityContractError(f"{label} must be an object")
    keys = set(value)
    if keys != required:
        raise CommunityContractError(
            f"{label} has invalid fields; missing={sorted(required - keys)} unknown={sorted(keys - required)}"
        )
    return dict(value)


def _parse_date(value: Any, label: str) -> str:
    text = _text(value, label, max_chars=10, preserve=False)
    try:
        return calendar_date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise CommunityContractError(f"{label} must be YYYY-MM-DD") from exc


def _parse_timestamp(value: Any, label: str, *, expected_date: str) -> str:
    text = _text(value, label, max_chars=64, preserve=False)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CommunityContractError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CommunityContractError(f"{label} must carry an explicit timezone")
    if parsed.date().isoformat() != expected_date:
        raise CommunityContractError(f"{label} date differs from its community phase")
    return parsed.isoformat()


def _parse_clock(value: Any, label: str) -> clock_time:
    text = _text(value, label, max_chars=8, preserve=False)
    try:
        return clock_time.fromisoformat(text)
    except ValueError as exc:
        raise CommunityContractError(f"{label} must be HH:MM:SS") from exc


def _timestamp_object(value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:  # Defensive: values originally pass _parse_timestamp.
        raise CommunityContractError(f"{label} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CommunityContractError(f"{label} must carry an explicit timezone")
    if parsed.utcoffset() != timedelta(hours=9):
        raise CommunityContractError(f"{label} must use Asia/Seoul (+09:00) civil time")
    return parsed


@dataclass(frozen=True)
class EventReference:
    """A typed event binding; arbitrary future identifiers are never accepted."""

    event_id: str
    turn: int
    date: str
    subturn: str

    @classmethod
    def from_mapping(cls, value: Any, *, label: str) -> "EventReference":
        data = _exact_mapping(
            value,
            required=frozenset({"event_id", "turn", "date", "subturn"}),
            label=label,
        )
        subturn = _text(data["subturn"], f"{label}.subturn", max_chars=2, preserve=False).lower()
        if subturn not in {"am", "pm"}:
            raise CommunityContractError(f"{label}.subturn must be am or pm")
        return cls(
            event_id=_identifier(data["event_id"], f"{label}.event_id"),
            turn=_strict_int(data["turn"], f"{label}.turn", minimum=1),
            date=_parse_date(data["date"], f"{label}.date"),
            subturn=subturn,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "turn": self.turn,
            "date": self.date,
            "subturn": self.subturn,
        }


@dataclass(frozen=True)
class CommunityPhaseContext:
    """The only PM phase shape accepted by the service."""

    phase_id: str
    after_event: EventReference
    observed_at: str
    next_am_event: EventReference | None

    @classmethod
    def from_mapping(cls, value: Any) -> "CommunityPhaseContext":
        data = _exact_mapping(
            value,
            required=frozenset({"phase_id", "after_event", "observed_at", "next_am_event"}),
            label="community phase",
        )
        after_event = EventReference.from_mapping(data["after_event"], label="community phase.after_event")
        if after_event.subturn != "pm":
            raise CommunityContractError("Community phase must follow a PM decision event")
        next_raw = data["next_am_event"]
        next_event = None if next_raw is None else EventReference.from_mapping(next_raw, label="community phase.next_am_event")
        if next_event is not None:
            if next_event.subturn != "am" or next_event.turn <= after_event.turn:
                raise CommunityContractError("Community Best visibility must target a later AM event")
            if next_event.date <= after_event.date:
                raise CommunityContractError("Community Best visibility must be on a later trading date")
        return cls(
            phase_id=_identifier(data["phase_id"], "community phase.phase_id"),
            after_event=after_event,
            observed_at=_parse_timestamp(
                data["observed_at"], "community phase.observed_at", expected_date=after_event.date
            ),
            next_am_event=next_event,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase_id": self.phase_id,
            "after_event": self.after_event.to_dict(),
            "observed_at": self.observed_at,
            "next_am_event": None if self.next_am_event is None else self.next_am_event.to_dict(),
        }


@dataclass(frozen=True)
class RegisteredCommunityPhase:
    """One resolver-approved PM community phase; no runtime phase is invented."""

    phase_id: str
    date: str
    after_event_id: str

    @classmethod
    def from_mapping(cls, value: Any) -> "RegisteredCommunityPhase":
        data = _exact_mapping(value, required=_PHASE_REGISTRY_FIELDS, label="resolved community phase")
        return cls(
            phase_id=_identifier(data["phase_id"], "resolved community phase.phase_id"),
            date=_parse_date(data["date"], "resolved community phase.date"),
            after_event_id=_identifier(data["after_event_id"], "resolved community phase.after_event_id"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "phase_id": self.phase_id,
            "date": self.date,
            "after_event_id": self.after_event_id,
        }


@dataclass(frozen=True)
class CommunityTimingPolicy:
    """Manifest-pinned civil-time bounds for PM commit and next-AM delivery."""

    timezone: str
    pm_phase_not_before: clock_time
    pm_phase_not_after: clock_time
    next_am_delivery_not_before: clock_time
    next_am_delivery_not_after: clock_time

    @classmethod
    def from_mapping(cls, value: Any) -> "CommunityTimingPolicy":
        data = _exact_mapping(value, required=_TIMING_POLICY_FIELDS, label="community timing policy")
        if data["timezone"] != "Asia/Seoul":
            raise CommunityContractError("community timing policy timezone must be Asia/Seoul")
        pm_start = _parse_clock(data["pm_phase_not_before"], "community timing policy.pm_phase_not_before")
        pm_end = _parse_clock(data["pm_phase_not_after"], "community timing policy.pm_phase_not_after")
        am_start = _parse_clock(
            data["next_am_delivery_not_before"], "community timing policy.next_am_delivery_not_before"
        )
        am_end = _parse_clock(
            data["next_am_delivery_not_after"], "community timing policy.next_am_delivery_not_after"
        )
        if pm_start > pm_end or am_start > am_end:
            raise CommunityContractError("community timing policy windows are inverted")
        # These are non-negotiable study semantics, not a convenience default:
        # a post-PM board cannot begin before the Korean regular-session PM
        # commit, and a next-AM Best payload cannot remain deliverable after
        # the AM decision cutoff.  The resolver/run bundle still pins the
        # exact narrower policy and its hash; this guard rejects a broad or
        # malicious override before it reaches an exposure path.
        if pm_start < _EARLIEST_PM_COMMIT:
            raise CommunityContractError("community PM window may not begin before 15:30 Asia/Seoul")
        if am_end > _LATEST_NEXT_AM_DELIVERY:
            raise CommunityContractError("community next-AM delivery window may not extend after 09:00 Asia/Seoul")
        return cls("Asia/Seoul", pm_start, pm_end, am_start, am_end)

    def to_dict(self) -> dict[str, str]:
        return {
            "timezone": self.timezone,
            "pm_phase_not_before": self.pm_phase_not_before.isoformat(),
            "pm_phase_not_after": self.pm_phase_not_after.isoformat(),
            "next_am_delivery_not_before": self.next_am_delivery_not_before.isoformat(),
            "next_am_delivery_not_after": self.next_am_delivery_not_after.isoformat(),
        }


@dataclass(frozen=True)
class PublicAuthorProfile:
    """A deliberately small, typed public profile with no stable author identity."""

    schema_version: str
    public_badges: tuple[str, ...]
    public_direction: str
    public_reliability_score: int

    @classmethod
    def from_mapping(cls, value: Any, *, label: str) -> "PublicAuthorProfile":
        data = _exact_mapping(value, required=_PROFILE_FIELDS, label=label)
        if _PRIVATE_PROFILE_KEYS & set(data):
            raise CommunityContractError(f"{label} contains a prohibited private field")
        if _text(data["schema_version"], f"{label}.schema_version", preserve=False) != "rn-public-profile-v1":
            raise CommunityContractError(f"{label}.schema_version is not approved")
        badges_raw = data["public_badges"]
        if not isinstance(badges_raw, Sequence) or isinstance(badges_raw, (str, bytes)):
            raise CommunityContractError(f"{label}.public_badges must be an ordered array")
        badges = tuple(sorted({_text(item, f"{label}.public_badges[]", max_chars=80, preserve=False) for item in badges_raw}))
        direction = _text(data["public_direction"], f"{label}.public_direction", max_chars=16, preserve=False).lower()
        if direction not in _CLAIM_STANCES:
            raise CommunityContractError(f"{label}.public_direction is invalid")
        score = _strict_int(data["public_reliability_score"], f"{label}.public_reliability_score", minimum=0)
        if score > 100:
            raise CommunityContractError(f"{label}.public_reliability_score must be <= 100")
        return cls(
            schema_version="rn-public-profile-v1",
            public_badges=badges,
            public_direction=direction,
            public_reliability_score=score,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "public_badges": list(self.public_badges),
            "public_direction": self.public_direction,
            "public_reliability_score": self.public_reliability_score,
        }


@dataclass(frozen=True)
class FrozenCommunityPost:
    post_id: str
    author_agent_id: str
    title: str
    body: str
    body_sha256: str
    content_version: str
    content_version_sha256: str
    post_type: str
    score: int
    like_count: int

    @classmethod
    def from_draft(
        cls,
        value: Any,
        *,
        run_id: str,
        condition_id: str,
        phase_id: str,
        allowed_author_ids: frozenset[str],
        active_author_ids: frozenset[str],
    ) -> "FrozenCommunityPost":
        data = _exact_mapping(
            value,
            required=frozenset({"author_agent_id", "title", "body", "body_sha256", "post_type", "score", "like_count"}),
            label="community post draft",
        )
        author = _identifier(data["author_agent_id"], "community post draft.author_agent_id")
        if author not in allowed_author_ids:
            raise CommunityContractError("Community post author is outside the frozen cohort")
        if author not in active_author_ids:
            raise CommunityContractError("Depth 0 agents may not post to RN community")
        title = _text(data["title"], "community post draft.title", max_chars=_MAX_TITLE_CHARS)
        body = _text(data["body"], "community post draft.body", max_chars=_MAX_BODY_CHARS)
        supplied_hash = _sha(data["body_sha256"], "community post draft.body_sha256")
        actual_hash = _body_sha256(body)
        if supplied_hash != actual_hash:
            raise CommunityContractError("Community post body_sha256 does not match the complete original body")
        post_type = _text(data["post_type"], "community post draft.post_type", max_chars=32, preserve=False)
        if not _POST_TYPE_RE.fullmatch(post_type):
            raise CommunityContractError("community post draft.post_type is invalid")
        score = _strict_int(data["score"], "community post draft.score")
        likes = _strict_int(data["like_count"], "community post draft.like_count", minimum=0)
        content_version_sha = _content_version_sha256(
            title=title,
            body=body,
            body_sha256=actual_hash,
            post_type=post_type,
        )
        post_id = "post:" + _sha256(
            {
                "run_id": run_id,
                "condition_id": condition_id,
                "phase_id": phase_id,
                "author_agent_id": author,
                "title": title,
                "body_sha256": actual_hash,
                "content_version_sha256": content_version_sha,
                "post_type": post_type,
            }
        )[:40]
        return cls(
            post_id=post_id,
            author_agent_id=author,
            title=title,
            body=body,
            body_sha256=actual_hash,
            content_version=_POST_CONTENT_VERSION,
            content_version_sha256=content_version_sha,
            post_type=post_type,
            score=score,
            like_count=likes,
        )

    @classmethod
    def from_ledger(cls, value: Any) -> "FrozenCommunityPost":
        data = _exact_mapping(
            value,
            required=frozenset(
                {
                    "post_id",
                    "author_agent_id",
                    "title",
                    "body",
                    "body_sha256",
                    "content_version",
                    "content_version_sha256",
                    "post_type",
                    "score",
                    "like_count",
                }
            ),
            label="frozen community post",
        )
        post = cls(
            post_id=_identifier(data["post_id"], "frozen community post.post_id"),
            author_agent_id=_identifier(data["author_agent_id"], "frozen community post.author_agent_id"),
            title=_text(data["title"], "frozen community post.title", max_chars=_MAX_TITLE_CHARS),
            body=_text(data["body"], "frozen community post.body", max_chars=_MAX_BODY_CHARS),
            body_sha256=_sha(data["body_sha256"], "frozen community post.body_sha256"),
            content_version=_text(
                data["content_version"], "frozen community post.content_version", max_chars=64, preserve=False
            ),
            content_version_sha256=_sha(
                data["content_version_sha256"], "frozen community post.content_version_sha256"
            ),
            post_type=_text(data["post_type"], "frozen community post.post_type", max_chars=32, preserve=False),
            score=_strict_int(data["score"], "frozen community post.score"),
            like_count=_strict_int(data["like_count"], "frozen community post.like_count", minimum=0),
        )
        if _body_sha256(post.body) != post.body_sha256:
            raise CommunityContractError("Frozen community post body hash no longer matches its full body")
        if post.content_version != _POST_CONTENT_VERSION or post.content_version_sha256 != _content_version_sha256(
            title=post.title,
            body=post.body,
            body_sha256=post.body_sha256,
            post_type=post.post_type,
        ):
            raise CommunityContractError("Frozen community post content version/hash is invalid")
        if not _POST_TYPE_RE.fullmatch(post.post_type):
            raise CommunityContractError("Frozen community post has an invalid post_type")
        return post

    def to_dict(self) -> dict[str, Any]:
        return {
            "post_id": self.post_id,
            "author_agent_id": self.author_agent_id,
            "title": self.title,
            "body": self.body,
            "body_sha256": self.body_sha256,
            "content_version": self.content_version,
            "content_version_sha256": self.content_version_sha256,
            "post_type": self.post_type,
            "score": self.score,
            "like_count": self.like_count,
        }


@dataclass(frozen=True)
class CommunityClaim:
    claim_id: str
    claim_text: str
    stance: str
    source_exposure_ids: tuple[str, ...]
    supporting_quote: str

    def stage_projection(self) -> dict[str, Any]:
        """Projection accepted by ``CurrentEvidencePacket`` without raw post text."""

        return {
            "claim_id": self.claim_id,
            "claim_text": self.claim_text,
            "stance": self.stance,
            "source_exposure_ids": list(self.source_exposure_ids),
        }


@dataclass(frozen=True)
class CommunityCheckpoint:
    phase_id: str
    mode: str
    best_post_ids: tuple[str, ...]
    right_censored: bool


class RNCommunityService:
    """Run-scoped RN community lifecycle with no dependency on legacy community state."""

    def __init__(
        self,
        memory: PaperMemoryStore,
        *,
        cohort_depths: Mapping[str, Any],
        public_profiles: Mapping[str, Mapping[str, Any]],
        community_policy: Mapping[str, Any],
        community_phase_registry: Iterable[Mapping[str, Any]],
        community_timing_policy: Mapping[str, Any],
    ) -> None:
        if not isinstance(memory, PaperMemoryStore):
            raise TypeError("RNCommunityService requires a PaperMemoryStore")
        if memory.condition_id not in {RN_COMM_OFF, RN_COMM_ON}:
            raise CommunityContractError("RN community service accepts only RN_COMM_OFF/RN_COMM_ON")
        if not isinstance(cohort_depths, Mapping) or not cohort_depths:
            raise CommunityContractError("cohort_depths must be a non-empty frozen mapping")
        depths: dict[str, int] = {}
        for raw_agent_id, raw_depth in cohort_depths.items():
            agent_id = _identifier(raw_agent_id, "cohort agent_id")
            if agent_id in depths:
                raise CommunityContractError("cohort_depths contains duplicate agent IDs")
            depth = _strict_int(raw_depth, f"cohort depth {agent_id}", minimum=0)
            if depth not in {0, 1, 2}:
                raise CommunityContractError("RN cohort depth must be 0, 1, or 2")
            depths[agent_id] = depth
        if not isinstance(public_profiles, Mapping):
            raise CommunityContractError("public_profiles must be a mapping")
        unknown_profiles = set(public_profiles) - set(depths)
        if unknown_profiles:
            raise CommunityContractError("public_profiles contains an agent outside the frozen cohort")
        active = {agent_id for agent_id, depth in depths.items() if depth in {1, 2}}
        missing_profiles = active - set(public_profiles)
        if missing_profiles:
            raise CommunityContractError("Every Depth 1/2 agent requires a sanitized public profile")
        profiles = {
            _identifier(agent_id, "public_profiles agent_id"): PublicAuthorProfile.from_mapping(
                value, label=f"public_profiles.{agent_id}"
            )
            for agent_id, value in public_profiles.items()
        }
        policy = _exact_mapping(community_policy, required=_COMMUNITY_POLICY_FIELDS, label="community_policy")
        if _strict_int(policy["best_k"], "community_policy.best_k", minimum=1) > 5:
            raise CommunityContractError("RN Best K may not exceed five")
        if policy["best_selection_policy"] != "top_k_or_fewer_available_no_forced_posting":
            raise CommunityContractError("RN Best selection policy is not approved")
        if policy["permissions_from_cohort_depth_map"] is not True:
            raise CommunityContractError("RN community must derive permissions from cohort depth")
        depth1_cap = _strict_int(policy["depth1_selective_read_cap"], "community_policy.depth1_selective_read_cap", minimum=0)
        depth2_cap = _strict_int(policy["depth2_selective_read_cap"], "community_policy.depth2_selective_read_cap", minimum=0)
        if depth2_cap < depth1_cap:
            raise CommunityContractError("Depth 2 selective-read cap may not be lower than Depth 1")
        if policy["best_payload"] != "title_plus_full_frozen_body":
            raise CommunityContractError("RN Best payload must contain full frozen body")
        if policy["visibility"] != "next_approved_am_decision_event":
            raise CommunityContractError("RN community visibility policy is not approved")
        phases: dict[str, RegisteredCommunityPhase] = {}
        for raw_phase in community_phase_registry:
            phase = RegisteredCommunityPhase.from_mapping(raw_phase)
            if phase.phase_id in phases:
                raise CommunityContractError("resolved community phase registry contains a duplicate phase_id")
            phases[phase.phase_id] = phase
        if not phases:
            raise CommunityContractError("RN community requires a non-empty resolved phase registry")
        timing = CommunityTimingPolicy.from_mapping(community_timing_policy)

        self.memory = memory
        # Mapping insertion order is the resolver's frozen cohort order; do
        # not sort by agent string and accidentally turn it into a new hidden
        # assignment/order policy.
        self.cohort_depths = dict(depths)
        self.cohort_agent_ids = tuple(self.cohort_depths)
        self._cohort_set = frozenset(self.cohort_agent_ids)
        self._active_agents = frozenset(active)
        self.public_profiles = profiles
        self.best_k = int(policy["best_k"])
        self._selective_caps = {1: depth1_cap, 2: depth2_cap}
        self.policy_sha256 = _sha256(policy)
        self._registered_phases = phases
        self.timing_policy = timing
        self.timing_policy_sha256 = _sha256(timing.to_dict())

    @classmethod
    def from_resolved_manifest(
        cls,
        memory: PaperMemoryStore,
        manifest: "ResolvedStudyManifest",
        *,
        public_profiles: Mapping[str, Mapping[str, Any]],
    ) -> "RNCommunityService":
        """Build from the one resolver output rather than duplicated policy inputs."""

        service = cls(
            memory,
            cohort_depths={member.agent_id: member.news_depth for member in manifest.cohort.members},
            public_profiles=public_profiles,
            community_policy=manifest.spec.community_policy,
            community_phase_registry=[
                {
                    "phase_id": phase.phase_id,
                    "date": phase.date,
                    "after_event_id": phase.after_event_id,
                }
                for phase in manifest.calendar.community_phases
            ],
            community_timing_policy=manifest.spec.community_timing_policy,
        )
        if service.timing_policy_sha256 != manifest.spec.community_timing_policy_sha256:
            raise CommunityContractError(
                "Resolved community timing policy hash differs from the StudySpec-pinned policy"
            )
        return service

    @property
    def enabled(self) -> bool:
        return self.memory.condition_id == RN_COMM_ON

    def selective_read_cap(self, agent_id: str) -> int:
        """Return the manifest-bound cap for one frozen cohort member."""

        agent = self._agent(agent_id)
        depth = self.cohort_depths[agent]
        return 0 if depth == 0 else self._selective_caps[depth]

    def preview_candidate_posts(
        self,
        context: CommunityPhaseContext | Mapping[str, Any],
        *,
        post_drafts: Iterable[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], ...]:
        """Return deterministic candidate metadata before the PM batch commits.

        The preview deliberately omits post bodies and author profiles.  It
        exists so a selective-read request can name the server-derived
        ``post_id`` without accepting a model- or client-invented post ID.
        ``run_pm_phase`` freezes the exact same drafts again and rejects a
        mismatch on retry through its append-only observation hash.
        """

        phase = self._phase(context)
        self._assert_phase_matches_schedule(phase)
        if not self.enabled:
            if list(post_drafts):
                raise CommunityContractError("RN_COMM_OFF has no candidate board")
            return ()
        posts = self._freeze_posts(phase, list(post_drafts))
        return tuple(
            {
                "post_id": post.post_id,
                "title": post.title,
                "post_type": post.post_type,
                "score": post.score,
                "like_count": post.like_count,
            }
            for post in posts.values()
        )

    def run_pm_phase(
        self,
        context: CommunityPhaseContext | Mapping[str, Any],
        *,
        post_drafts: Iterable[Mapping[str, Any]] = (),
        selective_reads: Iterable[Mapping[str, Any]] = (),
        private_post_traces: Iterable[Mapping[str, Any]] | None = None,
        private_reader_traces: Iterable[Mapping[str, Any]] | None = None,
    ) -> CommunityCheckpoint:
        """Freeze one PM board, selected reads, and the Best broadcast reservation.

        ``RN_COMM_OFF`` accepts only empty inputs and records an explicit
        no-op checkpoint.  It never creates a community exposure row.
        """

        phase = self._phase(context)
        drafts = list(post_drafts)
        reads = list(selective_reads)
        trace_rows = None if private_post_traces is None else list(private_post_traces)
        reader_trace_rows = (
            None if private_reader_traces is None else list(private_reader_traces)
        )
        self._assert_phase_matches_schedule(phase)
        if not self.enabled:
            if drafts or reads or trace_rows or reader_trace_rows:
                raise CommunityContractError(
                    "RN_COMM_OFF must not receive posts, selective reads, or private traces"
                )
            checkpoint = {
                "schema_version": "rn-community-checkpoint-v1",
                "mode": "off",
                "phase": phase.to_dict(),
                "post_count": 0,
                "selected_exposure_count": 0,
                "best_post_ids": [],
                "best_status": "no_op",
            }
            with connect(self.memory.db_path) as connection:
                self._write_observation(
                    connection,
                    event_id=phase.after_event.event_id,
                    turn=phase.after_event.turn,
                    date=phase.after_event.date,
                    stage=self._checkpoint_stage(phase.phase_id),
                    payload=checkpoint,
                )
                connection.commit()
            return CommunityCheckpoint(phase.phase_id, "off", (), False)

        posts = self._freeze_posts(phase, drafts)
        traces = (
            ()
            if trace_rows is None
            else self._validate_private_post_traces(phase, posts, trace_rows)
        )
        selected = self._validate_selective_reads(phase, posts, reads)
        if reader_trace_rows is None:
            raise CommunityContractError(
                "RN_COMM_ON requires a complete private candidate/reaction trace"
            )
        reader_traces = self._validate_private_reader_traces(
            phase,
            posts,
            selected,
            reader_trace_rows,
        )
        best = tuple(
            sorted(posts.values(), key=lambda post: (-post.score, -post.like_count, post.post_id))[: self.best_k]
        )
        posts_payload = {
            "schema_version": "rn-community-posts-v1",
            "phase": phase.to_dict(),
            "posts": [post.to_dict() for post in posts.values()],
        }
        selected_payload = {
            "schema_version": "rn-community-selected-v1",
            "phase": phase.to_dict(),
            "selections": [
                {"reader_agent_id": reader, "post_id": post_id}
                for reader, post_id in selected
            ],
        }
        schedule_payload = self._schedule_payload(phase, best)
        checkpoint_payload = {
            "schema_version": "rn-community-checkpoint-v1",
            "mode": "on",
            "phase": phase.to_dict(),
            "post_count": len(posts),
            "selected_exposure_count": len(selected),
            "best_post_ids": [post.post_id for post in best],
            "best_status": schedule_payload["status"],
        }
        with connect(self.memory.db_path) as connection:
            for trace in traces:
                self._write_private_post_trace(connection, trace)
            self._write_observation(
                connection,
                event_id=phase.after_event.event_id,
                turn=phase.after_event.turn,
                date=phase.after_event.date,
                stage=self._posts_stage(phase.phase_id),
                payload=posts_payload,
            )
            self._write_observation(
                connection,
                event_id=phase.after_event.event_id,
                turn=phase.after_event.turn,
                date=phase.after_event.date,
                stage=self._selected_stage(phase.phase_id),
                payload=selected_payload,
            )
            self._write_observation(
                connection,
                event_id=phase.after_event.event_id,
                turn=phase.after_event.turn,
                date=phase.after_event.date,
                stage=self._reader_trace_stage(phase.phase_id),
                payload={
                    "schema_version": "rn-community-reader-trace-v1",
                    "phase": phase.to_dict(),
                    "visible_from_event_id": (
                        None
                        if phase.next_am_event is None
                        else phase.next_am_event.event_id
                    ),
                    "readers": list(reader_traces),
                },
            )
            for reader, post_id in selected:
                post = posts[post_id]
                self._insert_exposure(
                    connection,
                    agent_id=reader,
                    event_id=phase.after_event.event_id,
                    channel="community_selected",
                    root_id=post.post_id,
                    body_sha256=post.body_sha256,
                    delivered_at=phase.observed_at,
                    status="delivered",
                    metadata=self._selected_metadata(phase, post),
                )
            self._write_observation(
                connection,
                event_id=phase.after_event.event_id,
                turn=phase.after_event.turn,
                date=phase.after_event.date,
                stage=self._schedule_stage(phase.phase_id),
                payload=schedule_payload,
            )
            if phase.next_am_event is None:
                for recipient in self.cohort_agent_ids:
                    for rank, post in enumerate(best, start=1):
                        self._insert_exposure(
                            connection,
                            agent_id=recipient,
                            event_id=phase.after_event.event_id,
                            channel="community_best",
                            root_id=post.post_id,
                            body_sha256=post.body_sha256,
                            delivered_at=None,
                            status="right_censored",
                            metadata=self._right_censored_metadata(phase, post, rank),
                        )
            self._write_observation(
                connection,
                event_id=phase.after_event.event_id,
                turn=phase.after_event.turn,
                date=phase.after_event.date,
                stage=self._checkpoint_stage(phase.phase_id),
                payload=checkpoint_payload,
            )
            connection.commit()
        return CommunityCheckpoint(
            phase_id=phase.phase_id,
            mode="on",
            best_post_ids=tuple(post.post_id for post in best),
            right_censored=phase.next_am_event is None and bool(best),
        )

    def _validate_private_post_traces(
        self,
        phase: CommunityPhaseContext,
        posts: Mapping[str, FrozenCommunityPost],
        traces: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], ...]:
        """Validate one private posting-decision trace per eligible author.

        These rows are never projected into a public post, reader payload, or
        STB evidence packet.  The service accepts them only at the PM board
        commit boundary so post content and its private provenance are written
        in one SQLite transaction.
        """

        if len(traces) != len(self._active_agents):
            raise CommunityContractError(
                "Private post traces must cover every eligible Depth 1/2 author"
            )
        posts_by_author = {post.author_agent_id: post for post in posts.values()}
        if len(posts_by_author) != len(posts):
            raise CommunityContractError("A community phase may contain at most one post per author")
        normalized: list[dict[str, Any]] = []
        seen_authors: set[str] = set()
        schema_version = RN_AUXILIARY_STAGE_SCHEMA_VERSIONS["community_posting"]
        for raw in traces:
            data = _exact_mapping(
                raw,
                required=_POST_TRACE_INPUT_FIELDS,
                label="private community post trace",
            )
            author = self._agent(data["author_agent_id"])
            if author not in self._active_agents or author in seen_authors:
                raise CommunityContractError(
                    "Private post traces contain an ineligible or duplicate author"
                )
            seen_authors.add(author)
            if (
                _identifier(data["phase_id"], "private post trace.phase_id") != phase.phase_id
                or _identifier(data["event_id"], "private post trace.event_id")
                != phase.after_event.event_id
                or _strict_int(data["turn"], "private post trace.turn", minimum=1)
                != phase.after_event.turn
                or _parse_date(data["date"], "private post trace.date")
                != phase.after_event.date
            ):
                raise CommunityContractError("Private post trace phase/event identity differs")
            if data["eligibility_status"] != "eligible":
                raise CommunityContractError("Private post trace eligibility must be eligible")
            status = _text(
                data["posting_status"],
                "private post trace.posting_status",
                max_chars=8,
                preserve=False,
            )
            if status not in {"posted", "skipped"}:
                raise CommunityContractError("Private post trace posting_status is invalid")

            ltb_id = _identifier(data["ltb_id"], "private post trace.ltb_id")
            fill_id = _identifier(data["fill_id"], "private post trace.fill_id")
            ltb_sha = _sha(data["ltb_sha256"], "private post trace.ltb_sha256")
            view_change_sha = _sha(
                data["view_change_sha256"],
                "private post trace.view_change_sha256",
            )
            expected_view_change_id = "view-change:" + view_change_sha[:40]
            if (
                _identifier(
                    data["view_change_id"], "private post trace.view_change_id"
                )
                != expected_view_change_id
            ):
                raise CommunityContractError("Private post trace view-change identity is invalid")

            ltb = self.memory.previous_ltb(
                agent_id=author,
                decision_turn=phase.after_event.turn + 1,
            )
            if (
                str(ltb["ltb_id"]) != ltb_id
                or str(ltb["event_id"]) != phase.after_event.event_id
                or _sha(ltb["scientific_sha256"], "stored posting LTB hash") != ltb_sha
            ):
                raise CommunityContractError(
                    "Private post trace does not reference the current post-fill LTB"
                )
            human_log = self.memory.human_log_for_ltb(ltb_id=ltb_id)
            if scientific_sha256(human_log["view_change"]) != view_change_sha:
                raise CommunityContractError(
                    "Private post trace view-change hash differs from the deterministic LTB log"
                )

            logical_call_id = _text(
                data["logical_call_id"],
                "private post trace.logical_call_id",
                max_chars=1_000,
                preserve=False,
            )
            expected_logical_id = "|".join(
                (
                    self.memory.run_id,
                    self.memory.condition_id,
                    author,
                    phase.after_event.event_id,
                    "community_posting",
                    schema_version,
                )
            )
            if logical_call_id != expected_logical_id:
                raise CommunityContractError(
                    "Private post trace logical call identity is not canonical"
                )

            post = posts_by_author.get(author)
            if status == "posted":
                if post is None:
                    raise CommunityContractError(
                        "Posted private trace has no matching frozen public post"
                    )
                post_id = _identifier(data["post_id"], "private post trace.post_id")
                title_sha = _sha(
                    data["title_sha256"], "private post trace.title_sha256"
                )
                body_sha = _sha(
                    data["body_sha256"], "private post trace.body_sha256"
                )
                if (
                    post_id != post.post_id
                    or title_sha
                    != hashlib.sha256(post.title.encode("utf-8")).hexdigest()
                    or body_sha != post.body_sha256
                ):
                    raise CommunityContractError(
                        "Private post trace does not match its frozen public post"
                    )
            else:
                if post is not None:
                    raise CommunityContractError(
                        "Skipped private trace unexpectedly has a public post"
                    )
                if any(
                    data[field] is not None
                    for field in ("post_id", "title_sha256", "body_sha256")
                ):
                    raise CommunityContractError(
                        "Skipped private post trace must not invent post content identity"
                    )
                post_id = title_sha = body_sha = None

            with connect(self.memory.db_path, read_only=True) as connection:
                fill = connection.execute(
                    """
                    SELECT agent_id, event_id, turn
                    FROM paper_fill_ledger
                    WHERE fill_id = ? AND run_id = ? AND condition_id = ?
                    """,
                    (
                        fill_id,
                        self.memory.run_id,
                        self.memory.condition_id,
                    ),
                ).fetchone()
            if fill is None or (
                str(fill["agent_id"]) != author
                or str(fill["event_id"]) != phase.after_event.event_id
                or int(fill["turn"]) != phase.after_event.turn
            ):
                raise CommunityContractError(
                    "Private post trace does not reference the committed current PM fill"
                )

            normalized.append(
                {
                    "run_id": self.memory.run_id,
                    "condition_id": self.memory.condition_id,
                    "manifest_sha256": self.memory.manifest_sha256,
                    "phase_id": phase.phase_id,
                    "event_id": phase.after_event.event_id,
                    "turn": phase.after_event.turn,
                    "date": phase.after_event.date,
                    "author_agent_id": author,
                    "eligibility_status": "eligible",
                    "posting_status": status,
                    "post_id": post_id,
                    "ltb_id": ltb_id,
                    "ltb_sha256": ltb_sha,
                    "view_change_id": expected_view_change_id,
                    "view_change_sha256": view_change_sha,
                    "fill_id": fill_id,
                    "prompt_template_sha256": _sha(
                        data["prompt_template_sha256"],
                        "private post trace.prompt_template_sha256",
                    ),
                    "prompt_values_sha256": _sha(
                        data["prompt_values_sha256"],
                        "private post trace.prompt_values_sha256",
                    ),
                    "logical_call_id": logical_call_id,
                    "accepted_response_sha256": _sha(
                        data["accepted_response_sha256"],
                        "private post trace.accepted_response_sha256",
                    ),
                    "title_sha256": title_sha,
                    "body_sha256": body_sha,
                }
            )
        if seen_authors != self._active_agents:
            raise CommunityContractError("Private post traces omit an eligible author")
        return tuple(sorted(normalized, key=lambda row: row["author_agent_id"]))

    @staticmethod
    def _write_private_post_trace(
        connection: sqlite3.Connection,
        trace: Mapping[str, Any],
    ) -> None:
        identity = {
            key: trace[key]
            for key in ("run_id", "condition_id", "phase_id", "author_agent_id")
        }
        trace_id = "post-trace:" + _sha256(identity)[:40]
        values = {**dict(trace), "trace_id": trace_id}
        trace_sha = _sha256(values)
        stored_values = {**values, "trace_sha256": trace_sha}
        columns = tuple(stored_values)
        placeholders = ",".join("?" for _ in columns)
        try:
            connection.execute(
                f"INSERT INTO community_post_trace ({','.join(columns)}) "
                f"VALUES ({placeholders}) ON CONFLICT(trace_id) DO NOTHING",
                tuple(stored_values[column] for column in columns),
            )
        except sqlite3.IntegrityError as exc:
            raise CommunityContractError(
                "Private post trace conflicts with committed provenance"
            ) from exc
        row = connection.execute(
            "SELECT * FROM community_post_trace WHERE trace_id = ?",
            (trace_id,),
        ).fetchone()
        if row is None or any(row[column] != value for column, value in stored_values.items()):
            raise CommunityContractError(
                "Private post trace retry differs from committed provenance"
            )

    def deliver_scheduled_best(
        self,
        context: CommunityPhaseContext | Mapping[str, Any],
        *,
        delivered_at: str,
    ) -> dict[str, tuple[dict[str, Any], ...]]:
        """Deliver the sealed PM Best payload to *every* cohort agent next AM.

        The caller cannot select recipients or alter title/body text.  A
        missing Best is represented by an explicit empty delivery observation;
        a final-day right-censored reservation cannot be delivered.
        """

        phase = self._phase(context)
        self._assert_phase_matches_schedule(phase)
        if not self.enabled:
            raise CommunityContractError("RN_COMM_OFF has no scheduled Best delivery")
        if phase.next_am_event is None:
            raise CommunityContractError("A right-censored final-PM Best may not be delivered")
        delivered = _parse_timestamp(
            delivered_at,
            "community Best delivered_at",
            expected_date=phase.next_am_event.date,
        )
        self._assert_delivery_timestamp(phase=phase, delivered_at=delivered)
        with connect(self.memory.db_path) as connection:
            schedule = self._read_observation(
                connection,
                event_id=phase.after_event.event_id,
                stage=self._schedule_stage(phase.phase_id),
            )
            self._assert_schedule_payload(schedule, phase)
            if schedule["status"] == "right_censored":
                raise CommunityContractError("A right-censored Best schedule may not be delivered")
            best = tuple(FrozenCommunityPost.from_ledger(item) for item in schedule["best_posts"])
            if len({post.post_id for post in best}) != len(best):
                raise CommunityContractError("Stored Best schedule repeats a post ID")
            expected_order = tuple(sorted(best, key=lambda post: (-post.score, -post.like_count, post.post_id)))
            if best != expected_order:
                raise CommunityContractError("Stored Best schedule violates deterministic rank ordering")
            if schedule["status"] == "empty" and best:
                raise CommunityContractError("Empty Best schedule contains posts")
            if schedule["status"] == "scheduled" and not best:
                raise CommunityContractError("Scheduled Best has no posts")
            output: dict[str, tuple[dict[str, Any], ...]] = {}
            delivered_rows: list[dict[str, Any]] = []
            for recipient in self.cohort_agent_ids:
                recipient_rows: list[dict[str, Any]] = []
                for rank, post in enumerate(best, start=1):
                    exposure_id = self._insert_exposure(
                        connection,
                        agent_id=recipient,
                        event_id=phase.next_am_event.event_id,
                        channel="community_best",
                        root_id=post.post_id,
                        body_sha256=post.body_sha256,
                        delivered_at=delivered,
                        status="delivered",
                        metadata=self._best_metadata(phase, post, rank),
                    )
                    entry = self._payload_projection(
                        post,
                        exposure_ids=(exposure_id,),
                        channel="best_only_body",
                        rank=rank,
                        profile=None,
                        reader_reaction=None,
                    )
                    recipient_rows.append(entry)
                    delivered_rows.append(
                        {
                            "agent_id": recipient,
                            "exposure_id": exposure_id,
                            "post_id": post.post_id,
                            "body_sha256": post.body_sha256,
                            "rank": rank,
                        }
                    )
                output[recipient] = tuple(recipient_rows)
            payload = {
                "schema_version": "rn-community-best-delivery-v1",
                "phase": phase.to_dict(),
                "delivered_at": delivered,
                "deliveries": delivered_rows,
            }
            self._write_observation(
                connection,
                event_id=phase.next_am_event.event_id,
                turn=phase.next_am_event.turn,
                date=phase.next_am_event.date,
                stage=self._delivery_stage(phase.phase_id),
                payload=payload,
            )
            connection.commit()
        return output

    def interpretation_payloads(self, *, agent_id: str, event_id: str) -> tuple[dict[str, Any], ...]:
        """Return exact full-body payloads visible for one next-AM interpretation.

        A selected-read/Best overlap is serialized once for the LLM but keeps
        both reader-owned exposure IDs so claims and audits retain complete
        lineage.  No author agent ID is exposed to the reader.
        """

        agent = self._agent(agent_id)
        event = _identifier(event_id, "interpretation event_id")
        if not self.enabled:
            return ()
        self._assert_am_interpretation_event(event)
        with connect(self.memory.db_path, read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_exposures
                WHERE run_id = ? AND condition_id = ? AND agent_id = ? AND status = 'delivered'
                  AND (
                    (channel = 'community_best' AND event_id = ?)
                    OR (channel = 'community_selected' AND json_extract(metadata_json, '$.visible_from_event_id') = ?)
                  )
                ORDER BY channel, root_id, exposure_id
                """,
                (self.memory.run_id, self.memory.condition_id, agent, event, event),
            ).fetchall()
        grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            if str(row["channel"]) not in {"community_best", "community_selected"}:
                continue
            metadata = self._load_exposure_metadata(row)
            body = _text(metadata["full_body"], "stored community exposure full_body", max_chars=_MAX_BODY_CHARS)
            body_hash = _sha(metadata["body_sha256"], "stored community exposure body_sha256")
            if _body_sha256(body) != body_hash or body_hash != str(row["body_sha256"]):
                raise CommunityContractError("Stored community exposure full body/hash integrity failure")
            grouped.setdefault((str(row["root_id"]), body_hash), []).append(row)

        candidate_payloads = self._candidate_payloads(
            agent_id=agent,
            event_id=event,
        )
        reaction_by_post = {
            str(item["post_id"]): str(item["reader_reaction"])
            for item in candidate_payloads
            if item.get("reader_reaction") is not None
        }
        payloads: list[dict[str, Any]] = []
        for (_, _), group in sorted(grouped.items(), key=lambda item: item[0]):
            metadata_by_channel = {str(row["channel"]): self._load_exposure_metadata(row) for row in group}
            canonical_metadata = next(iter(metadata_by_channel.values()))
            post = FrozenCommunityPost.from_ledger(
                {
                    "post_id": canonical_metadata["post_id"],
                    "author_agent_id": canonical_metadata["ledger_author_agent_id"],
                    "title": canonical_metadata["title"],
                    "body": canonical_metadata["full_body"],
                    "body_sha256": canonical_metadata["body_sha256"],
                    "content_version": canonical_metadata["content_version"],
                    "content_version_sha256": canonical_metadata["content_version_sha256"],
                    "post_type": canonical_metadata["post_type"],
                    "score": canonical_metadata["score"],
                    "like_count": canonical_metadata["like_count"],
                }
            )
            exposure_ids = tuple(sorted(str(row["exposure_id"]) for row in group))
            selected_metadata = metadata_by_channel.get("community_selected")
            best_metadata = metadata_by_channel.get("community_best")
            if selected_metadata and best_metadata:
                channel = "selected_and_best_overlap"
            elif selected_metadata:
                channel = "selected_body"
            else:
                channel = "best_only_body"
            profile = None if selected_metadata is None else selected_metadata["public_author_profile"]
            rank = None if best_metadata is None else best_metadata["rank"]
            payloads.append(
                self._payload_projection(
                    post,
                    exposure_ids=exposure_ids,
                    channel=channel,
                    rank=rank,
                    profile=profile,
                    reader_reaction=(
                        None
                        if selected_metadata is None
                        else reaction_by_post.get(post.post_id)
                    ),
                )
            )
        return tuple([*candidate_payloads, *payloads])

    def record_interpretation_claims(
        self,
        *,
        agent_id: str,
        event_id: str,
        claims: Iterable[Mapping[str, Any]],
    ) -> tuple[CommunityClaim, ...]:
        """Validate model claims against exact, delivered reader-owned exposures."""

        agent = self._agent(agent_id)
        event = _identifier(event_id, "community claim event_id")
        claim_rows = list(claims)
        if not self.enabled:
            if claim_rows:
                raise CommunityContractError("RN_COMM_OFF may not emit community interpretation claims")
            return ()
        self._assert_am_interpretation_event(event)
        available = self._visible_claim_sources(agent_id=agent, event_id=event)
        if not available:
            if claim_rows:
                raise CommunityContractError("Community interpretation has no delivered exposure for this agent/event")
            # A no-exposure agent must not acquire a synthetic claim ledger
            # row; this makes the no-call/no-exposure invariant auditable.
            return ()
        parsed: list[CommunityClaim] = []
        seen_ids: set[str] = set()
        for raw in claim_rows:
            data = _exact_mapping(
                raw,
                required=frozenset(
                    {
                        "claim_text",
                        "stance",
                        "source_exposure_ids",
                        "supporting_quote",
                    }
                ),
                label="community interpretation claim",
            )
            claim_text = _text(data["claim_text"], "community interpretation claim.claim_text", max_chars=_MAX_CLAIM_CHARS)
            stance = _text(data["stance"], "community interpretation claim.stance", max_chars=16, preserve=False).lower()
            if stance not in _CLAIM_STANCES:
                raise CommunityContractError("community interpretation claim.stance is invalid")
            sources_raw = data["source_exposure_ids"]
            if not isinstance(sources_raw, Sequence) or isinstance(sources_raw, (str, bytes)) or not sources_raw:
                raise CommunityContractError("community interpretation claim requires non-empty source exposure IDs")
            sources = tuple(sorted(_identifier(item, "community interpretation source exposure ID") for item in sources_raw))
            if len(sources) != len(set(sources)):
                raise CommunityContractError("community interpretation claim may not duplicate an exposure ID")
            unknown = set(sources) - set(available)
            if unknown:
                raise CommunityContractError("community interpretation claim cites an undelivered or foreign exposure")
            supporting_quote = _text(
                data["supporting_quote"],
                "community interpretation claim.supporting_quote",
                max_chars=1_000,
            )
            if not any(
                any(
                    supporting_quote in source_text
                    for source_text in available[source]["allowed_texts"]
                )
                for source in sources
            ):
                raise CommunityContractError(
                    "Community interpretation supporting_quote is not an exact "
                    "substring of any cited source at its delivered content level"
                )
            claim_id = "community_claim:" + _sha256(
                {
                    "run_id": self.memory.run_id,
                    "condition_id": self.memory.condition_id,
                    "agent_id": agent,
                    "event_id": event,
                    "claim_text": claim_text,
                    "stance": stance,
                    "source_exposure_ids": list(sources),
                    "supporting_quote": supporting_quote,
                }
            )[:40]
            if claim_id in seen_ids:
                raise CommunityContractError("Duplicate community claim content/source tuple")
            seen_ids.add(claim_id)
            parsed.append(
                CommunityClaim(
                    claim_id,
                    claim_text,
                    stance,
                    sources,
                    supporting_quote,
                )
            )
        payload = {
            "schema_version": "rn-community-claims-v1",
            "agent_id": agent,
            "event_id": event,
            "claims": [
                {
                    **claim.stage_projection(),
                    "supporting_quote": claim.supporting_quote,
                    "source_roots": sorted(
                        {
                            str(available[source]["root_id"])
                            for source in claim.source_exposure_ids
                        }
                    ),
                }
                for claim in parsed
            ],
        }
        with connect(self.memory.db_path) as connection:
            event_row = self._event_row_for_claim(connection, event)
            self._write_observation(
                connection,
                event_id=event,
                turn=event_row["turn"],
                date=event_row["date"],
                stage=self._claims_stage(agent),
                payload=payload,
            )
            connection.commit()
        return tuple(parsed)

    def validate_claims_for_agent(
        self,
        *,
        agent_id: str,
        event_id: str,
        claims: Iterable[Mapping[str, Any]],
    ) -> tuple[CommunityClaim, ...]:
        """Validate either interpretation drafts or durable STB projections.

        The interpretation boundary supplies three-field drafts and receives
        server-owned claim IDs.  The later STB boundary supplies the resulting
        four-field projections, including ``claim_id``; those must be reloaded
        and compared with the append-only ledger rather than created again.
        The exact schemas make the two operations unambiguous and preserve the
        historical public method used by the stage packet verifier.
        """

        agent = self._agent(agent_id)
        event = _identifier(event_id, "community claim validation event_id")
        rows = list(claims)
        if not rows:
            if not self.enabled:
                return ()
            schedule = self.memory.event_schedule
            if schedule is None:  # pragma: no cover - constructor enforces this.
                raise CommunityContractError("RN community requires EventSchedule")
            if str(schedule.event(event)["subturn"]) != "am":
                return ()
            recorded = self.recorded_claims_for_agent(agent_id=agent, event_id=event)
            if recorded:
                raise CommunityContractError("STB omitted committed community claims for this agent/event")
            return ()

        field_sets = [set(row) if isinstance(row, Mapping) else set() for row in rows]
        interpretation_fields = {
            "claim_text",
            "stance",
            "source_exposure_ids",
            "supporting_quote",
        }
        stage_fields = {"claim_id", "claim_text", "stance", "source_exposure_ids"}
        if all(fields == interpretation_fields for fields in field_sets):
            return self.record_interpretation_claims(
                agent_id=agent,
                event_id=event,
                claims=rows,
            )
        if all(fields == stage_fields for fields in field_sets):
            if not self.enabled:
                raise CommunityContractError("RN_COMM_OFF may not validate community claims")
            recorded = self.recorded_claims_for_agent(agent_id=agent, event_id=event)
            projections = tuple(claim.stage_projection() for claim in recorded)
            if tuple(dict(row) for row in rows) != projections:
                raise CommunityContractError("STB community claims differ from their append-only ledger")
            return recorded
        raise CommunityContractError("Community claims mix or violate interpretation/STB schemas")

    def recorded_claims_for_agent(
        self,
        *,
        agent_id: str,
        event_id: str,
    ) -> tuple[CommunityClaim, ...]:
        """Reload durable interpretation claims without re-running a model.

        The RN phase coordinator can skip an already committed community
        preparation phase after restart.  Downstream STB construction must
        therefore recover claims from the append-only observation ledger,
        rather than depend on an in-memory cache owned by the previous
        process.  A missing claim observation is accepted only when this
        reader/event truly has no delivered community exposure.
        """

        agent = self._agent(agent_id)
        event = _identifier(event_id, "recorded community claim event_id")
        if not self.enabled:
            return ()
        self._assert_am_interpretation_event(event)
        stage = self._claims_stage(agent)
        with connect(self.memory.db_path, read_only=True) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM observation_events
                WHERE run_id = ? AND condition_id = ? AND event_id = ? AND stage = ?
                """,
                (self.memory.run_id, self.memory.condition_id, event, stage),
            ).fetchone()
            if row is None:
                if self._visible_claim_sources(agent_id=agent, event_id=event):
                    raise CommunityContractError(
                        "Delivered community exposure has no committed interpretation claim observation"
                    )
                return ()
            payload = self._read_observation(connection, event_id=event, stage=stage)
            if (
                set(payload) != {"schema_version", "agent_id", "event_id", "claims"}
                or payload.get("schema_version") != "rn-community-claims-v1"
                or payload.get("agent_id") != agent
                or payload.get("event_id") != event
                or not isinstance(payload.get("claims"), list)
            ):
                raise CommunityContractError("Stored community claim observation has an invalid schema")
            visible = self._visible_claim_sources(agent_id=agent, event_id=event)
            claims: list[CommunityClaim] = []
            for index, raw in enumerate(payload["claims"]):
                data = _exact_mapping(
                    raw,
                    required=frozenset(
                        {
                            "claim_id",
                            "claim_text",
                            "stance",
                            "source_exposure_ids",
                            "supporting_quote",
                            "source_roots",
                        }
                    ),
                    label=f"stored community claim[{index}]",
                )
                sources_raw = data["source_exposure_ids"]
                roots_raw = data["source_roots"]
                if (
                    not isinstance(sources_raw, list)
                    or not sources_raw
                    or not isinstance(roots_raw, list)
                    or not roots_raw
                ):
                    raise CommunityContractError("Stored community claim lineage arrays are malformed")
                sources = tuple(
                    _identifier(item, f"stored community claim[{index}].source_exposure_ids[]")
                    for item in sources_raw
                )
                if tuple(sorted(sources)) != sources or len(sources) != len(set(sources)):
                    raise CommunityContractError("Stored community claim source IDs are not sorted and unique")
                roots = tuple(
                    _identifier(item, f"stored community claim[{index}].source_roots[]")
                    for item in roots_raw
                )
                expected_roots = tuple(
                    sorted({str(visible[source]["root_id"]) for source in sources if source in visible})
                )
                if roots != expected_roots or len(expected_roots) == 0:
                    raise CommunityContractError("Stored community claim root lineage is inconsistent")
                claim = CommunityClaim(
                    claim_id=_identifier(data["claim_id"], f"stored community claim[{index}].claim_id"),
                    claim_text=_text(
                        data["claim_text"],
                        f"stored community claim[{index}].claim_text",
                        max_chars=_MAX_CLAIM_CHARS,
                    ),
                    stance=_text(
                        data["stance"],
                        f"stored community claim[{index}].stance",
                        max_chars=16,
                        preserve=False,
                    ).lower(),
                    source_exposure_ids=sources,
                    supporting_quote=_text(
                        data["supporting_quote"],
                        f"stored community claim[{index}].supporting_quote",
                        max_chars=1_000,
                    ),
                )
                if claim.stance not in _CLAIM_STANCES:
                    raise CommunityContractError("Stored community claim stance is invalid")
                if not any(
                    any(
                        claim.supporting_quote in source_text
                        for source_text in visible[source]["allowed_texts"]
                    )
                    for source in claim.source_exposure_ids
                ):
                    raise CommunityContractError(
                        "Stored community claim quote is unsupported at its source level"
                    )
                self._assert_claim_exists(
                    claim,
                    connection=connection,
                    agent_id=agent,
                    event_id=event,
                )
                claims.append(claim)
            if len({claim.claim_id for claim in claims}) != len(claims):
                raise CommunityContractError("Stored community claim observation repeats a claim ID")
            return tuple(claims)

    def stb_evidence_rows(
        self,
        *,
        agent_id: str,
        event_id: str,
        claims: Iterable[CommunityClaim],
    ) -> tuple[dict[str, Any], ...]:
        """Return only claim lineage owned by this exact agent/event.

        The explicit owner/event binding is important: a structurally valid
        claim from another reader must never be usable as STB evidence simply
        because a caller copied its ID and source-exposure list.
        """

        agent = self._agent(agent_id)
        event = _identifier(event_id, "STB evidence event_id")
        self._assert_am_interpretation_event(event)
        rows: list[dict[str, Any]] = []
        with connect(self.memory.db_path, read_only=True) as connection:
            for claim in claims:
                if not isinstance(claim, CommunityClaim):
                    raise TypeError("stb_evidence_rows accepts CommunityClaim instances only")
                self._assert_claim_exists(
                    claim,
                    connection=connection,
                    agent_id=agent,
                    event_id=event,
                )
                rows.append(
                    {
                        "kind": "community_claim",
                        "claim_id": claim.claim_id,
                        "source_exposure_ids": list(claim.source_exposure_ids),
                    }
                )
        return tuple(rows)

    def attach_claims_to_stb(
        self,
        *,
        agent_id: str,
        event_id: str,
        stb_id: str,
        claims: Iterable[CommunityClaim],
        dimensions_by_claim: Mapping[str, Sequence[str]] | None = None,
    ) -> tuple[str, ...]:
        """Write append-only claim → STB evidence edges after the STB is committed."""

        agent = self._agent(agent_id)
        event = _identifier(event_id, "STB evidence event_id")
        target_stb_id = _identifier(stb_id, "STB evidence stb_id")
        claim_list = tuple(claims)
        for claim in claim_list:
            if not isinstance(claim, CommunityClaim):
                raise TypeError("attach_claims_to_stb accepts CommunityClaim instances only")
        dimensions = self._validate_claim_dimensions(claim_list, dimensions_by_claim)
        edge_ids: list[str] = []
        with connect(self.memory.db_path) as connection:
            target = connection.execute(
                """
                SELECT 1 FROM short_term_belief_history
                WHERE stb_id = ? AND run_id = ? AND condition_id = ? AND agent_id = ? AND event_id = ?
                """,
                (target_stb_id, self.memory.run_id, self.memory.condition_id, agent, event),
            ).fetchone()
            if target is None:
                raise CommunityContractError("STB evidence edge target is outside the run/condition/agent/event scope")
            for claim in claim_list:
                self._assert_claim_exists(claim, connection=connection, agent_id=agent, event_id=event)
                for dimension in dimensions[claim.claim_id]:
                    edge_id = "edge:" + _sha256(
                        {
                            "run_id": self.memory.run_id,
                            "condition_id": self.memory.condition_id,
                            "agent_id": agent,
                            "event_id": event,
                            "target_kind": "stb",
                            "target_id": target_stb_id,
                            "source_kind": "community_claim",
                            "source_id": claim.claim_id,
                            "dimension": dimension,
                        }
                    )[:40]
                    self._insert_edge(
                        connection,
                        edge_id=edge_id,
                        agent_id=agent,
                        event_id=event,
                        target_id=target_stb_id,
                        source_id=claim.claim_id,
                        dimension=dimension,
                    )
                    edge_ids.append(edge_id)
            connection.commit()
        return tuple(edge_ids)

    # ----- Strict source validation -------------------------------------------------

    def _phase(self, value: CommunityPhaseContext | Mapping[str, Any]) -> CommunityPhaseContext:
        phase = value if isinstance(value, CommunityPhaseContext) else CommunityPhaseContext.from_mapping(value)
        if not isinstance(phase, CommunityPhaseContext):
            raise TypeError("context must be a CommunityPhaseContext or exact mapping")
        return phase

    def _assert_phase_matches_schedule(self, phase: CommunityPhaseContext) -> None:
        schedule = self.memory.event_schedule
        if schedule is None:
            raise CommunityContractError("RN community requires the same frozen EventSchedule as paper memory")
        registered = self._registered_phases.get(phase.phase_id)
        if registered is None:
            raise CommunityContractError("Community phase_id is absent from the resolved phase registry")
        if (
            registered.after_event_id != phase.after_event.event_id
            or registered.date != phase.after_event.date
        ):
            raise CommunityContractError("Community phase source differs from its resolved phase registry key")
        source = schedule.event(phase.after_event.event_id)
        if (
            int(source["turn"]) != phase.after_event.turn
            or str(source["date"]) != phase.after_event.date
            or str(source["subturn"]) != phase.after_event.subturn
        ):
            raise CommunityContractError("Community phase source event differs from frozen EventSchedule")
        observed = _timestamp_object(phase.observed_at, label="community phase.observed_at")
        if not self.timing_policy.pm_phase_not_before <= observed.time() <= self.timing_policy.pm_phase_not_after:
            raise CommunityContractError("Community phase observed_at is outside its manifest-pinned PM commit window")
        if phase.next_am_event is None:
            expected = self._next_approved_am_event(phase.after_event.turn)
            if expected is not None:
                raise CommunityContractError("Community phase falsely right-censors an available next AM event")
            return
        expected = self._next_approved_am_event(phase.after_event.turn)
        if expected is None:
            raise CommunityContractError("Community phase invents a next AM event after the frozen schedule ends")
        target = schedule.event(phase.next_am_event.event_id)
        if (
            int(target["turn"]) != phase.next_am_event.turn
            or str(target["date"]) != phase.next_am_event.date
            or str(target["subturn"]) != phase.next_am_event.subturn
        ):
            raise CommunityContractError("Community Best target differs from frozen EventSchedule")
        if (
            str(expected["event_id"]) != phase.next_am_event.event_id
            or int(expected["turn"]) != phase.next_am_event.turn
            or str(expected["date"]) != phase.next_am_event.date
        ):
            raise CommunityContractError("Community phase skips the next approved AM event")

    def _assert_delivery_timestamp(self, *, phase: CommunityPhaseContext, delivered_at: str) -> None:
        if phase.next_am_event is None:
            raise CommunityContractError("A right-censored final-PM Best may not be delivered")
        delivered = _timestamp_object(delivered_at, label="community Best delivered_at")
        if not (
            self.timing_policy.next_am_delivery_not_before
            <= delivered.time()
            <= self.timing_policy.next_am_delivery_not_after
        ):
            raise CommunityContractError("Community Best delivery is outside its manifest-pinned next-AM window")

    def _next_approved_am_event(self, after_turn: int) -> dict[str, Any] | None:
        schedule = self.memory.event_schedule
        if schedule is None:  # pragma: no cover - caller already checks this invariant.
            raise CommunityContractError("RN community requires EventSchedule")
        for event in schedule.events:
            if int(event["turn"]) > after_turn and str(event["subturn"]) == "am":
                return dict(event)
        return None

    def _freeze_posts(
        self, phase: CommunityPhaseContext, drafts: Iterable[Mapping[str, Any]]
    ) -> dict[str, FrozenCommunityPost]:
        posts: dict[str, FrozenCommunityPost] = {}
        authors: set[str] = set()
        for raw in drafts:
            post = FrozenCommunityPost.from_draft(
                raw,
                run_id=self.memory.run_id,
                condition_id=self.memory.condition_id,
                phase_id=phase.phase_id,
                allowed_author_ids=self._cohort_set,
                active_author_ids=self._active_agents,
            )
            if post.author_agent_id in authors:
                raise CommunityContractError("An active agent may author at most one post per community phase")
            authors.add(post.author_agent_id)
            if post.post_id in posts:
                raise CommunityContractError("Duplicate deterministic community post ID")
            posts[post.post_id] = post
        return dict(sorted(posts.items()))

    def _validate_selective_reads(
        self,
        phase: CommunityPhaseContext,
        posts: Mapping[str, FrozenCommunityPost],
        selections: Iterable[Mapping[str, Any]],
    ) -> tuple[tuple[str, str], ...]:
        parsed: list[tuple[str, str]] = []
        counts: dict[str, int] = {}
        seen: set[tuple[str, str]] = set()
        for raw in selections:
            data = _exact_mapping(
                raw,
                required=frozenset({"reader_agent_id", "post_id"}),
                label="community selective read",
            )
            reader = self._agent(data["reader_agent_id"])
            post_id = _identifier(data["post_id"], "community selective read.post_id")
            if self.cohort_depths[reader] == 0:
                raise CommunityContractError("Depth 0 agents may not selectively read community posts")
            if post_id not in posts:
                raise CommunityContractError("Selective read references no frozen post in this phase")
            post = posts[post_id]
            if post.author_agent_id == reader:
                raise CommunityContractError("A community author may not selectively read their own post")
            key = (reader, post_id)
            if key in seen:
                raise CommunityContractError("Duplicate community selective read")
            seen.add(key)
            counts[reader] = counts.get(reader, 0) + 1
            if counts[reader] > self._selective_caps[self.cohort_depths[reader]]:
                raise CommunityContractError("Selective read count exceeds the frozen depth-specific cap")
            parsed.append(key)
        return tuple(sorted(parsed))

    def _validate_private_reader_traces(
        self,
        phase: CommunityPhaseContext,
        posts: Mapping[str, FrozenCommunityPost],
        selected: Sequence[tuple[str, str]],
        traces: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], ...]:
        """Seal exactly what each active reader saw and how they reacted.

        Candidate title metadata is a causal exposure even when the body was
        not selected.  These rows remain inside the private observation
        ledger.  The service, not the model/provider, assigns their exposure
        IDs and checks the final board score against the complete reaction
        batch before committing either artifact.
        """

        if len(traces) != len(self._active_agents):
            raise CommunityContractError(
                "Private reader traces must cover every Depth 1/2 reader"
            )
        selected_set = set(selected)
        readers: list[dict[str, Any]] = []
        seen_readers: set[str] = set()
        snapshot_by_post: dict[str, tuple[str, str, int, int]] = {}
        reaction_counts: dict[str, tuple[int, int]] = {}
        for raw_trace in traces:
            trace = _exact_mapping(
                raw_trace,
                required=frozenset({"reader_agent_id", "candidates"}),
                label="private community reader trace",
            )
            reader = self._agent(trace["reader_agent_id"])
            if reader not in self._active_agents or reader in seen_readers:
                raise CommunityContractError(
                    "Private reader trace has an ineligible or duplicate reader"
                )
            seen_readers.add(reader)
            raw_candidates = trace["candidates"]
            if not isinstance(raw_candidates, Sequence) or isinstance(
                raw_candidates, (str, bytes, bytearray)
            ):
                raise CommunityContractError(
                    "Private reader trace candidates must be an ordered array"
                )
            expected_post_ids = {
                post_id
                for post_id, post in posts.items()
                if post.author_agent_id != reader
            }
            candidates: list[dict[str, Any]] = []
            seen_posts: set[str] = set()
            for raw_candidate in raw_candidates:
                candidate = _exact_mapping(
                    raw_candidate,
                    required=frozenset(
                        {
                            "post_id",
                            "title",
                            "post_type",
                            "score",
                            "like_count",
                            "selected",
                            "reaction",
                        }
                    ),
                    label="private reader candidate",
                )
                post_id = _identifier(
                    candidate["post_id"], "private reader candidate.post_id"
                )
                if post_id not in expected_post_ids or post_id in seen_posts:
                    raise CommunityContractError(
                        "Private reader candidate is foreign, self-authored, or duplicated"
                    )
                seen_posts.add(post_id)
                post = posts[post_id]
                title = _text(
                    candidate["title"],
                    "private reader candidate.title",
                    max_chars=_MAX_TITLE_CHARS,
                )
                post_type = _text(
                    candidate["post_type"],
                    "private reader candidate.post_type",
                    max_chars=32,
                    preserve=False,
                )
                score = _strict_int(
                    candidate["score"], "private reader candidate.score"
                )
                like_count = _strict_int(
                    candidate["like_count"],
                    "private reader candidate.like_count",
                    minimum=0,
                )
                if title != post.title or post_type != post.post_type:
                    raise CommunityContractError(
                        "Private candidate title/type differs from the frozen post"
                    )
                snapshot = (title, post_type, score, like_count)
                previous_snapshot = snapshot_by_post.setdefault(post_id, snapshot)
                if previous_snapshot != snapshot:
                    raise CommunityContractError(
                        "Candidate snapshot differs across readers in one PM batch"
                    )
                if not isinstance(candidate["selected"], bool):
                    raise CommunityContractError(
                        "Private reader candidate.selected must be boolean"
                    )
                was_selected = bool(candidate["selected"])
                reaction_raw = candidate["reaction"]
                if was_selected:
                    reaction = _text(
                        reaction_raw,
                        "private reader candidate.reaction",
                        max_chars=16,
                        preserve=False,
                    )
                    if reaction not in _REACTIONS:
                        raise CommunityContractError(
                            "Private reader candidate reaction is invalid"
                        )
                    likes, unlikes = reaction_counts.get(post_id, (0, 0))
                    if reaction == "like":
                        likes += 1
                    elif reaction == "unlike":
                        unlikes += 1
                    reaction_counts[post_id] = (likes, unlikes)
                else:
                    if reaction_raw is not None:
                        raise CommunityContractError(
                            "An unselected title-only candidate may not carry a reaction"
                        )
                    reaction = None
                if was_selected != ((reader, post_id) in selected_set):
                    raise CommunityContractError(
                        "Private reader trace selection differs from committed selective reads"
                    )
                title_sha = hashlib.sha256(title.encode("utf-8")).hexdigest()
                exposure_id = "exp:" + _sha256(
                    {
                        "run_id": self.memory.run_id,
                        "condition_id": self.memory.condition_id,
                        "phase_id": phase.phase_id,
                        "reader_agent_id": reader,
                        "post_id": post_id,
                        "content_level": "title_only",
                        "title_sha256": title_sha,
                        "score": score,
                        "like_count": like_count,
                    }
                )[:40]
                candidates.append(
                    {
                        "source_exposure_id": exposure_id,
                        "post_id": post_id,
                        "content_level": "title_only",
                        "title": title,
                        "title_sha256": title_sha,
                        "post_type": post_type,
                        "score": score,
                        "like_count": like_count,
                        "selected": was_selected,
                        "reaction": reaction,
                    }
                )
            if seen_posts != expected_post_ids:
                raise CommunityContractError(
                    "Private reader trace does not preserve the exact candidate board"
                )
            readers.append(
                {
                    "reader_agent_id": reader,
                    "candidates": sorted(
                        candidates, key=lambda item: item["post_id"]
                    ),
                }
            )
        if seen_readers != self._active_agents:
            raise CommunityContractError(
                "Private reader traces omit an active community reader"
            )
        for post_id, post in posts.items():
            snapshot = snapshot_by_post.get(post_id)
            # A one-agent active cohort can have a self-authored post with no
            # eligible readers.  In the study cohort every post has readers,
            # but keeping this branch explicit makes the contract general.
            if snapshot is None:
                if len(self._active_agents) > 1:
                    raise CommunityContractError(
                        "A frozen post is absent from every candidate snapshot"
                    )
                continue
            _, _, base_score, base_likes = snapshot
            likes, unlikes = reaction_counts.get(post_id, (0, 0))
            if post.score != base_score + likes - unlikes:
                raise CommunityContractError(
                    "Frozen post score differs from the sealed reaction batch"
                )
            if post.like_count != base_likes + likes:
                raise CommunityContractError(
                    "Frozen post like_count differs from the sealed reaction batch"
                )
        return tuple(sorted(readers, key=lambda item: item["reader_agent_id"]))

    def _schedule_payload(self, phase: CommunityPhaseContext, best: Sequence[FrozenCommunityPost]) -> dict[str, Any]:
        status = "empty" if not best else "right_censored" if phase.next_am_event is None else "scheduled"
        return {
            "schema_version": "rn-community-best-schedule-v1",
            "phase": phase.to_dict(),
            "status": status,
            "best_posts": [post.to_dict() for post in best],
            "audience_agent_ids": list(self.cohort_agent_ids),
        }

    def _assert_schedule_payload(self, payload: Any, phase: CommunityPhaseContext) -> None:
        data = _exact_mapping(
            payload,
            required=frozenset(
                {
                    "schema_version",
                    "phase",
                    "status",
                    "best_posts",
                    "audience_agent_ids",
                    "community_timing_policy_sha256",
                }
            ),
            label="stored community Best schedule",
        )
        if data["schema_version"] != "rn-community-best-schedule-v1":
            raise CommunityContractError("Stored Best schedule has an unknown schema version")
        stored_phase = CommunityPhaseContext.from_mapping(data["phase"])
        if stored_phase != phase:
            raise CommunityContractError("Stored Best schedule phase differs from delivery phase")
        if data["status"] not in {"empty", "scheduled", "right_censored"}:
            raise CommunityContractError("Stored Best schedule status is invalid")
        if not isinstance(data["best_posts"], list) or not isinstance(data["audience_agent_ids"], list):
            raise CommunityContractError("Stored Best schedule arrays are malformed")
        audience = tuple(_identifier(item, "stored Best audience agent_id") for item in data["audience_agent_ids"])
        if audience != self.cohort_agent_ids:
            raise CommunityContractError("Stored Best schedule audience differs from the full frozen cohort")
        if len(data["best_posts"]) > self.best_k:
            raise CommunityContractError("Stored Best schedule exceeds the frozen Best cap")
        if _sha(data["community_timing_policy_sha256"], "stored Best schedule timing policy hash") != (
            self.timing_policy_sha256
        ):
            raise CommunityContractError("Stored Best schedule timing policy hash differs from the resolved policy")

    # ----- Append-only persistence --------------------------------------------------

    def _write_observation(
        self,
        connection: sqlite3.Connection,
        *,
        event_id: str,
        turn: int,
        date: str,
        stage: str,
        payload: Mapping[str, Any],
    ) -> str:
        event = _identifier(event_id, "observation event_id")
        stage_id = _identifier(stage, "observation stage")
        payload_with_timing = dict(payload)
        # ``PaperMemoryStore`` deliberately rehydrates community claims from a
        # four-field, exact-schema observation.  Do not turn a policy-audit
        # field into agent-visible claim state or weaken that closed contract.
        # The parent PM schedule/delivery observations are timing-hash-bound;
        # a claim is a child of those sealed delivered exposures.
        if not stage_id.startswith("community_claims:"):
            supplied_policy_hash = payload_with_timing.get("community_timing_policy_sha256")
            if supplied_policy_hash is not None and _sha(
                supplied_policy_hash,
                "community observation timing policy hash",
            ) != self.timing_policy_sha256:
                raise CommunityContractError("Community observation timing policy hash differs from the resolved policy")
            payload_with_timing["community_timing_policy_sha256"] = self.timing_policy_sha256
        elif "community_timing_policy_sha256" in payload_with_timing:
            raise CommunityContractError("Community claim observation may not add timing metadata")
        payload_json = _canonical_json(payload_with_timing)
        payload_sha = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        observation_id = "obs:" + _sha256(
            {
                "run_id": self.memory.run_id,
                "condition_id": self.memory.condition_id,
                "event_id": event,
                "stage": stage_id,
            }
        )[:40]
        values = {
            "observation_id": observation_id,
            "run_id": self.memory.run_id,
            "condition_id": self.memory.condition_id,
            "event_id": event,
            "turn": _strict_int(turn, "observation turn", minimum=1),
            "date": _parse_date(date, "observation date"),
            "stage": stage_id,
            "payload_json": payload_json,
            "payload_sha256": payload_sha,
        }
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        try:
            connection.execute(
                f"INSERT INTO observation_events ({columns}) VALUES ({placeholders})", tuple(values.values())
            )
        except sqlite3.IntegrityError as exc:
            existing = connection.execute(
                """
                SELECT * FROM observation_events
                WHERE run_id = ? AND condition_id = ? AND event_id = ? AND stage = ?
                """,
                (self.memory.run_id, self.memory.condition_id, event, stage_id),
            ).fetchone()
            if existing is None or (
                str(existing["observation_id"]) != observation_id
                or str(existing["payload_sha256"]) != payload_sha
                or str(existing["payload_json"]) != payload_json
                or int(existing["turn"]) != values["turn"]
                or str(existing["date"]) != values["date"]
            ):
                raise CommunityContractError("Conflicting append-only community observation replay") from exc
        return observation_id

    def _read_observation(
        self, connection: sqlite3.Connection, *, event_id: str, stage: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT payload_json, payload_sha256 FROM observation_events
            WHERE run_id = ? AND condition_id = ? AND event_id = ? AND stage = ?
            """,
            (self.memory.run_id, self.memory.condition_id, event_id, stage),
        ).fetchone()
        if row is None:
            raise CommunityContractError("Required sealed community observation is missing")
        payload_json = str(row["payload_json"])
        if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != str(row["payload_sha256"]):
            raise CommunityContractError("Stored community observation hash mismatch")
        try:
            parsed = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise CommunityContractError("Stored community observation is not valid JSON") from exc
        if _canonical_json(parsed) != payload_json:
            raise CommunityContractError("Stored community observation is not canonical")
        if not isinstance(parsed, dict):
            raise CommunityContractError("Stored community observation must be an object")
        return parsed

    def _insert_exposure(
        self,
        connection: sqlite3.Connection,
        *,
        agent_id: str,
        event_id: str,
        channel: str,
        root_id: str,
        body_sha256: str,
        delivered_at: str | None,
        status: str,
        metadata: Mapping[str, Any],
    ) -> str:
        if channel not in {"community_best", "community_selected"}:
            raise CommunityContractError("RN community exposure channel is invalid")
        if status not in {"delivered", "right_censored"}:
            raise CommunityContractError("RN community service writes only delivered/right-censored exposures")
        if status == "delivered" and delivered_at is None:
            raise CommunityContractError("Delivered exposure requires a delivered_at timestamp")
        if status == "right_censored" and delivered_at is not None:
            raise CommunityContractError("Right-censored exposure may not carry a delivered_at timestamp")
        agent = self._agent(agent_id)
        event = _identifier(event_id, "community exposure event_id")
        root = _identifier(root_id, "community exposure root_id")
        body_hash = _sha(body_sha256, "community exposure body_sha256")
        metadata_json = _canonical_json(dict(metadata))
        self._validate_exposure_metadata(
            json.loads(metadata_json),
            channel=channel,
            expected_root_id=root,
            expected_body_sha256=body_hash,
        )
        exposure_id = "exp:" + _sha256(
            {
                "run_id": self.memory.run_id,
                "condition_id": self.memory.condition_id,
                "agent_id": agent,
                "event_id": event,
                "channel": channel,
                "root_id": root,
                "body_sha256": body_hash,
                "status": status,
            }
        )[:40]
        values = {
            "exposure_id": exposure_id,
            "run_id": self.memory.run_id,
            "condition_id": self.memory.condition_id,
            "agent_id": agent,
            "event_id": event,
            "channel": channel,
            "root_id": root,
            "body_sha256": body_hash,
            "delivered_at": delivered_at,
            "status": status,
            "metadata_json": metadata_json,
        }
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        try:
            connection.execute(
                f"INSERT INTO agent_exposures ({columns}) VALUES ({placeholders})", tuple(values.values())
            )
        except sqlite3.IntegrityError as exc:
            existing = connection.execute(
                "SELECT * FROM agent_exposures WHERE exposure_id = ?", (exposure_id,)
            ).fetchone()
            if existing is None or any(
                (existing[key] is not None and str(existing[key]) or None)
                != (str(value) if value is not None else None)
                for key, value in values.items()
            ):
                raise CommunityContractError("Conflicting append-only community exposure replay") from exc
        return exposure_id

    def _insert_edge(
        self,
        connection: sqlite3.Connection,
        *,
        edge_id: str,
        agent_id: str,
        event_id: str,
        target_id: str,
        source_id: str,
        dimension: str | None,
    ) -> None:
        values = {
            "edge_id": edge_id,
            "run_id": self.memory.run_id,
            "condition_id": self.memory.condition_id,
            "agent_id": agent_id,
            "event_id": event_id,
            "target_kind": "stb",
            "target_id": target_id,
            "source_kind": "community_claim",
            "source_id": source_id,
            "dimension": dimension,
        }
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        try:
            connection.execute(
                f"INSERT INTO memory_evidence_edges ({columns}) VALUES ({placeholders})", tuple(values.values())
            )
        except sqlite3.IntegrityError as exc:
            existing = connection.execute("SELECT * FROM memory_evidence_edges WHERE edge_id = ?", (edge_id,)).fetchone()
            if existing is None or any(
                (existing[key] is not None and str(existing[key]) or None)
                != (str(value) if value is not None else None)
                for key, value in values.items()
            ):
                raise CommunityContractError("Conflicting append-only community evidence edge replay") from exc

    # ----- Payload projections and database reads ----------------------------------

    def _selected_metadata(self, phase: CommunityPhaseContext, post: FrozenCommunityPost) -> dict[str, Any]:
        return {
            "schema_version": "rn-community-exposure-v1",
            "source_phase_id": phase.phase_id,
            "visible_from_event_id": None if phase.next_am_event is None else phase.next_am_event.event_id,
            "content_level": "full_body",
            "post_id": post.post_id,
            "ledger_author_agent_id": post.author_agent_id,
            "title": post.title,
            "full_body": post.body,
            "body_sha256": post.body_sha256,
            "content_version": post.content_version,
            "content_version_sha256": post.content_version_sha256,
            "post_type": post.post_type,
            "score": post.score,
            "like_count": post.like_count,
            "rank": None,
            "public_author_profile": self.public_profiles[post.author_agent_id].to_dict(),
        }

    def _best_metadata(self, phase: CommunityPhaseContext, post: FrozenCommunityPost, rank: int) -> dict[str, Any]:
        return {
            "schema_version": "rn-community-exposure-v1",
            "source_phase_id": phase.phase_id,
            "visible_from_event_id": phase.next_am_event.event_id if phase.next_am_event else None,
            "content_level": "full_body",
            "post_id": post.post_id,
            "ledger_author_agent_id": post.author_agent_id,
            "title": post.title,
            "full_body": post.body,
            "body_sha256": post.body_sha256,
            "content_version": post.content_version,
            "content_version_sha256": post.content_version_sha256,
            "post_type": post.post_type,
            "score": post.score,
            "like_count": post.like_count,
            "rank": rank,
            "public_author_profile": None,
        }

    def _right_censored_metadata(
        self, phase: CommunityPhaseContext, post: FrozenCommunityPost, rank: int
    ) -> dict[str, Any]:
        return {
            "schema_version": "rn-community-exposure-v1",
            "source_phase_id": phase.phase_id,
            "visible_from_event_id": None,
            "content_level": "full_body",
            "post_id": post.post_id,
            "ledger_author_agent_id": post.author_agent_id,
            "title": post.title,
            "full_body": post.body,
            "body_sha256": post.body_sha256,
            "content_version": post.content_version,
            "content_version_sha256": post.content_version_sha256,
            "post_type": post.post_type,
            "score": post.score,
            "like_count": post.like_count,
            "rank": rank,
            "public_author_profile": None,
        }

    def _validate_exposure_metadata(
        self,
        metadata: Any,
        *,
        channel: str,
        expected_root_id: str,
        expected_body_sha256: str,
    ) -> None:
        data = _exact_mapping(
            metadata,
            required=frozenset(
                {
                    "schema_version",
                    "source_phase_id",
                    "visible_from_event_id",
                    "content_level",
                    "post_id",
                    "ledger_author_agent_id",
                    "title",
                    "full_body",
                    "body_sha256",
                    "content_version",
                    "content_version_sha256",
                    "post_type",
                    "score",
                    "like_count",
                    "rank",
                    "public_author_profile",
                }
            ),
            label="community exposure metadata",
        )
        if data["schema_version"] != "rn-community-exposure-v1" or data["content_level"] != "full_body":
            raise CommunityContractError("Community exposure metadata has an invalid schema/content level")
        if _identifier(data["post_id"], "community exposure metadata.post_id") != expected_root_id:
            raise CommunityContractError("Community exposure metadata root differs from its row")
        body = _text(data["full_body"], "community exposure metadata.full_body", max_chars=_MAX_BODY_CHARS)
        body_hash = _sha(data["body_sha256"], "community exposure metadata.body_sha256")
        if body_hash != expected_body_sha256 or _body_sha256(body) != body_hash:
            raise CommunityContractError("Community exposure metadata body/hash mismatch")
        content_version = _text(
            data["content_version"], "community exposure metadata.content_version", max_chars=64, preserve=False
        )
        content_version_sha = _sha(
            data["content_version_sha256"], "community exposure metadata.content_version_sha256"
        )
        _identifier(data["source_phase_id"], "community exposure metadata.source_phase_id")
        _identifier(data["ledger_author_agent_id"], "community exposure metadata.ledger_author_agent_id")
        title = _text(data["title"], "community exposure metadata.title", max_chars=_MAX_TITLE_CHARS)
        post_type = _text(data["post_type"], "community exposure metadata.post_type", max_chars=32, preserve=False)
        if not _POST_TYPE_RE.fullmatch(post_type):
            raise CommunityContractError("Community exposure metadata post_type is invalid")
        if content_version != _POST_CONTENT_VERSION or content_version_sha != _content_version_sha256(
            title=title,
            body=body,
            body_sha256=body_hash,
            post_type=post_type,
        ):
            raise CommunityContractError("Community exposure metadata content version/hash mismatch")
        _strict_int(data["score"], "community exposure metadata.score")
        _strict_int(data["like_count"], "community exposure metadata.like_count", minimum=0)
        if data["visible_from_event_id"] is not None:
            _identifier(data["visible_from_event_id"], "community exposure metadata.visible_from_event_id")
        if channel == "community_best":
            _strict_int(data["rank"], "community Best exposure rank", minimum=1)
            if data["public_author_profile"] is not None:
                raise CommunityContractError("Best broadcast may not leak an author profile")
        else:
            if data["rank"] is not None:
                raise CommunityContractError("Selective-read exposure may not invent a Best rank")
            PublicAuthorProfile.from_mapping(data["public_author_profile"], label="selective-read public profile")

    def _payload_projection(
        self,
        post: FrozenCommunityPost,
        *,
        exposure_ids: tuple[str, ...],
        channel: str,
        rank: int | None,
        profile: Mapping[str, Any] | None,
        reader_reaction: str | None,
    ) -> dict[str, Any]:
        if _body_sha256(post.body) != post.body_sha256:
            raise CommunityContractError("Cannot project a corrupted full-body community post")
        payload: dict[str, Any] = {
            "post_id": post.post_id,
            "title": post.title,
            "body": post.body,
            "body_sha256": post.body_sha256,
            "content_version": post.content_version,
            "content_version_sha256": post.content_version_sha256,
            "post_type": post.post_type,
            "score": post.score,
            "source_exposure_ids": list(exposure_ids),
            "exposure_channel": channel,
            "untrusted_content_kind": "community_post",
            "content_level": "full_body",
            "reader_reaction": reader_reaction,
        }
        if rank is not None:
            payload["rank"] = rank
        if profile is not None:
            payload["public_author_profile"] = PublicAuthorProfile.from_mapping(
                profile, label="projected public author profile"
            ).to_dict()
        return payload

    def _candidate_payloads(
        self,
        *,
        agent_id: str,
        event_id: str,
    ) -> tuple[dict[str, Any], ...]:
        """Reload private title-only candidate/reaction context for next AM."""

        with connect(self.memory.db_path, read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT event_id, stage, payload_json, payload_sha256
                FROM observation_events
                WHERE run_id = ? AND condition_id = ?
                  AND stage LIKE 'community_reader_trace:%'
                ORDER BY event_id, stage
                """,
                (self.memory.run_id, self.memory.condition_id),
            ).fetchall()
            matches: list[dict[str, Any]] = []
            for row in rows:
                payload_json = str(row["payload_json"])
                if (
                    hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
                    != str(row["payload_sha256"])
                ):
                    raise CommunityContractError(
                        "Stored private reader trace hash mismatch"
                    )
                try:
                    payload = json.loads(payload_json)
                except json.JSONDecodeError as exc:
                    raise CommunityContractError(
                        "Stored private reader trace is not JSON"
                    ) from exc
                if _canonical_json(payload) != payload_json:
                    raise CommunityContractError(
                        "Stored private reader trace is not canonical"
                    )
                if (
                    isinstance(payload, Mapping)
                    and payload.get("visible_from_event_id") == event_id
                ):
                    matches.append(payload)
        if len(matches) > 1:
            raise CommunityContractError(
                "Multiple PM reader traces target one next-AM event"
            )
        if not matches:
            return ()
        payload = _exact_mapping(
            matches[0],
            required=frozenset(
                {
                    "schema_version",
                    "phase",
                    "visible_from_event_id",
                    "readers",
                    "community_timing_policy_sha256",
                }
            ),
            label="stored private reader trace",
        )
        if payload["schema_version"] != "rn-community-reader-trace-v1":
            raise CommunityContractError(
                "Stored private reader trace has an unknown schema"
            )
        if (
            _sha(
                payload["community_timing_policy_sha256"],
                "stored private reader trace timing policy hash",
            )
            != self.timing_policy_sha256
        ):
            raise CommunityContractError(
                "Stored private reader trace timing policy differs"
            )
        if payload["visible_from_event_id"] != event_id:
            raise CommunityContractError(
                "Stored private reader trace targets another event"
            )
        stored_phase = CommunityPhaseContext.from_mapping(payload["phase"])
        if (
            stored_phase.next_am_event is None
            or stored_phase.next_am_event.event_id != event_id
        ):
            raise CommunityContractError(
                "Stored private reader trace phase does not target this AM event"
            )
        readers = payload["readers"]
        if not isinstance(readers, list):
            raise CommunityContractError(
                "Stored private reader trace readers are malformed"
            )
        reader_matches = [
            item
            for item in readers
            if isinstance(item, Mapping)
            and item.get("reader_agent_id") == agent_id
        ]
        if self.cohort_depths[agent_id] == 0:
            if reader_matches:
                raise CommunityContractError(
                    "Depth 0 unexpectedly has a candidate/reaction trace"
                )
            return ()
        if len(reader_matches) != 1:
            raise CommunityContractError(
                "Active reader is not covered exactly once by its private trace"
            )
        reader = _exact_mapping(
            reader_matches[0],
            required=frozenset({"reader_agent_id", "candidates"}),
            label="stored private reader row",
        )
        raw_candidates = reader["candidates"]
        if not isinstance(raw_candidates, list):
            raise CommunityContractError(
                "Stored private reader candidates are malformed"
            )
        result: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for raw_candidate in raw_candidates:
            candidate = _exact_mapping(
                raw_candidate,
                required=frozenset(
                    {
                        "source_exposure_id",
                        "post_id",
                        "content_level",
                        "title",
                        "title_sha256",
                        "post_type",
                        "score",
                        "like_count",
                        "selected",
                        "reaction",
                    }
                ),
                label="stored title-only candidate",
            )
            exposure_id = _identifier(
                candidate["source_exposure_id"],
                "stored title-only candidate.source_exposure_id",
            )
            if exposure_id in seen_ids:
                raise CommunityContractError(
                    "Stored title-only candidate repeats an exposure ID"
                )
            seen_ids.add(exposure_id)
            post_id = _identifier(
                candidate["post_id"], "stored title-only candidate.post_id"
            )
            title = _text(
                candidate["title"],
                "stored title-only candidate.title",
                max_chars=_MAX_TITLE_CHARS,
            )
            title_sha = _sha(
                candidate["title_sha256"],
                "stored title-only candidate.title_sha256",
            )
            if hashlib.sha256(title.encode("utf-8")).hexdigest() != title_sha:
                raise CommunityContractError(
                    "Stored title-only candidate title hash mismatch"
                )
            if candidate["content_level"] != "title_only":
                raise CommunityContractError(
                    "Stored candidate exposure contains more than title-only data"
                )
            post_type = _text(
                candidate["post_type"],
                "stored title-only candidate.post_type",
                max_chars=32,
                preserve=False,
            )
            if not _POST_TYPE_RE.fullmatch(post_type):
                raise CommunityContractError(
                    "Stored title-only candidate post_type is invalid"
                )
            score = _strict_int(
                candidate["score"], "stored title-only candidate.score"
            )
            like_count = _strict_int(
                candidate["like_count"],
                "stored title-only candidate.like_count",
                minimum=0,
            )
            expected_exposure_id = "exp:" + _sha256(
                {
                    "run_id": self.memory.run_id,
                    "condition_id": self.memory.condition_id,
                    "phase_id": stored_phase.phase_id,
                    "reader_agent_id": agent_id,
                    "post_id": post_id,
                    "content_level": "title_only",
                    "title_sha256": title_sha,
                    "score": score,
                    "like_count": like_count,
                }
            )[:40]
            if exposure_id != expected_exposure_id:
                raise CommunityContractError(
                    "Stored title-only candidate exposure identity is invalid"
                )
            if not isinstance(candidate["selected"], bool):
                raise CommunityContractError(
                    "Stored title-only candidate.selected is invalid"
                )
            reaction = candidate["reaction"]
            if candidate["selected"]:
                if reaction not in _REACTIONS:
                    raise CommunityContractError(
                        "Stored selected candidate reaction is invalid"
                    )
            elif reaction is not None:
                raise CommunityContractError(
                    "Stored unselected candidate has a reaction"
                )
            result.append(
                {
                    "post_id": post_id,
                    "title": title,
                    "post_type": post_type,
                    "score": score,
                    "like_count": like_count,
                    "selected": bool(candidate["selected"]),
                    "reader_reaction": reaction,
                    "source_exposure_ids": [exposure_id],
                    "exposure_channel": "title_only_candidate",
                    "untrusted_content_kind": "community_post",
                    "content_level": "title_only",
                }
            )
        return tuple(sorted(result, key=lambda item: item["post_id"]))

    def _load_exposure_metadata(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            metadata = json.loads(str(row["metadata_json"]))
        except json.JSONDecodeError as exc:
            raise CommunityContractError("Stored community exposure metadata is invalid JSON") from exc
        if _canonical_json(metadata) != str(row["metadata_json"]):
            raise CommunityContractError("Stored community exposure metadata is not canonical")
        self._validate_exposure_metadata(
            metadata,
            channel=str(row["channel"]),
            expected_root_id=str(row["root_id"]),
            expected_body_sha256=str(row["body_sha256"]),
        )
        return metadata

    def _visible_exposure_rows(self, *, agent_id: str, event_id: str) -> dict[str, sqlite3.Row]:
        with connect(self.memory.db_path, read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_exposures
                WHERE run_id = ? AND condition_id = ? AND agent_id = ? AND status = 'delivered'
                  AND (
                    (channel = 'community_best' AND event_id = ?)
                    OR (channel = 'community_selected' AND json_extract(metadata_json, '$.visible_from_event_id') = ?)
                  )
                """,
                (self.memory.run_id, self.memory.condition_id, agent_id, event_id, event_id),
            ).fetchall()
        result: dict[str, sqlite3.Row] = {}
        for row in rows:
            if str(row["channel"]) not in {"community_best", "community_selected"}:
                continue
            self._load_exposure_metadata(row)
            exposure_id = str(row["exposure_id"])
            if exposure_id in result:
                raise CommunityContractError("Duplicate stored community exposure ID")
            result[exposure_id] = row
        return result

    def _visible_claim_sources(
        self,
        *,
        agent_id: str,
        event_id: str,
    ) -> dict[str, dict[str, Any]]:
        """Return reader-owned source material with an explicit content level."""

        sources: dict[str, dict[str, Any]] = {}
        for exposure_id, row in self._visible_exposure_rows(
            agent_id=agent_id,
            event_id=event_id,
        ).items():
            metadata = self._load_exposure_metadata(row)
            sources[exposure_id] = {
                "root_id": str(row["root_id"]),
                "content_level": "full_body",
                "allowed_texts": (
                    str(metadata["title"]),
                    str(metadata["full_body"]),
                ),
            }
        for candidate in self._candidate_payloads(
            agent_id=agent_id,
            event_id=event_id,
        ):
            exposure_ids = candidate["source_exposure_ids"]
            if not isinstance(exposure_ids, list) or len(exposure_ids) != 1:
                raise CommunityContractError(
                    "Title-only candidate has an invalid source identity"
                )
            exposure_id = str(exposure_ids[0])
            material = {
                "root_id": str(candidate["post_id"]),
                "content_level": "title_only",
                "allowed_texts": (str(candidate["title"]),),
            }
            if exposure_id in sources and sources[exposure_id] != material:
                raise CommunityContractError(
                    "Community source exposure identity collision"
                )
            sources[exposure_id] = material
        return sources

    def _event_row_for_claim(self, connection: sqlite3.Connection, event_id: str) -> dict[str, Any]:
        schedule = self.memory.event_schedule
        if schedule is None:
            raise CommunityContractError("RN community requires EventSchedule")
        event = schedule.event(event_id)
        if str(event["subturn"]) != "am":
            raise CommunityContractError("Community interpretation claims are permitted only at an AM event")
        return {"turn": int(event["turn"]), "date": str(event["date"])}

    def _assert_am_interpretation_event(self, event_id: str) -> None:
        schedule = self.memory.event_schedule
        if schedule is None:
            raise CommunityContractError("RN community requires EventSchedule")
        event = schedule.event(event_id)
        if str(event["subturn"]) != "am":
            raise CommunityContractError("Community interpretation is permitted only at an AM event")

    # ----- Claim lineage ------------------------------------------------------------

    def _assert_claim_exists(
        self,
        claim: CommunityClaim,
        *,
        connection: sqlite3.Connection | None = None,
        agent_id: str | None = None,
        event_id: str | None = None,
    ) -> None:
        own_connection = connection is None
        if connection is None:
            connection = connect(self.memory.db_path, read_only=True)
        try:
            if agent_id is None or event_id is None:
                row = connection.execute(
                    """
                    SELECT event_id, payload_json FROM observation_events
                    WHERE run_id = ? AND condition_id = ? AND stage LIKE 'community_claims:%'
                    """,
                    (self.memory.run_id, self.memory.condition_id),
                ).fetchall()
                matches: list[tuple[str, str]] = []
                for item in row:
                    parsed = self._read_observation(
                        connection, event_id=str(item["event_id"]), stage=self._claims_stage_from_payload(item["payload_json"])
                    )
                    if any(entry.get("claim_id") == claim.claim_id for entry in parsed.get("claims", [])):
                        matches.append((str(parsed["agent_id"]), str(parsed["event_id"])))
                if len(matches) != 1:
                    raise CommunityContractError("Community claim does not exist exactly once in this run/condition")
                agent_id, event_id = matches[0]
            payload = self._read_observation(
                connection,
                event_id=event_id,
                stage=self._claims_stage(agent_id),
            )
            expected = claim.stage_projection()
            matching = [item for item in payload.get("claims", []) if item.get("claim_id") == claim.claim_id]
            if len(matching) != 1 or any(matching[0].get(key) != value for key, value in expected.items()):
                raise CommunityContractError("Community claim identity/content does not match its append-only ledger")
            available = self._visible_claim_sources(
                agent_id=agent_id,
                event_id=event_id,
            )
            if set(claim.source_exposure_ids) - set(available):
                raise CommunityContractError("Community claim references exposure not visible to its agent/event")
            if matching[0].get("supporting_quote") != claim.supporting_quote:
                raise CommunityContractError(
                    "Community claim quote differs from its private append-only ledger"
                )
            if not any(
                any(
                    claim.supporting_quote in source_text
                    for source_text in available[source]["allowed_texts"]
                )
                for source in claim.source_exposure_ids
            ):
                raise CommunityContractError(
                    "Community claim quote is unsupported at its delivered source level"
                )
        finally:
            if own_connection:
                connection.close()

    @staticmethod
    def _claims_stage_from_payload(payload_json: Any) -> str:
        try:
            parsed = json.loads(str(payload_json))
        except json.JSONDecodeError as exc:
            raise CommunityContractError("Stored claim payload is malformed") from exc
        if not isinstance(parsed, Mapping):
            raise CommunityContractError("Stored claim payload is malformed")
        return RNCommunityService._claims_stage(_identifier(parsed.get("agent_id"), "stored claim agent_id"))

    def _validate_claim_dimensions(
        self,
        claims: Sequence[CommunityClaim],
        dimensions_by_claim: Mapping[str, Sequence[str]] | None,
    ) -> dict[str, tuple[str | None, ...]]:
        claim_ids = {claim.claim_id for claim in claims}
        if dimensions_by_claim is None:
            return {claim.claim_id: (None,) for claim in claims}
        if set(dimensions_by_claim) != claim_ids:
            raise CommunityContractError("dimensions_by_claim must cover exactly the supplied claim IDs")
        result: dict[str, tuple[str | None, ...]] = {}
        valid_dimensions = {f"dim_{index}" for index in range(1, 7)}
        for claim_id, raw_dimensions in dimensions_by_claim.items():
            if not isinstance(raw_dimensions, Sequence) or isinstance(raw_dimensions, (str, bytes)) or not raw_dimensions:
                raise CommunityContractError("Each claim must map to a non-empty dimension array")
            values = tuple(sorted(_text(item, "community claim dimension", max_chars=5, preserve=False) for item in raw_dimensions))
            if set(values) - valid_dimensions or len(values) != len(set(values)):
                raise CommunityContractError("Community claim dimensions must be distinct dim_1..dim_6")
            result[claim_id] = values
        return result

    # ----- Names and simple helpers -------------------------------------------------

    def _agent(self, value: Any) -> str:
        agent = _identifier(value, "community agent_id")
        if agent not in self._cohort_set:
            raise CommunityContractError("Community agent is outside the frozen cohort")
        return agent

    @staticmethod
    def _posts_stage(phase_id: str) -> str:
        return f"community_posts:{phase_id}"

    @staticmethod
    def _selected_stage(phase_id: str) -> str:
        return f"community_selected:{phase_id}"

    @staticmethod
    def _reader_trace_stage(phase_id: str) -> str:
        return f"community_reader_trace:{phase_id}"

    @staticmethod
    def _schedule_stage(phase_id: str) -> str:
        return f"community_best_schedule:{phase_id}"

    @staticmethod
    def _delivery_stage(phase_id: str) -> str:
        return f"community_best_delivery:{phase_id}"

    @staticmethod
    def _checkpoint_stage(phase_id: str) -> str:
        return f"community_checkpoint:{phase_id}"

    @staticmethod
    def _claims_stage(agent_id: str) -> str:
        return f"community_claims:{agent_id}"


__all__ = [
    "CommunityCheckpoint",
    "CommunityClaim",
    "CommunityContractError",
    "CommunityPhaseContext",
    "CommunityTimingPolicy",
    "EventReference",
    "FrozenCommunityPost",
    "PublicAuthorProfile",
    "RegisteredCommunityPhase",
    "RNCommunityService",
]
