"""Render human-readable RN preflight artifact indices from sealed JSON.

The Markdown files are deliberately projections of ``RUN_RECORD.json``.  No
operator can turn a failed gate into a GO by hand-editing a status sentence:
execution code reads the JSON record and validates its hashes separately.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from twinmarket_kr.rn_ab.spec import RN_CONDITIONS


class RNRunRecordError(ValueError):
    """The sealed preflight record cannot be rendered safely."""


def render_preflight_pair_record(record: Mapping[str, Any]) -> str:
    """Render the root pair-level entry point for a no-network preflight."""

    _validate_preflight_record(record)
    source = _mapping(record["source_hashes"], "source_hashes")
    prompts = _mapping(record["prompt_bundle"], "prompt_bundle")
    personas = _mapping(record["persona_snapshot"], "persona_snapshot")
    lines = [
        "# RN Community AB Run Record",
        "",
        "이 파일은 수동 일지가 아니라 `RUN_RECORD.json`에서 렌더한 preflight artifact index입니다.",
        "",
        "## 상태",
        "",
        "- 실행 상태: **PRE-FLIGHT COMPLETE / PAID EXECUTION NOT AUTHORIZED**",
        "- integrity 상태: sealed input·baseline HEAD·초기 DB base 검증 통과",
        "- network requests: 0",
        "- paid API calls: 0",
        "- validator/report: `not_generated_due_to_preflight_only_no_execution`",
        "",
        "## 고정 식별자",
        "",
        f"- run_id: `{record['run_id']}`",
        f"- baseline commit / checked-out HEAD: `{record['baseline_commit']}` / `{record['checked_out_baseline_commit']}`",
        f"- resolved manifest SHA-256: `{record['resolved_study_manifest_sha256']}`",
        f"- source snapshot: [`{source['path']}`]({source['path']}) (`{source['snapshot_sha256']}`)",
        f"- source tree SHA-256: `{source['source_tree_sha256']}`",
        f"- dependency-lock tree SHA-256: `{source['dependency_tree_sha256']}`",
        f"- prompt bundle: [`{prompts['path']}`]({prompts['path']}) (`{prompts['canonical_sha256']}`)",
        f"- persona snapshot: [`{personas['path']}`]({personas['path']}) (`{personas['snapshot_manifest_sha256']}`)",
        "",
        "## 실행 정책",
        "",
        "- 모델/공급자/reasoning-off policy는 sealed `study_spec.json`과 resolved manifest에서만 읽습니다.",
        "- RN strict request는 `reasoning: {\"effort\": \"none\", \"exclude\": true}`를 요청 직전에 강제합니다.",
        "- 수수료/매도세/fee amount는 RN trade policy의 0원 baseline으로 봉인됩니다.",
        "- live reasoning-off canary, D2 search registry, 승인된 community prompt/journal adapter가 아직 없으므로 이 record는 GO 승인이 아닙니다.",
        "",
        "## Arm base artifacts",
        "",
        "| Condition | Community | DB | Response journal | Initial portfolios | LTB₀ | Base digest |",
        "|---|---|---|---|---:|---:|---|",
    ]
    arms = _mapping(record["arms"], "arms")
    bases = _mapping(record["clean_base"], "clean_base")
    for condition_id in RN_CONDITIONS:
        arm = _mapping(arms[condition_id], condition_id)
        base = _mapping(bases[condition_id], f"clean_base.{condition_id}")
        lines.append(
            "| {condition} | {mode} | [{db}]({db}) | [{journal}]({journal}) | {portfolios} | {ltb0} | `{digest}` |".format(
                condition=condition_id,
                mode=arm["community_mode"],
                db=arm["db"],
                journal=arm["journal"],
                portfolios=base["initial_portfolios"],
                ltb0=base["ltb0"],
                digest=base["clean_base_scientific_sha256"],
            )
        )
    lines.extend(
        [
            "",
            "## Frozen inputs",
            "",
            f"- real-news bundle SHA-256: `{record['real_news_bundle_manifest_sha256']}`",
            f"- article-version leakage review: [`{record['article_version_leakage_review_manifest']}`]({record['article_version_leakage_review_manifest']}) (`{record['article_version_leakage_review_manifest_sha256']}`)",
            f"- known-injection closure SHA-256: `{record['known_injection_registry_sha256']}` (RN baseline fake count is 0)",
            "- runtime input byte hashes are in `RUN_RECORD.json > runtime_inputs`; execution reopens only those copies under `inputs/runtime/`.",
            "",
            "## 다음 단계",
            "",
            "이 preflight는 유료 실행을 시작하지 않았습니다. live canary 및 모든 P0 gate가 승인된 뒤에도 새 실행 factory가 이 sealed source/input state와 정확히 일치하는지 다시 확인해야 합니다.",
            "",
        ]
    )
    return "\n".join(lines)


def render_preflight_condition_record(record: Mapping[str, Any], *, condition_id: str) -> str:
    """Render an arm-local entry point that links back to the paired root."""

    _validate_preflight_record(record)
    if condition_id not in RN_CONDITIONS:
        raise RNRunRecordError("Unknown RN condition")
    arm = _mapping(_mapping(record["arms"], "arms")[condition_id], condition_id)
    base = _mapping(_mapping(record["clean_base"], "clean_base")[condition_id], condition_id)
    return "\n".join(
        [
            f"# {condition_id} Run Record",
            "",
            "Pair-level provenance and gate state: [`../RUN_RECORD.md`](../RUN_RECORD.md)",
            "",
            "- execution status: **PRE-FLIGHT COMPLETE / NOT AUTHORIZED**",
            f"- community mode: `{arm['community_mode']}`",
            f"- canonical local DB: [`{arm['db']}`](../{arm['db']})",
            f"- response journal: [`{arm['journal']}`](../{arm['journal']})",
            f"- initial portfolios / LTB₀: `{base['initial_portfolios']}` / `{base['ltb0']}`",
            f"- clean base digest: `{base['clean_base_scientific_sha256']}`",
            "- scientific-stage counts, reports, and live API telemetry are not generated in preflight.",
            "",
        ]
    )


def _validate_preflight_record(record: Mapping[str, Any]) -> None:
    if not isinstance(record, Mapping):
        raise RNRunRecordError("RUN_RECORD must be a mapping")
    if record.get("artifact_type") != "rn_ab_preflight_run_record":
        raise RNRunRecordError("RUN_RECORD is not an RN preflight record")
    if record.get("mode") != "preflight_only_no_network_no_paid_api":
        raise RNRunRecordError("RUN_RECORD mode is not preflight-only")
    if record.get("execution_authorized") is not False:
        raise RNRunRecordError("Preflight record may not authorize execution")
    for key in ("run_id", "baseline_commit", "checked_out_baseline_commit", "resolved_study_manifest_sha256"):
        if not isinstance(record.get(key), str) or not record[key]:
            raise RNRunRecordError(f"RUN_RECORD.{key} is required")
    arms = _mapping(record.get("arms"), "arms")
    bases = _mapping(record.get("clean_base"), "clean_base")
    if set(arms) != set(RN_CONDITIONS) or set(bases) != set(RN_CONDITIONS):
        raise RNRunRecordError("RUN_RECORD must contain exactly two RN arms and clean bases")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RNRunRecordError(f"RUN_RECORD.{label} must be a mapping")
    return value


__all__ = [
    "RNRunRecordError",
    "render_preflight_condition_record",
    "render_preflight_pair_record",
]
