"""Deterministic, no-network preparation inputs for the RN paper path.

These artifacts close three data-preparation gaps without pretending that a
human signed or an external service supplied data:

* Depth-2 recent-search results are selected only from the already reviewed,
  sealed real-news registry.  Current-event articles and any article outside
  the seven-calendar-day cutoff window are excluded.
* public author profiles are uniform, neutral, and contain no runtime state;
* the community post truth policy is a code-owned exact contract; and
* one generation manifest binds every output to the resolved study inputs.

The artifacts do not authorize paid execution.  Live strict-reasoning canary
and journal-aware LLM providers remain separate gates.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from twinmarket_kr.rn_ab.community import CommunityContractError, PublicAuthorProfile
from twinmarket_kr.rn_ab.persona_snapshot import SealedPersonaSnapshot
from twinmarket_kr.rn_ab.resolver import ResolvedStudyManifest, canonical_json_bytes, canonical_sha256


class PreflightInputError(RuntimeError):
    """Generated preparation inputs differ from their sealed source contract."""


DEPTH2_REGISTRY_VERSION = "rn-depth2-recent-search-v1"
PUBLIC_PROFILE_REGISTRY_VERSION = "rn-public-author-profile-registry-v1"
COMMUNITY_TRUTH_POLICY_VERSION = "rn-community-post-truth-policy-v1"
GENERATION_MANIFEST_VERSION = "rn-generated-preflight-inputs-v1"

DEPTH2_FILENAME = "depth2_recent_search_registry.json"
PUBLIC_PROFILE_FILENAME = "public_author_profile_registry.json"
TRUTH_POLICY_FILENAME = "community_post_truth_policy.json"
GENERATION_MANIFEST_FILENAME = "generated_input_manifest.json"

_LOOKBACK_DAYS = 7
_SEARCH_TOP_K = 10
_SEARCH_SELECTION_POLICY = (
    "reviewed-clean-news-prior-event-only_then-published-desc_article-id-v1"
)
_PROFILE_GENERATION_POLICY = "uniform-neutral-no-private-runtime-state-v1"
_PUBLIC_PROFILE = {
    "schema_version": "rn-public-profile-v1",
    "public_badges": ["registered-community-member"],
    "public_direction": "neutral",
    "public_reliability_score": 50,
}
_PROHIBITED_READER_FIELDS = (
    "action_reason",
    "agent_id",
    "author_agent_id",
    "belief_summary",
    "cash",
    "fill",
    "fills",
    "holdings",
    "order_history",
    "orders",
    "portfolio",
    "portfolio_summary",
    "private_belief",
    "recent_trade",
    "recent_trades",
    "stable_agent_id",
    "trade_history",
    "view_change",
)
_PUBLIC_PROFILE_FIELDS = (
    "public_badges",
    "public_direction",
    "public_reliability_score",
    "schema_version",
)
_ROOT_FIELDS = frozenset(
    {
        "artifact_type",
        "version",
        "study_id",
        "resolved_study_manifest_sha256",
        "source_bindings",
        "generator_bindings",
        "artifacts",
        "human_approval_claimed",
        "network_requests",
        "paid_api_calls",
        "execution_authorized",
        "manifest_sha256",
    }
)
_DEPTH2_FIELDS = frozenset(
    {
        "artifact_type",
        "version",
        "study_id",
        "calendar_event_registry_sha256",
        "stage_input_registry_sha256",
        "real_news_bundle_sha256",
        "lookback_days",
        "top_k",
        "selection_policy",
        "events",
        "registry_sha256",
    }
)
_DEPTH2_EVENT_FIELDS = frozenset(
    {
        "event_id",
        "cutoff_timestamp",
        "window_start_timestamp",
        "excluded_current_event_article_ids",
        "result_article_ids",
        "result_payload_sha256s",
    }
)
_PROFILE_FIELDS = frozenset(
    {
        "artifact_type",
        "version",
        "study_id",
        "persona_prompt_map_sha256",
        "active_agent_ids_sha256",
        "generation_policy",
        "profiles",
        "registry_sha256",
    }
)
_PROFILE_ROW_FIELDS = frozenset({"agent_id", "profile"})
_TRUTH_FIELDS = frozenset(
    {
        "artifact_type",
        "version",
        "study_id",
        "public_post_is_unverified_author_claim",
        "claims_may_diverge_from_private_state",
        "canonical_fill_truth_source",
        "reader_must_not_infer_canonical_position",
        "prohibited_reader_fields",
        "public_profile_field_allowlist",
        "policy_sha256",
    }
)


@dataclass(frozen=True)
class GeneratedPreflightInputs:
    """Verified run-local preparation artifact set."""

    root: Path
    depth2_registry_path: Path
    public_profile_registry_path: Path
    truth_policy_path: Path
    manifest_path: Path
    depth2_registry: Mapping[str, Any]
    public_profile_registry: Mapping[str, Any]
    truth_policy: Mapping[str, Any]
    manifest: Mapping[str, Any]

    @property
    def manifest_sha256(self) -> str:
        return str(self.manifest["manifest_sha256"])

    @property
    def public_profiles(self) -> dict[str, dict[str, Any]]:
        return {
            str(row["agent_id"]): dict(row["profile"])
            for row in self.public_profile_registry["profiles"]
        }

    def depth2_search_results(
        self,
        *,
        event_id: str,
        news: Any,
    ) -> tuple[dict[str, str], ...]:
        """Return the exact full-text-safe projection pinned for one event."""

        matches = [
            row for row in self.depth2_registry["events"] if row["event_id"] == event_id
        ]
        if len(matches) != 1:
            raise PreflightInputError(
                f"Depth-2 registry does not contain exactly one event row: {event_id}"
            )
        row = matches[0]
        ids = row["result_article_ids"]
        hashes = row["result_payload_sha256s"]
        results: list[dict[str, str]] = []
        for article_id, expected_hash in zip(ids, hashes, strict=True):
            try:
                article = news.articles[article_id]
            except (AttributeError, KeyError) as exc:
                raise PreflightInputError(
                    f"Depth-2 registry article is absent from sealed clean news: {article_id}"
                ) from exc
            if article.payload_sha256 != expected_hash:
                raise PreflightInputError(
                    f"Depth-2 registry payload hash differs for {article_id}"
                )
            results.append(dict(article.stage_projection(news_depth=2)))
        return tuple(results)


def build_generated_preflight_inputs(
    *,
    destination_dir: Path | str,
    resolved: ResolvedStudyManifest,
    personas: SealedPersonaSnapshot,
    stage_inputs: Any,
    news: Any,
) -> GeneratedPreflightInputs:
    """Generate and atomically seal local-only input artifacts.

    The destination must be new.  No source file or runtime database is ever
    modified, and no network/API client is imported.
    """

    if not isinstance(resolved, ResolvedStudyManifest):
        raise PreflightInputError("generated inputs require a ResolvedStudyManifest")
    if not isinstance(personas, SealedPersonaSnapshot):
        raise PreflightInputError("generated inputs require a sealed persona snapshot")
    destination = Path(destination_dir)
    if destination.exists() or destination.is_symlink():
        raise PreflightInputError(f"generated input destination already exists: {destination}")
    if not destination.parent.is_dir():
        raise PreflightInputError("generated input destination parent must already exist")

    personas.assert_agent_set(resolved.agent_ids)
    depth2 = _build_depth2_registry(resolved=resolved, stage_inputs=stage_inputs, news=news)
    profiles = _build_public_profile_registry(resolved=resolved, personas=personas)
    truth = _build_truth_policy(resolved=resolved)
    manifest = _build_generation_manifest(
        resolved=resolved,
        personas=personas,
        stage_inputs=stage_inputs,
        news=news,
        depth2=depth2,
        profiles=profiles,
        truth=truth,
    )

    temporary = Path(tempfile.mkdtemp(prefix=".rn-generated-inputs-", dir=destination.parent))
    try:
        _write_json(temporary / DEPTH2_FILENAME, depth2)
        _write_json(temporary / PUBLIC_PROFILE_FILENAME, profiles)
        _write_json(temporary / TRUTH_POLICY_FILENAME, truth)
        _write_json(temporary / GENERATION_MANIFEST_FILENAME, manifest)
        verified = load_generated_preflight_inputs(
            temporary,
            resolved=resolved,
            personas=personas,
            stage_inputs=stage_inputs,
            news=news,
        )
        os.replace(temporary, destination)
        return load_generated_preflight_inputs(
            destination,
            resolved=resolved,
            personas=personas,
            stage_inputs=stage_inputs,
            news=news,
        )
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def load_generated_preflight_inputs(
    root: Path | str,
    *,
    resolved: ResolvedStudyManifest,
    personas: SealedPersonaSnapshot,
    stage_inputs: Any,
    news: Any,
) -> GeneratedPreflightInputs:
    """Load and bind all four artifacts back to their exact source inputs."""

    base = Path(root)
    if base.is_symlink() or not base.is_dir():
        raise PreflightInputError("generated preflight input root must be a real directory")
    paths = {
        "depth2": base / DEPTH2_FILENAME,
        "profiles": base / PUBLIC_PROFILE_FILENAME,
        "truth": base / TRUTH_POLICY_FILENAME,
        "manifest": base / GENERATION_MANIFEST_FILENAME,
    }
    values = {label: _read_json(path) for label, path in paths.items()}
    _validate_depth2_registry(
        values["depth2"], resolved=resolved, stage_inputs=stage_inputs, news=news
    )
    _validate_public_profile_registry(
        values["profiles"], resolved=resolved, personas=personas
    )
    _validate_truth_policy(values["truth"], resolved=resolved)
    _validate_generation_manifest(
        values["manifest"],
        resolved=resolved,
        personas=personas,
        stage_inputs=stage_inputs,
        news=news,
        depth2=values["depth2"],
        profiles=values["profiles"],
        truth=values["truth"],
        paths=paths,
    )
    return GeneratedPreflightInputs(
        root=base,
        depth2_registry_path=paths["depth2"],
        public_profile_registry_path=paths["profiles"],
        truth_policy_path=paths["truth"],
        manifest_path=paths["manifest"],
        depth2_registry=values["depth2"],
        public_profile_registry=values["profiles"],
        truth_policy=values["truth"],
        manifest=values["manifest"],
    )


def _build_depth2_registry(*, resolved: Any, stage_inputs: Any, news: Any) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for event in resolved.decision_events:
        event_id = str(event.decision_event_id)
        stage = stage_inputs.event(event_id)
        cutoff = _timestamp(stage.news_cutoff_timestamp, f"{event_id}.news_cutoff_timestamp")
        window_start = cutoff - timedelta(days=_LOOKBACK_DAYS)
        current_ids = tuple(slot.article_id for slot in news.slots_by_event[event_id])
        current_set = set(current_ids)
        candidates: list[tuple[datetime, str, str]] = []
        for article in news.articles.values():
            published = _timestamp(article.published_at, f"{article.article_id}.published_at")
            observed = _timestamp(article.observed_at, f"{article.article_id}.observed_at")
            if article.article_id in current_set:
                continue
            if published < window_start or published > cutoff or observed > cutoff:
                continue
            candidates.append((published, article.article_id, article.payload_sha256))
        candidates.sort(key=lambda item: (-item[0].timestamp(), item[1]))
        selected = candidates[:_SEARCH_TOP_K]
        rows.append(
            {
                "event_id": event_id,
                "cutoff_timestamp": cutoff.isoformat(),
                "window_start_timestamp": window_start.isoformat(),
                "excluded_current_event_article_ids": list(current_ids),
                "result_article_ids": [item[1] for item in selected],
                "result_payload_sha256s": [item[2] for item in selected],
            }
        )
    payload = {
        "artifact_type": "rn_depth2_recent_search_registry",
        "version": DEPTH2_REGISTRY_VERSION,
        "study_id": resolved.spec.study_id,
        "calendar_event_registry_sha256": resolved.calendar.canonical_sha256,
        "stage_input_registry_sha256": stage_inputs.canonical_sha256,
        "real_news_bundle_sha256": news.bundle_sha256,
        "lookback_days": _LOOKBACK_DAYS,
        "top_k": _SEARCH_TOP_K,
        "selection_policy": _SEARCH_SELECTION_POLICY,
        "events": rows,
    }
    payload["registry_sha256"] = canonical_sha256(payload)
    return payload


def _build_public_profile_registry(
    *, resolved: Any, personas: SealedPersonaSnapshot
) -> dict[str, Any]:
    active_ids = [
        member.agent_id for member in resolved.cohort.members if member.news_depth in {1, 2}
    ]
    profiles = [
        {"agent_id": agent_id, "profile": dict(_PUBLIC_PROFILE)}
        for agent_id in active_ids
    ]
    payload = {
        "artifact_type": "rn_public_author_profile_registry",
        "version": PUBLIC_PROFILE_REGISTRY_VERSION,
        "study_id": resolved.spec.study_id,
        "persona_prompt_map_sha256": personas.prompt_map_sha256,
        "active_agent_ids_sha256": canonical_sha256(active_ids),
        "generation_policy": _PROFILE_GENERATION_POLICY,
        "profiles": profiles,
    }
    payload["registry_sha256"] = canonical_sha256(payload)
    return payload


def _build_truth_policy(*, resolved: Any) -> dict[str, Any]:
    payload = {
        "artifact_type": "rn_community_post_truth_policy",
        "version": COMMUNITY_TRUTH_POLICY_VERSION,
        "study_id": resolved.spec.study_id,
        "public_post_is_unverified_author_claim": True,
        "claims_may_diverge_from_private_state": True,
        "canonical_fill_truth_source": "run_local_paper_fill_ledger_only",
        "reader_must_not_infer_canonical_position": True,
        "prohibited_reader_fields": list(_PROHIBITED_READER_FIELDS),
        "public_profile_field_allowlist": list(_PUBLIC_PROFILE_FIELDS),
    }
    payload["policy_sha256"] = canonical_sha256(payload)
    return payload


def _build_generation_manifest(
    *,
    resolved: Any,
    personas: SealedPersonaSnapshot,
    stage_inputs: Any,
    news: Any,
    depth2: Mapping[str, Any],
    profiles: Mapping[str, Any],
    truth: Mapping[str, Any],
) -> dict[str, Any]:
    payload = {
        "artifact_type": "rn_generated_preflight_input_manifest",
        "version": GENERATION_MANIFEST_VERSION,
        "study_id": resolved.spec.study_id,
        "resolved_study_manifest_sha256": resolved.sha256,
        "source_bindings": {
            "persona_snapshot_manifest_sha256": personas.manifest_sha256,
            "persona_prompt_map_sha256": personas.prompt_map_sha256,
            "calendar_event_registry_sha256": resolved.calendar.canonical_sha256,
            "stage_input_registry_sha256": stage_inputs.canonical_sha256,
            "real_news_bundle_sha256": news.bundle_sha256,
        },
        "generator_bindings": {
            "depth2": DEPTH2_REGISTRY_VERSION,
            "public_profiles": PUBLIC_PROFILE_REGISTRY_VERSION,
            "community_truth": COMMUNITY_TRUTH_POLICY_VERSION,
            "depth2_selection_policy": _SEARCH_SELECTION_POLICY,
            "public_profile_generation_policy": _PROFILE_GENERATION_POLICY,
        },
        "artifacts": {
            DEPTH2_FILENAME: str(depth2["registry_sha256"]),
            PUBLIC_PROFILE_FILENAME: str(profiles["registry_sha256"]),
            TRUTH_POLICY_FILENAME: str(truth["policy_sha256"]),
        },
        "human_approval_claimed": False,
        "network_requests": 0,
        "paid_api_calls": 0,
        "execution_authorized": False,
    }
    payload["manifest_sha256"] = canonical_sha256(payload)
    return payload


def _validate_depth2_registry(
    value: Any, *, resolved: Any, stage_inputs: Any, news: Any
) -> None:
    data = _exact(value, _DEPTH2_FIELDS, "Depth-2 registry")
    if (
        data["artifact_type"] != "rn_depth2_recent_search_registry"
        or data["version"] != DEPTH2_REGISTRY_VERSION
        or data["study_id"] != resolved.spec.study_id
        or data["calendar_event_registry_sha256"] != resolved.calendar.canonical_sha256
        or data["stage_input_registry_sha256"] != stage_inputs.canonical_sha256
        or data["real_news_bundle_sha256"] != news.bundle_sha256
        or data["lookback_days"] != _LOOKBACK_DAYS
        or data["top_k"] != _SEARCH_TOP_K
        or data["selection_policy"] != _SEARCH_SELECTION_POLICY
    ):
        raise PreflightInputError("Depth-2 registry source/policy binding is invalid")
    _require_self_hash(data, "registry_sha256", "Depth-2 registry")
    expected = _build_depth2_registry(resolved=resolved, stage_inputs=stage_inputs, news=news)
    if data != expected:
        raise PreflightInputError("Depth-2 registry is not the deterministic safe projection")
    events = data["events"]
    if not isinstance(events, list):
        raise PreflightInputError("Depth-2 registry events must be an ordered array")
    for index, row in enumerate(events):
        parsed = _exact(row, _DEPTH2_EVENT_FIELDS, f"Depth-2 event[{index}]")
        ids = parsed["result_article_ids"]
        hashes = parsed["result_payload_sha256s"]
        excluded = parsed["excluded_current_event_article_ids"]
        if not all(isinstance(item, str) and item for item in ids + hashes + excluded):
            raise PreflightInputError("Depth-2 registry contains an invalid article identity")
        if len(ids) != len(hashes) or len(ids) != len(set(ids)) or len(ids) > _SEARCH_TOP_K:
            raise PreflightInputError("Depth-2 registry result identity/hash arrays are invalid")
        if set(ids) & set(excluded):
            raise PreflightInputError("Depth-2 registry repeats a current-event article")


def _validate_public_profile_registry(
    value: Any, *, resolved: Any, personas: SealedPersonaSnapshot
) -> None:
    data = _exact(value, _PROFILE_FIELDS, "public profile registry")
    active_ids = [
        member.agent_id for member in resolved.cohort.members if member.news_depth in {1, 2}
    ]
    if (
        data["artifact_type"] != "rn_public_author_profile_registry"
        or data["version"] != PUBLIC_PROFILE_REGISTRY_VERSION
        or data["study_id"] != resolved.spec.study_id
        or data["persona_prompt_map_sha256"] != personas.prompt_map_sha256
        or data["active_agent_ids_sha256"] != canonical_sha256(active_ids)
        or data["generation_policy"] != _PROFILE_GENERATION_POLICY
    ):
        raise PreflightInputError("public profile registry source/policy binding is invalid")
    _require_self_hash(data, "registry_sha256", "public profile registry")
    rows = data["profiles"]
    if not isinstance(rows, list) or len(rows) != len(active_ids):
        raise PreflightInputError("public profile registry must exactly cover active D1/D2 agents")
    found: list[str] = []
    for index, row in enumerate(rows):
        parsed = _exact(row, _PROFILE_ROW_FIELDS, f"public profile[{index}]")
        agent_id = parsed["agent_id"]
        if not isinstance(agent_id, str):
            raise PreflightInputError("public profile agent_id must be a string")
        try:
            profile = PublicAuthorProfile.from_mapping(
                parsed["profile"], label=f"public profile[{index}].profile"
            )
        except CommunityContractError as exc:
            raise PreflightInputError(f"public profile[{index}] is unsafe: {exc}") from exc
        if profile.to_dict() != _PUBLIC_PROFILE:
            raise PreflightInputError("public profile differs from the uniform neutral policy")
        found.append(agent_id)
    if found != active_ids:
        raise PreflightInputError("public profiles differ from frozen active cohort order")


def _validate_truth_policy(value: Any, *, resolved: Any) -> None:
    data = _exact(value, _TRUTH_FIELDS, "community truth policy")
    _require_self_hash(data, "policy_sha256", "community truth policy")
    if data != _build_truth_policy(resolved=resolved):
        raise PreflightInputError("community truth policy differs from the code-owned contract")


def _validate_generation_manifest(
    value: Any,
    *,
    resolved: Any,
    personas: SealedPersonaSnapshot,
    stage_inputs: Any,
    news: Any,
    depth2: Mapping[str, Any],
    profiles: Mapping[str, Any],
    truth: Mapping[str, Any],
    paths: Mapping[str, Path],
) -> None:
    data = _exact(value, _ROOT_FIELDS, "generation manifest")
    _require_self_hash(data, "manifest_sha256", "generation manifest")
    expected = _build_generation_manifest(
        resolved=resolved,
        personas=personas,
        stage_inputs=stage_inputs,
        news=news,
        depth2=depth2,
        profiles=profiles,
        truth=truth,
    )
    if data != expected:
        raise PreflightInputError("generation manifest differs from source/artifact bindings")
    if (
        data["human_approval_claimed"] is not False
        or data["network_requests"] != 0
        or data["paid_api_calls"] != 0
        or data["execution_authorized"] is not False
    ):
        raise PreflightInputError("generated input manifest makes an unauthorized execution claim")
    artifact_hashes = data["artifacts"]
    for filename, path_key, expected_digest in (
        (DEPTH2_FILENAME, "depth2", depth2.get("registry_sha256")),
        (PUBLIC_PROFILE_FILENAME, "profiles", profiles.get("registry_sha256")),
        (TRUTH_POLICY_FILENAME, "truth", truth.get("policy_sha256")),
    ):
        if _sha256_file(paths[path_key]) == "":
            raise PreflightInputError(f"generated input artifact is unreadable: {filename}")
        if artifact_hashes.get(filename) != expected_digest:
            raise PreflightInputError(
                f"generation manifest has the wrong artifact digest for {filename}"
            )


def _exact(value: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PreflightInputError(f"{label} must be an object")
    if set(value) != fields:
        raise PreflightInputError(
            f"{label} has invalid exact schema; "
            f"missing={sorted(fields - set(value))} unknown={sorted(set(value) - fields)}"
        )
    return dict(value)


def _require_self_hash(data: Mapping[str, Any], field: str, label: str) -> None:
    digest = data.get(field)
    if not isinstance(digest, str) or len(digest) != 64:
        raise PreflightInputError(f"{label}.{field} must be a SHA-256 digest")
    content = {key: value for key, value in data.items() if key != field}
    if canonical_sha256(content) != digest:
        raise PreflightInputError(f"{label}.{field} does not match canonical content")


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise PreflightInputError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PreflightInputError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PreflightInputError(f"{label} must have an explicit timezone")
    return parsed


def _reject_constant(value: str) -> None:
    raise PreflightInputError(f"non-finite JSON constant is forbidden: {value}")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PreflightInputError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PreflightInputError(f"generated input is not a regular file: {path}")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightInputError(f"generated input JSON is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PreflightInputError(f"generated input root must be an object: {path}")
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("xb") as handle:
        handle.write(canonical_json_bytes(payload))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "COMMUNITY_TRUTH_POLICY_VERSION",
    "DEPTH2_FILENAME",
    "DEPTH2_REGISTRY_VERSION",
    "GENERATION_MANIFEST_FILENAME",
    "GENERATION_MANIFEST_VERSION",
    "GeneratedPreflightInputs",
    "PreflightInputError",
    "PUBLIC_PROFILE_FILENAME",
    "PUBLIC_PROFILE_REGISTRY_VERSION",
    "TRUTH_POLICY_FILENAME",
    "build_generated_preflight_inputs",
    "load_generated_preflight_inputs",
]
