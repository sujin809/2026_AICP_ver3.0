from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

import config
from twinmarket_kr.belief_projection import (
    BELIEF_DIMENSION_KEYS,
    render_belief_summary,
)
from twinmarket_kr.llm.call_policy import (
    INTEGRATED_STAGE_MAX_TOKENS_V1,
    INTEGRATED_STAGE_SCHEMA_VERSIONS_V1,
)
from twinmarket_kr.llm.client import OpenRouterClient, response_content, stable_llm_seed
from twinmarket_kr.llm.response_journal import (
    ResponseJournalError,
    open_journal_call,
    parse_strict_json_object,
)
from twinmarket_kr.outcome_schedule import (
    OutcomeScheduleError,
    outcome_evidence_relation,
)
from twinmarket_kr.llm.validation import (
    LLMValidationError,
    build_validation_retry_prompt,
    record_validation_failure,
)


BELIEF_EVIDENCE_RELATIONS = ("support", "contradict")

# STB는 이전 belief·과거 판단·체결·수익률·가격 성과를 입력받지 않는다. 따라서
# STB dim_6은 누적 자기평가가 아니라 "오늘 정보의 한계와 오늘 판단의 주의점"만
# 담아야 한다. 금칙어 목록은 의미를 검증하지 못하고 정상 문장을 과도하게
# 막으므로, 두 구획 표시가 모두 있는지만 구조적으로 확인한다. 표시 뒤의
# 자연어는 제한하지 않는다. 누적 자기평가는 실제 fill과 도래한 outcome을
# 입력받는 LTB dim_6의 역할이므로 LTB에는 이 검사를 적용하지 않는다.
STB_DIM_6_REQUIRED_MARKERS = ("정보 한계:", "주의점:")


class BeliefValidationError(LLMValidationError):
    pass


def validate_stb_dim_6_scope(dimensions: Mapping[str, str]) -> list[str]:
    """Require the STB dim_6 information-limit form instead of past-performance recall."""

    text = str(dimensions.get("dim_6") or "")
    missing = [marker for marker in STB_DIM_6_REQUIRED_MARKERS if marker not in text]
    if missing:
        return [f"dim_6:requires_markers:{missing}"]
    return []


def load_prompt(name: str) -> str:
    return (config.PROMPT_DIR / name).read_text(encoding="utf-8")


def render_prompt(name: str, /, **values: Any) -> str:
    """Render named prompt slots without interpreting JSON braces.

    The production prompts contain literal JSON examples.  ``str.format``
    treats those braces as replacement fields and made the old and strict
    prompt paths incompatible.  Production rendering now replaces only the
    explicitly named slots supplied by the caller.
    """

    prompt = load_prompt(name)
    for key, value in values.items():
        prompt = prompt.replace("{" + key + "}", str(value))
    return prompt


def _limits() -> dict[str, int]:
    return {f"{key}_limit": value for key, value in config.BELIEF_LIMITS.items()}


def belief_dimensions(value: Mapping[str, Any], *, label: str = "belief") -> dict[str, str]:
    # 글자수 한도는 프롬프트에 목표치로만 남기고 검증에서는 강제하지 않는다.
    # 한 턴에 outcome 3건이 동시에 도래하면 150자 안에 빠짐없이 요약하기가
    # 어려워 라이브에서 반복적으로 10회 소진되는 이벤트를 만들었다(2026-03-09
    # 부근에서 여러 agent가 이 이유만으로 실행을 여러 차례 중단시켰다). 길이
    # 초과는 내용의 신뢰도를 해치지 않으므로 재시도 없이 그대로 받아들인다.
    dimensions: dict[str, str] = {}
    for key in BELIEF_DIMENSION_KEYS:
        raw = value.get(key)
        if not isinstance(raw, str) or not raw.strip():
            raise BeliefValidationError(f"{label}.{key} must be a non-empty string")
        dimensions[key] = raw.strip()
    return dimensions


