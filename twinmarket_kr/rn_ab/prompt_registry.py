"""Versioned, hash-pinned common prompts kept beside the legacy prompt tree.

Legacy simulation modules load templates directly from ``prompts/``.  The
separate common bundle uses ordinary stage filenames in ``prompts/common/``
and a single payload sentinel per template.  That keeps prompt wording
reusable across news variants while the calling code owns input selection,
sealing, and audit details.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import string
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from twinmarket_kr.rn_ab.memory import scientific_sha256
from twinmarket_kr.rn_ab.prompt_contracts import BELIEF_DIMENSION_FIELDS


class RNPromptError(ValueError):
    """An RN prompt file or its render arguments violate the sealed contract."""


STB_STAGE = "stb"
ANALYSIS_STAGE = "analysis"
DECISION_STAGE = "decision"
LTB_STAGE = "post_fill_ltb"
INITIAL_BELIEF_STAGE = "initial_belief"
NEWS_PRE_SEARCH_STAGE = "news_pre_search"
NEWS_POST_SEARCH_STAGE = "news_post_search"
NEWS_INTERPRETATION_STAGE = "news_interpretation"
COMMUNITY_POSTING_STAGE = "community_posting"
COMMUNITY_READING_STAGE = "community_reading"
COMMUNITY_INTERPRETATION_STAGE = "community_interpretation"
# This marker is an authoring-time insertion point only; it is replaced before
# a model sees the template.  Keep it generic so prompt artifacts themselves
# are reusable across news variants and experiment conditions.
PAYLOAD_SENTINEL = "<<STAGE_PAYLOAD_JSON>>"
_DIMENSION_LIMIT_TOKEN = re.compile(r"\{(dim_[1-6]_limit)\}")
_ANY_DIMENSION_LIMIT_TOKEN = re.compile(r"\{(dim_[^{}]*_limit)\}")
_NAMED_TEMPLATE_TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SINGLE_LINE_BRACE_TOKEN = re.compile(r"\{([^{}\r\n]*)\}")
_SOURCE_INSERTION_TOKEN = re.compile(
    rf"{re.escape(PAYLOAD_SENTINEL)}|\{{([A-Za-z_][A-Za-z0-9_]*)\}}"
)
_COMMUNITY_READING_SELECT_HEADING = "### select 모드 (글 목록 보기)"
_COMMUNITY_READING_REACT_HEADING = "### react 모드 (글 읽고 반응)"

PROMPT_FILENAMES: dict[str, str] = {
    STB_STAGE: "update_short_term_belief.txt",
    ANALYSIS_STAGE: "market_analysis.txt",
    DECISION_STAGE: "make_decision.txt",
    LTB_STAGE: "update_long_term_belief.txt",
}

PROMPT_STAGE_ORDER: tuple[str, ...] = (
    STB_STAGE,
    ANALYSIS_STAGE,
    DECISION_STAGE,
    LTB_STAGE,
)

SUPPORT_PROMPT_FILENAMES: dict[str, str] = {
    INITIAL_BELIEF_STAGE: "initial_belief.txt",
    NEWS_PRE_SEARCH_STAGE: "news_agent_pre_search.txt",
    NEWS_POST_SEARCH_STAGE: "news_agent_post_search.txt",
    NEWS_INTERPRETATION_STAGE: "news_interpretation.txt",
    COMMUNITY_POSTING_STAGE: "posting_decision.txt",
    COMMUNITY_READING_STAGE: "community_reading.txt",
    COMMUNITY_INTERPRETATION_STAGE: "community_thinking.txt",
}

SUPPORT_PROMPT_STAGE_ORDER: tuple[str, ...] = tuple(SUPPORT_PROMPT_FILENAMES)

# Sealing every common prompt does not mean inventing extra LLM stages.  The
# RN design deliberately replaces the old initial/news helper calls with
# deterministic LTB0 and the current-only STB evidence stage.  Community
# support prompts remain actual conditional lifecycle calls.
SUPPORT_PROMPT_RUNTIME_USE: Mapping[str, str] = {
    INITIAL_BELIEF_STAGE: "sealed_compatibility_only_deterministic_ltb0_no_model_call",
    NEWS_PRE_SEARCH_STAGE: "sealed_compatibility_only_depth2_registry_no_model_call",
    NEWS_POST_SEARCH_STAGE: "sealed_compatibility_only_depth2_registry_no_model_call",
    NEWS_INTERPRETATION_STAGE: "sealed_compatibility_only_current_stb_owns_interpretation",
    COMMUNITY_POSTING_STAGE: "conditional_post_pm_journaled_call",
    COMMUNITY_READING_STAGE: "conditional_select_and_react_journaled_calls",
    COMMUNITY_INTERPRETATION_STAGE: "conditional_next_am_journaled_call",
}

ALL_PROMPT_FILENAMES: tuple[str, ...] = tuple(
    dict.fromkeys(
        (
            *(PROMPT_FILENAMES[stage] for stage in PROMPT_STAGE_ORDER),
            *(SUPPORT_PROMPT_FILENAMES[stage] for stage in SUPPORT_PROMPT_STAGE_ORDER),
        )
    )
)

# The 0720 market-analysis and decision prompts use these literal insertion
# slots.  Keep the prose intact and substitute only this fixed allowlist; do
# not use ``str.format`` over a model-facing template with JSON braces.
_LEGACY_STAGE_SLOTS: Mapping[str, tuple[str, ...]] = {
    ANALYSIS_STAGE: (
        "persona_prompt",
        "today_belief",
        "market_features",
        "portfolio_summary",
        "news_interpretation",
    ),
    DECISION_STAGE: (
        "persona_prompt",
        "today_belief",
        "market_analysis",
        "portfolio_summary",
        "order_history",
        "trading_constraints",
        "decision_space_instruction",
    ),
}

_SUPPORT_STAGE_SLOTS: Mapping[str, tuple[str, ...]] = {
    INITIAL_BELIEF_STAGE: (
        "persona_prompt",
        "dim_1_limit",
        "dim_2_limit",
        "dim_3_limit",
        "dim_4_limit",
        "dim_5_limit",
        "dim_6_limit",
    ),
    NEWS_PRE_SEARCH_STAGE: ("persona_prompt", "base_news_context"),
    NEWS_POST_SEARCH_STAGE: (
        "persona_prompt",
        "base_news_context",
        "search_results",
        "pre_search_thinking",
    ),
    NEWS_INTERPRETATION_STAGE: ("persona_prompt", "news_context"),
    COMMUNITY_POSTING_STAGE: (
        "persona_prompt",
        "ltb_dimensions",
        "view_change",
        "committed_pm_fill",
        "date",
        "post_types_guide",
    ),
    COMMUNITY_READING_STAGE: (
        "persona_prompt",
        "mode",
        "post_list_str",
        "read_limit",
        "posts_content_str",
    ),
    COMMUNITY_INTERPRETATION_STAGE: (
        "persona_prompt",
        "candidate_posts_summary",
        "best_posts_summary",
        "posts_read_summary",
    ),
}


def canonical_prompt_payload(value: Mapping[str, Any], *, label: str = "RN prompt payload") -> str:
    """Serialize one already-validated stage payload without formatting tricks."""
    if not isinstance(value, Mapping) or not value:
        raise RNPromptError(f"{label} must be a non-empty object")
    try:
        rendered = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RNPromptError(f"{label} must be canonical JSON serializable") from exc
    return rendered


@dataclass(frozen=True)
class RNPromptTemplate:
    """One UTF-8 template with exactly one canonical JSON insertion point."""

    stage: str
    filename: str
    text: str
    sha256: str

    def render(self, payload: Mapping[str, Any]) -> str:
        template_text = _render_belief_limit_tokens(
            self.text,
            stage=self.stage,
            payload=payload,
        )
        _validate_renderable_template_text(template_text, stage=self.stage)
        rendered = _render_source_insertions(
            template_text,
            stage=self.stage,
            payload=payload,
        )
        if not rendered.strip():
            raise RNPromptError(f"RN {self.stage} prompt did not render its payload exactly once")
        return rendered


@dataclass(frozen=True)
class RNSupportPromptTemplate:
    """One reviewed 0720 support prompt with a fixed named-slot allowlist."""

    stage: str
    filename: str
    text: str
    sha256: str

    def render(self, values: Mapping[str, Any]) -> str:
        if not isinstance(values, Mapping):
            raise RNPromptError(f"RN {self.stage} support prompt values must be an object")
        expected = set(_SUPPORT_STAGE_SLOTS[self.stage])
        if set(values) != expected:
            raise RNPromptError(
                f"RN {self.stage} support prompt values have invalid fields; "
                f"missing={sorted(expected - set(values))} unknown={sorted(set(values) - expected)}"
            )
        rendered = _render_support_slots(self.text, stage=self.stage, values=values)
        if self.stage == COMMUNITY_READING_STAGE:
            rendered = _render_active_community_reading_mode(
                rendered,
                mode=values["mode"],
            )
        if not rendered.strip():
            raise RNPromptError(f"RN {self.stage} support prompt rendered empty")
        return rendered


@dataclass(frozen=True)
class RNPromptBundle:
    """The exact STB/analysis/decision/LTB prompt set consumed by the adapter.

    ``prompt_dir`` is explicit.  No structured-pipeline code path falls back to global
    ``config.PROMPT_DIR``; the caller is responsible for passing a run-local
    sealed copy once a preflight bundle has been created.
    """

    prompt_dir: Path
    templates: Mapping[str, RNPromptTemplate]
    support_templates: Mapping[str, RNSupportPromptTemplate]
    canonical_sha256: str

    @classmethod
    def load(cls, *, prompt_dir: Path | str) -> "RNPromptBundle":
        root = Path(prompt_dir)
        if not root.exists() or not root.is_dir() or root.is_symlink():
            raise RNPromptError(f"RN prompt directory must be a real directory: {root}")
        templates: dict[str, RNPromptTemplate] = {}
        for stage, filename in PROMPT_FILENAMES.items():
            path = root / filename
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raise RNPromptError(f"Cannot read RN {stage} prompt: {path}") from exc
            if path.is_symlink():
                raise RNPromptError(f"RN {stage} prompt may not be a symbolic link: {path}")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RNPromptError(f"RN {stage} prompt is not UTF-8: {path}") from exc
            if text.count(PAYLOAD_SENTINEL) != 1:
                raise RNPromptError(
                    f"RN {stage} prompt must contain exactly one {PAYLOAD_SENTINEL} sentinel"
                )
            _validate_belief_limit_tokens(text, stage=stage)
            _validate_legacy_stage_slots(text, stage=stage)
            _validate_template_brace_tokens(text, stage=stage)
            templates[stage] = RNPromptTemplate(
                stage=stage,
                filename=filename,
                text=text,
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        support_templates: dict[str, RNSupportPromptTemplate] = {}
        for stage, filename in SUPPORT_PROMPT_FILENAMES.items():
            path = root / filename
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raise RNPromptError(f"Cannot read RN {stage} support prompt: {path}") from exc
            if path.is_symlink():
                raise RNPromptError(f"RN {stage} support prompt may not be a symbolic link: {path}")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RNPromptError(f"RN {stage} support prompt is not UTF-8: {path}") from exc
            _validate_support_template(text, stage=stage)
            support_templates[stage] = RNSupportPromptTemplate(
                stage=stage,
                filename=filename,
                text=text,
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        canonical = scientific_sha256(
            {
                "artifact_type": "rn_prompt_bundle",
                "version": "rn-prompt-bundle-v2",
                "core_templates": [
                    {
                        "stage": stage,
                        "filename": templates[stage].filename,
                        "sha256": templates[stage].sha256,
                    }
                    for stage in PROMPT_STAGE_ORDER
                ],
                "support_templates": [
                    {
                        "stage": stage,
                        "filename": support_templates[stage].filename,
                        "sha256": support_templates[stage].sha256,
                        "runtime_use": SUPPORT_PROMPT_RUNTIME_USE[stage],
                    }
                    for stage in SUPPORT_PROMPT_STAGE_ORDER
                ],
            }
        )
        return cls(
            prompt_dir=root,
            templates=templates,
            support_templates=support_templates,
            canonical_sha256=canonical,
        )

    def template(self, stage: str) -> RNPromptTemplate:
        try:
            return self.templates[stage]
        except KeyError as exc:
            raise RNPromptError(f"Unknown RN prompt stage: {stage!r}") from exc

    def render(self, stage: str, payload: Mapping[str, Any]) -> str:
        return self.template(stage).render(payload)

    def support_template(self, stage: str) -> RNSupportPromptTemplate:
        try:
            return self.support_templates[stage]
        except KeyError as exc:
            raise RNPromptError(f"Unknown RN support prompt stage: {stage!r}") from exc

    def render_support(self, stage: str, values: Mapping[str, Any]) -> str:
        return self.support_template(stage).render(values)

    def manifest(self) -> dict[str, Any]:
        return {
            "artifact_type": "rn_prompt_bundle",
            "version": "rn-prompt-bundle-v2",
            "canonical_sha256": self.canonical_sha256,
            "templates": [
                {
                    "stage": stage,
                    "filename": self.template(stage).filename,
                    "sha256": self.template(stage).sha256,
                }
                for stage in PROMPT_STAGE_ORDER
            ],
            "support_templates": [
                {
                    "stage": stage,
                    "filename": self.support_template(stage).filename,
                    "sha256": self.support_template(stage).sha256,
                    "runtime_use": SUPPORT_PROMPT_RUNTIME_USE[stage],
                }
                for stage in SUPPORT_PROMPT_STAGE_ORDER
            ],
        }

    def seal_into(self, destination: Path | str) -> "RNPromptBundle":
        """Copy the verified templates into a fresh run-local prompt directory.

        A paper stage never reopens the editable repository ``prompts/`` tree.
        Re-loading immediately before copy closes a basic time-of-check/time-of-
        use gap, and the destination is verified again before it becomes visible.
        """
        target = Path(destination)
        if target.exists() or target.is_symlink():
            raise RNPromptError(f"RN prompt sealing destination already exists: {target}")
        if not target.parent.exists() or not target.parent.is_dir():
            raise RNPromptError("RN prompt sealing destination parent must already exist")
        fresh = RNPromptBundle.load(prompt_dir=self.prompt_dir)
        if fresh.canonical_sha256 != self.canonical_sha256:
            raise RNPromptError("RN prompt source changed before it could be sealed")
        temporary = Path(tempfile.mkdtemp(prefix=".rn-prompt-seal-", dir=target.parent))
        try:
            for stage, filename in PROMPT_FILENAMES.items():
                source = self.prompt_dir / filename
                shutil.copyfile(source, temporary / filename)
                if hashlib.sha256((temporary / filename).read_bytes()).hexdigest() != self.template(stage).sha256:
                    raise RNPromptError(f"RN {stage} prompt changed during sealing")
            for stage, filename in SUPPORT_PROMPT_FILENAMES.items():
                source = self.prompt_dir / filename
                shutil.copyfile(source, temporary / filename)
                if (
                    hashlib.sha256((temporary / filename).read_bytes()).hexdigest()
                    != self.support_template(stage).sha256
                ):
                    raise RNPromptError(f"RN {stage} support prompt changed during sealing")
            manifest_path = temporary / "prompt_bundle_manifest.json"
            with manifest_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    json.dumps(
                        self.manifest(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
                    )
                )
                handle.write("\n")
            sealed = RNPromptBundle.load(prompt_dir=temporary)
            if sealed.canonical_sha256 != self.canonical_sha256:
                raise RNPromptError("RN run-local prompt bundle hash differs after sealing")
            os.replace(temporary, target)
            return RNPromptBundle.load(prompt_dir=target)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise


def _validate_belief_limit_tokens(text: str, *, stage: str) -> None:
    """Check literal legacy limit tokens without applying Python formatting.

    Using ``str.format`` would make JSON-bearing templates fragile and would
    give input payloads unintended formatting power. The six explicit limit
    tokens are therefore the only variables this registry ever substitutes.
    """

    tokens = _ANY_DIMENSION_LIMIT_TOKEN.findall(text)
    if stage in {STB_STAGE, LTB_STAGE}:
        expected = [f"{field}_limit" for field in BELIEF_DIMENSION_FIELDS]
        if sorted(tokens) != sorted(expected) or text.count("{dim_") != len(expected):
            raise RNPromptError(
                f"RN {stage} prompt must contain each literal belief limit token exactly once"
            )
    elif tokens or "{dim_" in text:
        raise RNPromptError(f"RN {stage} prompt may not contain belief limit tokens")


def _validate_legacy_stage_slots(text: str, *, stage: str) -> None:
    """Allow exactly the old named slots required by one reusable stage."""

    expected = _LEGACY_STAGE_SLOTS.get(stage, ())
    named = _NAMED_TEMPLATE_TOKEN.findall(text)
    dimension_tokens = {f"{field}_limit" for field in BELIEF_DIMENSION_FIELDS}
    observed = [token for token in named if token not in dimension_tokens]
    if sorted(observed) != sorted(expected):
        raise RNPromptError(
            f"RN {stage} prompt must contain exactly its approved legacy template slots"
        )


def _validate_template_brace_tokens(text: str, *, stage: str) -> None:
    """Reject malformed authoring tokens without interpreting JSON braces.

    Prompt JSON examples place a quote or newline immediately after ``{``;
    template tokens start with an identifier.  Inspect only the latter at load
    time, before any untrusted runtime value has been inserted.
    """

    if "{{" in text or "}}" in text:
        raise RNPromptError(f"RN {stage} prompt may not contain doubled template braces")
    approved = set(_LEGACY_STAGE_SLOTS.get(stage, ()))
    if stage in {STB_STAGE, LTB_STAGE}:
        approved.update(f"{field}_limit" for field in BELIEF_DIMENSION_FIELDS)
    for match in _SINGLE_LINE_BRACE_TOKEN.finditer(text):
        token = match.group(1)
        if token and re.match(r"[A-Za-z_]", token) and token not in approved:
            raise RNPromptError(
                f"RN {stage} prompt contains an unapproved or malformed template token: {{{token}}}"
            )


def _validate_renderable_template_text(text: str, *, stage: str) -> None:
    """Validate only source-template state before runtime interpolation."""

    if text.count(PAYLOAD_SENTINEL) != 1:
        raise RNPromptError(f"RN {stage} prompt must retain exactly one payload sentinel before render")
    _validate_legacy_stage_slots(text, stage=stage)
    _validate_template_brace_tokens(text, stage=stage)
    if "{dim_" in text:
        raise RNPromptError(f"RN {stage} prompt has an unresolved belief limit token")


def _render_source_insertions(
    text: str,
    *,
    stage: str,
    payload: Mapping[str, Any],
) -> str:
    """Interpolate every source token in one pass without rescanning values."""

    expected = _LEGACY_STAGE_SLOTS.get(stage, ())
    values = _legacy_stage_slot_values(stage=stage, payload=payload) if expected else {}
    if set(values) != set(expected):  # defensive: code and template allowlists must move together.
        raise RNPromptError(f"RN {stage} legacy template-slot map is incomplete")
    serialized = canonical_prompt_payload(payload, label=f"RN {stage} payload")
    seen_sentinel = 0
    seen_slots: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        nonlocal seen_sentinel
        token = match.group(0)
        if token == PAYLOAD_SENTINEL:
            seen_sentinel += 1
            return serialized
        slot = match.group(1)
        if slot not in values:
            raise RNPromptError(f"RN {stage} prompt has an unresolved template token: {token}")
        seen_slots.add(slot)
        return values[slot]

    rendered = _SOURCE_INSERTION_TOKEN.sub(replace, text)
    if seen_sentinel != 1 or seen_slots != set(expected):
        raise RNPromptError(f"RN {stage} prompt did not render its source tokens exactly once")
    return rendered


def _legacy_stage_slot_values(*, stage: str, payload: Mapping[str, Any]) -> dict[str, str]:
    """Build only the explicitly approved 0720 prompt insertions from sealed payloads."""

    persona = payload.get("persona")
    if not isinstance(persona, Mapping):
        raise RNPromptError(f"RN {stage} payload.persona must be an object")
    persona_text = persona.get("persona_text")
    if not isinstance(persona_text, str) or not persona_text.strip():
        raise RNPromptError(f"RN {stage} payload.persona.persona_text must be a non-empty string")
    previous_ltb = _required_payload_fragment(payload, "previous_ltb", stage=stage)
    current_stb = _required_payload_fragment(payload, "current_stb", stage=stage)
    belief = _canonical_fragment(
        {"previous_ltb": previous_ltb, "current_stb": current_stb},
        label=f"RN {stage} today_belief",
    )
    if stage == ANALYSIS_STAGE:
        return {
            "persona_prompt": persona_text,
            "today_belief": belief,
            "market_features": _canonical_fragment(
                _required_payload_fragment(payload, "market", stage=stage),
                label="RN analysis market_features",
            ),
            "portfolio_summary": _canonical_fragment(
                _required_payload_fragment(payload, "execution_state", stage=stage),
                label="RN analysis portfolio_summary",
            ),
            "news_interpretation": _canonical_fragment(
                current_stb,
                label="RN analysis news_interpretation",
            ),
        }
    if stage == DECISION_STAGE:
        execution_state = _required_payload_fragment(payload, "execution_state", stage=stage)
        if not isinstance(execution_state, Mapping):
            raise RNPromptError("RN decision payload.execution_state must be an object")
        allowed_actions = execution_state.get("allowed_actions")
        if not isinstance(allowed_actions, list) or not allowed_actions:
            raise RNPromptError("RN decision payload.execution_state.allowed_actions must be a non-empty list")
        return {
            "persona_prompt": persona_text,
            "today_belief": belief,
            "market_analysis": _canonical_fragment(
                _required_payload_fragment(payload, "analysis", stage=stage),
                label="RN decision market_analysis",
            ),
            "portfolio_summary": _canonical_fragment(
                execution_state,
                label="RN decision portfolio_summary",
            ),
            "order_history": _canonical_fragment(
                _required_payload_fragment(payload, "order_history", stage=stage),
                label="RN decision order_history",
            ),
            "trading_constraints": _canonical_fragment(
                execution_state,
                label="RN decision trading_constraints",
            ),
            "decision_space_instruction": _canonical_fragment(
                {"allowed_actions": allowed_actions},
                label="RN decision decision_space_instruction",
            ),
        }
    raise RNPromptError(f"RN {stage} has no legacy template-slot map")


def _required_payload_fragment(payload: Mapping[str, Any], field: str, *, stage: str) -> Any:
    if field not in payload or payload[field] is None:
        raise RNPromptError(f"RN {stage} payload.{field} is required for its legacy prompt slot")
    return payload[field]


def _canonical_fragment(value: Any, *, label: str) -> str:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RNPromptError(f"{label} must be canonical JSON serializable") from exc
    return rendered


def _validate_support_template(text: str, *, stage: str) -> None:
    """Validate legacy ``str.format`` syntax without ever calling it.

    The seven support prompts intentionally remain byte-identical to the 0720
    sources.  Their JSON examples therefore contain doubled literal braces.
    ``Formatter.parse`` is used only as a parser at load time; runtime values
    are inserted by :func:`_render_support_slots` in one source-only pass.
    """

    expected = tuple(_SUPPORT_STAGE_SLOTS[stage])
    observed: list[str] = []
    try:
        parsed = tuple(string.Formatter().parse(text))
    except ValueError as exc:
        raise RNPromptError(f"RN {stage} support prompt has malformed braces") from exc
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name not in expected or format_spec or conversion:
            raise RNPromptError(
                f"RN {stage} support prompt contains an unapproved template field: {field_name!r}"
            )
        observed.append(field_name)
    if sorted(observed) != sorted(expected) or len(observed) != len(expected):
        raise RNPromptError(
            f"RN {stage} support prompt must contain each approved field exactly once"
        )
    if stage == COMMUNITY_READING_STAGE:
        _validate_community_reading_template(text)


def _validate_community_reading_template(text: str) -> None:
    """Require two unambiguous branches with literal, parseable examples.

    The legacy prompt contains both select and react guidance in one source
    file.  At runtime only the active branch is exposed to the model; this
    load-time check makes accidental reintroduction of an ellipsis/union-type
    pseudo-JSON example a sealed-prompt failure rather than an API-time pause.
    """

    if text.count(_COMMUNITY_READING_SELECT_HEADING) != 1 or text.count(
        _COMMUNITY_READING_REACT_HEADING
    ) != 1:
        raise RNPromptError("RN community_reading prompt must contain one select and one react branch")
    if text.index(_COMMUNITY_READING_SELECT_HEADING) >= text.index(
        _COMMUNITY_READING_REACT_HEADING
    ):
        raise RNPromptError("RN community_reading prompt branches are out of order")
    if "..." in text or '"like"|"unlike"|"none"' in text:
        raise RNPromptError("RN community_reading prompt must not contain pseudo-JSON output examples")
    if '"selected_post_ids": []' not in text or '"reactions": []' not in text:
        raise RNPromptError("RN community_reading prompt must contain parseable empty-array JSON examples")


def _support_value(value: Any, *, label: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None:
        raise RNPromptError(f"{label} must not be boolean/null")
    if isinstance(value, int):
        return str(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RNPromptError(f"{label} must be text, integer, or canonical JSON") from exc


def _render_support_slots(
    text: str,
    *,
    stage: str,
    values: Mapping[str, Any],
) -> str:
    """Render source fields and literal braces once; never rescan input text."""

    _validate_support_template(text, stage=stage)
    normalized = {
        key: _support_value(value, label=f"RN {stage} support value {key}")
        for key, value in values.items()
    }
    seen: list[str] = []
    output: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        if text.startswith("{{", index):
            output.append("{")
            index += 2
            continue
        if text.startswith("}}", index):
            output.append("}")
            index += 2
            continue
        if text[index] == "{":
            close = text.find("}", index + 1)
            if close < 0:
                raise RNPromptError(f"RN {stage} support prompt has an unclosed field")
            field_name = text[index + 1 : close]
            if field_name not in normalized:
                raise RNPromptError(
                    f"RN {stage} support prompt contains an unresolved field: {{{field_name}}}"
                )
            output.append(normalized[field_name])
            seen.append(field_name)
            index = close + 1
            continue
        if text[index] == "}":
            raise RNPromptError(f"RN {stage} support prompt has an unmatched closing brace")
        output.append(text[index])
        index += 1
    expected = tuple(_SUPPORT_STAGE_SLOTS[stage])
    if sorted(seen) != sorted(expected) or len(seen) != len(expected):
        raise RNPromptError(f"RN {stage} support prompt did not render each field exactly once")
    return "".join(output)


def _render_active_community_reading_mode(text: str, *, mode: Any) -> str:
    """Expose exactly one output schema for a select/react model call.

    Both instructions remain together in the reviewed legacy-derived source
    file, but showing both schemas to one strict JSON call creates an
    avoidable parser ambiguity.  The source slots are rendered before this
    projection, so the sealed template hash and request-value hash still bind
    the exact original artifact and full typed input map.
    """

    if not isinstance(mode, str) or mode not in {"select", "react"}:
        raise RNPromptError("RN community_reading mode must be exactly select or react")
    select_start = text.find(_COMMUNITY_READING_SELECT_HEADING)
    react_start = text.find(_COMMUNITY_READING_REACT_HEADING)
    if select_start < 0 or react_start < 0 or select_start >= react_start:
        raise RNPromptError("RN community_reading prompt branches are malformed after render")
    prefix = text[:select_start].rstrip()
    active = (
        text[select_start:react_start].strip()
        if mode == "select"
        else text[react_start:].strip()
    )
    if not active:
        raise RNPromptError("RN community_reading active branch rendered empty")
    return f"{prefix}\n\n{active}\n"


def _render_belief_limit_tokens(
    text: str,
    *,
    stage: str,
    payload: Mapping[str, Any],
) -> str:
    """Safely replace the six legacy ``{dim_n_limit}`` tokens before JSON insertion."""

    _validate_belief_limit_tokens(text, stage=stage)
    if stage not in {STB_STAGE, LTB_STAGE}:
        return text
    output_contract = payload.get("output_contract")
    if not isinstance(output_contract, Mapping):
        raise RNPromptError(f"RN {stage} payload must contain an output_contract object")
    dimension_contract = output_contract.get("dimension_contract")
    if not isinstance(dimension_contract, list):
        raise RNPromptError(f"RN {stage} output_contract.dimension_contract must be a list")
    limits: dict[str, int] = {}
    for item in dimension_contract:
        if not isinstance(item, Mapping):
            raise RNPromptError(f"RN {stage} dimension contract entry must be an object")
        field = item.get("field")
        limit = item.get("character_limit")
        if (
            not isinstance(field, str)
            or field not in BELIEF_DIMENSION_FIELDS
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or field in limits
        ):
            raise RNPromptError(f"RN {stage} dimension contract has an invalid field or character limit")
        limits[field] = limit
    if set(limits) != set(BELIEF_DIMENSION_FIELDS):
        raise RNPromptError(f"RN {stage} output_contract must provide all six belief limits")
    rendered = text
    for field in BELIEF_DIMENSION_FIELDS:
        rendered = rendered.replace("{" + field + "_limit}", str(limits[field]))
    if "{dim_" in rendered:  # defensive: do not silently ship a malformed prompt.
        raise RNPromptError(f"RN {stage} prompt has an unresolved belief limit token")
    return rendered