def _parse_json_mapping(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        value = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _normalize_dimension_evidence(
    value: Any,
    *,
    field: str,
    allowed_ids_by_dimension: Mapping[str, set[str]],
) -> tuple[dict[str, dict[str, list[str]]], list[str]]:
    errors: list[str] = []
    if not isinstance(value, Mapping):
        return {}, [f"{field}:requires_object"]
    missing = set(BELIEF_DIMENSION_KEYS) - set(value)
    unknown = set(value) - set(BELIEF_DIMENSION_KEYS)
    if missing:
        errors.append(f"{field}:missing_dimensions:{sorted(missing)}")
    if unknown:
        errors.append(f"{field}:unknown_dimensions:{sorted(unknown)}")
    normalized: dict[str, dict[str, list[str]]] = {}
    for dimension in BELIEF_DIMENSION_KEYS:
        relations = value.get(dimension)
        if not isinstance(relations, Mapping):
            errors.append(f"{field}.{dimension}:requires_object")
            continue
        if set(relations) != set(BELIEF_EVIDENCE_RELATIONS):
            errors.append(
                f"{field}.{dimension}:requires_exact_support_contradict"
            )
            continue
        normalized[dimension] = {}
        for relation in BELIEF_EVIDENCE_RELATIONS:
            rows = relations.get(relation)
            if not isinstance(rows, list) or any(
                not isinstance(item, str) or not item.strip() for item in rows
            ):
                errors.append(
                    f"{field}.{dimension}.{relation}:requires_string_array"
                )
                continue
            ids = [item.strip() for item in rows]
            if len(ids) != len(set(ids)):
                errors.append(f"{field}.{dimension}.{relation}:duplicate_ids")
            allowed_pool = set(allowed_ids_by_dimension.get(dimension, set()))
            unknown_ids = set(ids) - allowed_pool
            if unknown_ids:
                # 라이브에서 뉴스 ID의 카테고리 접두어(예: news_..._섹터_<hash>를
                # news_..._종목_<hash>로)만 착각해 소진되는 사례가 반복됐다.
                # 같은 해시 접미어를 가진 실제 ID가 registry에 있으면 그대로
                # 알려줘 모델이 스스로 대조하지 않아도 되게 한다.
                suggestions = {
                    bad_id: sorted(
                        candidate
                        for candidate in allowed_pool
                        if candidate != bad_id
                        and candidate.rsplit("_", 1)[-1]
                        == bad_id.rsplit("_", 1)[-1]
                    )
                    for bad_id in unknown_ids
                }
                suggestions = {k: v for k, v in suggestions.items() if v}
                hint = f":did_you_mean:{suggestions}" if suggestions else ""
                # 접두어 착각이 아니라 아예 없는 ID를 지어낸 경우(예: 존재하지
                # 않는 turn 번호)에는 근접 후보가 없어 위 힌트가 비어 있다.
                # 허용 목록이 짧으면 그대로 나열해 무엇을 골라야 하는지
                # 모호함 없이 알려준다. 라이브에서 이 유형이 소진까지 간 적이
                # 있고, 그때 오류는 "없는 ID"라고만 말하고 있는 ID는 알려주지
                # 않았다.
                if not suggestions and 0 < len(allowed_pool) <= 12:
                    hint = f":allowed={sorted(allowed_pool)}"
                errors.append(
                    f"{field}.{dimension}.{relation}:unknown_ids:"
                    f"{sorted(unknown_ids)}{hint}"
                )
            normalized[dimension][relation] = ids
        support = set(normalized[dimension].get("support", []))
        contradict = set(normalized[dimension].get("contradict", []))
        overlapping = support & contradict
        if overlapping:
            # 위반한 ID를 명시하지 않으면 모델이 support/contradict 목록을 스스로
            # 대조해 겹치는 항목을 찾아야 한다. 라이브에서 그 추론이 실패하면서
            # 지적받은 차원은 고치고 다른 차원을 새로 깨뜨리는 재시도 루프가
            # 반복됐다. 어느 ID를 어디서 빼야 하는지 그대로 알려준다.
            errors.append(
                f"{field}.{dimension}:same_id_in_both_relations:"
                f"{sorted(overlapping)}"
            )
    return normalized, errors


async def _generate_hierarchical_belief(
    *,
    prompt_name: str,
    prompt_payload: Mapping[str, Any],
    evidence_field: str,
    allowed_ids_by_dimension: Mapping[str, set[str]],
    audit_label: str,
    client: OpenRouterClient,
    seed: int | None,
    validation_attempts: int,
    previous_dimensions: Mapping[str, str] | None = None,
    required_dim_6_outcome_ids: set[str] | None = None,
    fixed_relations_by_dimension: Mapping[
        str,
        Mapping[str, set[str]],
    ]
    | None = None,
    dimension_text_validator: Callable[[Mapping[str, str]], list[str]] | None = None,
) -> dict[str, Any]:
    if validation_attempts < 1:
        raise ValueError("validation_attempts must be at least 1")
    prompt = render_prompt(prompt_name, **_limits()).replace(
        "<<STAGE_PAYLOAD_JSON>>",
        json.dumps(prompt_payload, ensure_ascii=False, indent=2),
    )
    required_keys = {*BELIEF_DIMENSION_KEYS, evidence_field}
    invalid_history: list[list[str]] = []
    current_prompt = prompt
    temperatures = [
        0.2 if attempt == 1 else 0.1
        for attempt in range(1, validation_attempts + 1)
    ]
    seeds = [
        stable_llm_seed(seed or 0, audit_label, attempt)
        for attempt in range(1, validation_attempts + 1)
    ]
    max_tokens = int(INTEGRATED_STAGE_MAX_TOKENS_V1[audit_label])
    journal_call = open_journal_call(
        stage=audit_label,
        schema_version=INTEGRATED_STAGE_SCHEMA_VERSIONS_V1[audit_label],
        base_prompt=prompt,
        client=client,
        temperature_schedule=temperatures,
        seed_schedule=seeds,
        max_tokens=max_tokens,
        validation_attempts=validation_attempts,
        validation_procedure_version=f"{audit_label}-validator-v4",
        response_format={"type": "json_object"},
        semantic_inputs={
            "evidence_field": evidence_field,
            "dimension_text_validator": (
                getattr(dimension_text_validator, "__name__", "custom")
                if dimension_text_validator is not None
                else None
            ),
            "allowed_ids_by_dimension": {
                key: sorted(value)
                for key, value in allowed_ids_by_dimension.items()
            },
            "required_dim_6_outcome_ids": sorted(
                required_dim_6_outcome_ids or set()
            ),
            "fixed_relations_by_dimension": {
                dimension: {
                    relation: sorted(ids)
                    for relation, ids in relations.items()
                }
                for dimension, relations in (
                    fixed_relations_by_dimension or {}
                ).items()
            },
        },
    )

    def _validate(
        raw: Mapping[str, Any],
    ) -> tuple[dict[str, str], dict[str, dict[str, list[str]]], list[str]]:
        errors: list[str] = []
        if set(raw) != required_keys:
            errors.append(
                "top_level_keys:"
                f"missing={sorted(required_keys - set(raw))}:"
                f"unknown={sorted(set(raw) - required_keys)}"
            )
        try:
            dimensions = belief_dimensions(raw, label=audit_label)
        except BeliefValidationError as exc:
            dimensions = {}
            errors.append(str(exc))
        if dimensions and dimension_text_validator is not None:
            errors.extend(dimension_text_validator(dimensions))
        evidence, evidence_errors = _normalize_dimension_evidence(
            raw.get(evidence_field),
            field=evidence_field,
            allowed_ids_by_dimension=allowed_ids_by_dimension,
        )
        errors.extend(evidence_errors)
        fixed_relations = fixed_relations_by_dimension or {}
        if evidence:
            for dimension in BELIEF_DIMENSION_KEYS:
                relations = fixed_relations.get(dimension, {})
                for relation in BELIEF_EVIDENCE_RELATIONS:
                    opposite = (
                        "contradict"
                        if relation == "support"
                        else "support"
                    )
                    flipped_ids = set(relations.get(relation, set())) & set(
                        evidence.get(dimension, {}).get(opposite, [])
                    )
                    if flipped_ids:
                        errors.append(
                            f"{evidence_field}.{dimension}:"
                            "must_preserve_evidence_relation:"
                            f"{relation}_moved_to_{opposite}:"
                            f"{sorted(flipped_ids)}"
                        )
        if previous_dimensions is not None and dimensions:
            # 차원별 문장 유지는 정당하다. 관점이 안 변한 차원에 새 표현을
            # 강제하면 임베딩 기반 deviation 측정에 억지 패러프레이즈 노이즈가
            # 깔리고, 그 노이즈는 진짜 변화가 없을수록 커진다(신호와 역방향).
            # 라이브에서도 통합(evidence 규칙)은 전부 지키고 dim_6 문장만
            # 반복한 agent가 소진됐다. 퇴행적 전체 복사만 막는다: 여섯 차원
            # 전부가 직전 LTB와 완전히 동일하면 통합이 아니므로 거부한다.
            if all(
                dimensions[key] == previous_dimensions[key]
                for key in BELIEF_DIMENSION_KEYS
            ):
                errors.append("ltb_must_not_copy_all_six_dimensions_verbatim")
        due_ids = required_dim_6_outcome_ids or set()
        if due_ids and evidence:
            # _normalize_dimension_evidence는 구조가 깨진 차원을 normalized에
            # 넣지 않고 건너뛴다. dim_1~dim_5만 정상이면 evidence는 참이지만
            # dim_6 키가 없어서 직접 첨자 접근은 KeyError를 낸다. 그 예외는
            # 재시도 루프 밖으로 전파돼 재시도 없이 에이전트를 죽인다.
            # (윗줄 must_preserve_evidence_relation 검사와 같은 방어를 쓴다.)
            dim_6_evidence = evidence.get("dim_6", {})
            cited = [
                evidence_id
                for relation in BELIEF_EVIDENCE_RELATIONS
                for evidence_id in dim_6_evidence.get(relation, [])
                if evidence_id in due_ids
            ]
            cited_set = set(cited)
            if len(cited) != len(cited_set) or cited_set != due_ids:
                # 도래 outcome이 3건 동시에 몰리면(같은 이벤트에 h1/h5/
                # next_turn이 겹치는 날) 모델이 그중 하나를 빠뜨리거나
                # 하나를 두 번 인용하는 실수를 반복했다. 어떤 ID가
                # 빠졌는지/중복됐는지 말해주지 않으면 재시도가 무엇을
                # 고쳐야 하는지 알 수 없어 10회 내내 같은 실수를 반복한다.
                missing = sorted(due_ids - cited_set)
                duplicated = sorted(
                    {evidence_id for evidence_id in cited if cited.count(evidence_id) > 1}
                )
                detail = ""
                if missing:
                    detail += f":missing={missing}"
                if duplicated:
                    detail += f":duplicated={duplicated}"
                errors.append(
                    f"every_due_outcome_must_be_cited_once_in_dim_6{detail}"
                )
        return dimensions, evidence, errors

    if journal_call is not None and journal_call.replay is not None:
        raw = dict(journal_call.replay.response)
        dimensions, evidence, errors = _validate(raw)
        if errors:
            raise ResponseJournalError(
                f"accepted replay no longer passes {audit_label}: {errors}"
            )
        journal_call.repair_replay_acceptance(client=client)
        return {
            **dimensions,
            evidence_field: evidence,
            "generation_attempts": journal_call.replay.validation_attempt,
        }

    for attempt in range(1, validation_attempts + 1):
        attempt_seed = seeds[attempt - 1]
        if journal_call is not None:
            journal_call.begin(attempt)
        try:
            response = await client.chat(
                [{"role": "user", "content": current_prompt}],
                response_format={"type": "json_object"},
                temperature=temperatures[attempt - 1],
                seed=attempt_seed,
                max_tokens=max_tokens,
                audit_label=audit_label,
                logical_call_id=(
                    journal_call.logical_call_id
                    if journal_call is not None
                    else None
                ),
                phase_attempt_id=(
                    journal_call.phase_attempt_id
                    if journal_call is not None
                    else None
                ),
            )
        except BaseException as exc:
            if journal_call is not None:
                journal_call.error(attempt, exc)
            raise
        raw_content = response_content(response) or "{}"
        parse_errors: list[str] = []
        raw: dict[str, Any] | None
        try:
            raw = (
                parse_strict_json_object(raw_content)
                if journal_call is not None
                else _parse_json_mapping(raw_content)
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raw = None
            parse_errors.append(f"strict_json_object:{exc}")
        dimensions, evidence, errors = _validate(raw or {})
        errors = [*parse_errors, *errors]
        if not errors:
            if journal_call is not None:
                if raw is None:
                    raise AssertionError("validated response has no raw JSON object")
                journal_call.accept(raw, attempt, client=client)
            return {
                **dimensions,
                evidence_field: evidence,
                "generation_attempts": attempt,
            }
        if journal_call is not None:
            journal_call.rejected(
                attempt,
                response=raw,
                errors=errors,
            )
        invalid_history.append(errors)
        record_validation_failure(
            label=audit_label,
            attempt=attempt,
            errors=errors,
            raw_content=raw_content,
            seed=attempt_seed,
        )
        current_prompt = build_validation_retry_prompt(
            prompt,
            errors=errors,
            schema_hint=(
                "dim_1부터 dim_6까지의 비어 있지 않은 문자열과 "
                f"{evidence_field}만 가진 JSON object를 출력하세요."
                # 같은 ID를 support/contradict 양쪽에 넣는 것은 규칙 위반인 줄
                # 모르는 경우가 대부분이므로 규칙 자체는 알려준다. 다만 어느
                # 쪽에 둘지는 지정하지 않는다. 기본값을 주면 양면적 근거가
                # 특정 방향으로 계도되어 evidence 방향 집계에 연구자가 주입한
                # 편향이 섞인다. 방향 판단은 모델의 관측 대상이다.
                " 같은 근거 ID는 한 차원에서 support 또는 contradict 중"
                " 한 쪽에만 넣으세요. 위 검증 오류에 ID가 적혀 있으면 그 ID를"
                " 페르소나의 판단에 따라 한 쪽에서 제거하고, 지적되지 않은"
                " 다른 차원은 그대로 두세요."
                " 글자 수 초과가 지적된 차원은 한도 안으로 줄여 다시 쓰세요."
                + (
                    " dim_6은 `정보 한계: ... / 주의점: ...` 형식으로 두 표시를"
                    " 모두 포함해야 하며 과거 성과 회고를 쓰지 마세요."
                    if dimension_text_validator is not None
                    else ""
                )
                # 가격 결과의 support/contradict는 dim_6 문장이 아니라 원래
                # 거래 판단 기준으로 강제된다. 라이브에서 모델이 손실 결과를
                # 방향은 더 이상 강제하지 않는다. 남은 요구사항은 "빠짐없이,
                # 각각 한 번만"이라는 완전성뿐이므로 그것만 안내한다.
                + (
                    " 입력에 제공된 가격 결과 ID 전부를 빠짐없이, 각각 한 번만"
                    " dim_6의 support 또는 contradict에 넣어야 합니다."
                    " 어느 쪽에 넣을지는 당신의 판단입니다."
                    " every_due_outcome 오류에 missing이 있으면 그 ID를 추가하고,"
                    " duplicated가 있으면 중복분을 하나만 남기세요."
                    " unknown_ids 오류에 allowed 목록이 있으면 그 안의 ID만"
                    " 사용하고, 목록에 없는 ID는 지어내지 마세요."
                    if required_dim_6_outcome_ids
                    else ""
                )
                # 전 차원 완전 복사 거부 규칙은 프롬프트에 노출하지 않는
                # 구현 상세이므로, 위반으로 거부됐을 때만 여기서 알려준다.
                + (
                    " 여섯 차원 전체를 직전 Long-Term Belief와 완전히 동일하게"
                    " 제출할 수는 없습니다. 오늘의 Short-Term Belief와 체결을"
                    " 통합한 결과를 반영해 다시 작성하세요."
                    if previous_dimensions is not None
                    else ""
                )
            ),
        )
    raise BeliefValidationError(
        f"{audit_label} failed after {validation_attempts} attempts: {invalid_history}"
    )


async def generate_short_term_belief(
    agent: Mapping[str, Any],
    *,
    event: Mapping[str, Any],
    current_evidence: Mapping[str, Any],
    allowed_evidence_ids: set[str],
    client: OpenRouterClient | None = None,
    seed: int | None = None,
    validation_attempts: int = 10,
) -> dict[str, Any]:
    """Create the current-turn STB from news/community evidence only."""

    client = client or OpenRouterClient()
    payload = {
        "schema_version": "simulation-stb-input-v1",
        "persona": {
            "agent_id": str(agent["agent_id"]),
            "news_depth": int(agent.get("news_depth") or 0),
            "persona_prompt": str(agent["persona_prompt"]),
        },
        "event": dict(event),
        "current_evidence": dict(current_evidence),
        "sanitized_evidence_registry": sorted(allowed_evidence_ids),
    }
    allowed = {
        dimension: set(allowed_evidence_ids)
        for dimension in BELIEF_DIMENSION_KEYS
    }
    return await _generate_hierarchical_belief(
        prompt_name="update_short_term_belief.txt",
        prompt_payload=payload,
        evidence_field="dimension_evidence",
        allowed_ids_by_dimension=allowed,
        audit_label="short_term_belief",
        client=client,
        seed=seed,
        validation_attempts=validation_attempts,
        dimension_text_validator=validate_stb_dim_6_scope,
    )


def render_ltb_human_log(
    *,
    previous_dimensions: Mapping[str, str],
    current_dimensions: Mapping[str, str],
) -> tuple[str, dict[str, dict[str, str]]]:
    """Derive non-causal human log fields from committed six-dimensional state."""

    summary = render_belief_summary(current_dimensions)
    view_change = {
        dimension: {
            "before": previous_dimensions[dimension],
            "after": current_dimensions[dimension],
        }
        for dimension in BELIEF_DIMENSION_KEYS
    }
    return summary, view_change


async def update_long_term_belief(
    agent: Mapping[str, Any],
    *,
    event: Mapping[str, Any],
    previous_ltb: Mapping[str, Any],
    current_stb: Mapping[str, Any],
    transaction_episode: Mapping[str, Any],
    eligible_price_outcomes_dim_6_only: list[Mapping[str, Any]] | None = None,
    client: OpenRouterClient | None = None,
    seed: int | None = None,
    validation_attempts: int = 10,
) -> dict[str, Any]:
    """Recursively create LTB_t after the same-turn actual fill is committed."""

    client = client or OpenRouterClient()
    previous_dimensions = belief_dimensions(
        previous_ltb.get("dimensions", previous_ltb),
        label="previous_ltb",
    )
    current_dimensions = belief_dimensions(
        current_stb.get("dimensions", current_stb),
        label="current_stb",
    )
    raw_stb_evidence = current_stb.get("dimension_evidence") or {}
    normalized_stb_evidence, stb_errors = _normalize_dimension_evidence(
        raw_stb_evidence,
        field="current_stb.dimension_evidence",
        allowed_ids_by_dimension={
            key: {
                str(item)
                for relation in BELIEF_EVIDENCE_RELATIONS
                for item in (
                    raw_stb_evidence.get(key, {}).get(relation, [])
                    if isinstance(raw_stb_evidence, Mapping)
                    and isinstance(raw_stb_evidence.get(key), Mapping)
                    else []
                )
            }
            for key in BELIEF_DIMENSION_KEYS
        },
    )
    if stb_errors:
        raise BeliefValidationError(
            "current STB evidence is invalid: " + "; ".join(stb_errors)
        )
    outcomes = [dict(item) for item in (eligible_price_outcomes_dim_6_only or [])]
    outcome_id_list = [
        str(item.get("outcome_id") or "").strip()
        for item in outcomes
    ]
    if any(not outcome_id for outcome_id in outcome_id_list):
        raise BeliefValidationError(
            "every eligible price outcome must have a non-empty outcome_id"
        )
    if len(outcome_id_list) != len(set(outcome_id_list)):
        raise BeliefValidationError(
            "eligible price outcomes contain duplicate outcome_id values"
        )
    outcome_ids = set(outcome_id_list)
    allowed_by_dimension = {
        dimension: {
            evidence_id
            for relation in BELIEF_EVIDENCE_RELATIONS
            for evidence_id in normalized_stb_evidence[dimension][relation]
        }
        for dimension in BELIEF_DIMENSION_KEYS
    }
    allowed_by_dimension["dim_6"].update(outcome_ids)
    fixed_relations_by_dimension = {
        dimension: {
            relation: set(normalized_stb_evidence[dimension][relation])
            for relation in BELIEF_EVIDENCE_RELATIONS
        }
        for dimension in BELIEF_DIMENSION_KEYS
    }
    # 가격 결과의 support/contradict는 더 이상 강제하지 않는다.
    #
    # 예전에는 markout 부호로 관계를 확정해 모델이 그 값을 맞히도록 요구했다.
    # 라이브에서 이 규칙이 실행을 죽인 유일한 지배적 원인이었다(v4 소진 24건
    # 중 22건). 다른 검증 오류는 모델이 2~3회 재시도로 스스로 회복하는데,
    # 이 규칙만은 "모델이 동의하지 않는 외부 정답"이라 10회 내내 자기 직관으로
    # 돌아갔다. 특히 매도 후 하락(=판 물량 기준으로는 이득)을 손실로 읽는
    # 오분류가 반복됐다.
    #
    # 게다가 강제 라벨 자체가 항상 옳지도 않다: 같은 fill이 horizon에 따라
    # 정반대 라벨을 받고(실측 200건 중 100건), 부분 매도는 잔여 포지션 손익을
    # 무시하며, epsilon이 1e-12라 0.01% 움직임도 "판단이 맞았음"이 된다.
    #
    # dim_6는 에이전트의 누적 자기평가, 즉 기억이다. 자기 성과를 어떻게
    # 해석하는지가 관측 대상이므로 판단을 모델에게 돌려준다. 객관적 사실인
    # action_aligned_markout은 simulation_trade_outcomes에 그대로 남으므로,
    # 연구자는 사후에 "에이전트가 자기 성패를 얼마나 정확히 인식했는가"를
    # 두 값의 비교로 측정할 수 있다.
    for outcome in outcomes:
        outcome_id = str(outcome["outcome_id"]).strip()
        try:
            outcome_evidence_relation(outcome.get("action_aligned_markout"))
        except OutcomeScheduleError as exc:
            raise BeliefValidationError(
                f"{outcome_id} has invalid action_aligned_markout"
            ) from exc
    payload = {
        "schema_version": "simulation-post-fill-ltb-input-v1",
        "persona": {
            "agent_id": str(agent["agent_id"]),
            "news_depth": int(agent.get("news_depth") or 0),
            "persona_prompt": str(agent["persona_prompt"]),
        },
        "event": dict(event),
        "previous_ltb": previous_dimensions,
        "current_stb": {
            "dimensions": current_dimensions,
            "dimension_evidence": normalized_stb_evidence,
        },
        "transaction_episode": dict(transaction_episode),
        "eligible_price_outcomes_dim_6_only": outcomes,
        "sanitized_evidence_registry": sorted(
            set().union(*allowed_by_dimension.values())
        ),
    }
    generated = await _generate_hierarchical_belief(
        prompt_name="update_long_term_belief.txt",
        prompt_payload=payload,
        evidence_field="integration_evidence",
        allowed_ids_by_dimension=allowed_by_dimension,
        audit_label="post_fill_long_term_belief",
        client=client,
        seed=seed,
        validation_attempts=validation_attempts,
        previous_dimensions=previous_dimensions,
        required_dim_6_outcome_ids=outcome_ids,
        fixed_relations_by_dimension=fixed_relations_by_dimension,
    )
    summary, view_change = render_ltb_human_log(
        previous_dimensions=previous_dimensions,
        current_dimensions=generated,
    )
    return {
        **generated,
        "belief_summary": summary,
        "view_change": view_change,
    }
