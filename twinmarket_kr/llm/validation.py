from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config


MAX_RESPONSE_EXCERPT_CHARS = 2048


class LLMValidationError(RuntimeError):
    """A model response was received but did not satisfy the experiment schema."""


# 재시도는 온도를 올려 가며 표본을 벌린다.
#
# v9까지 모든 stage가 "1회차만 높고 나머지 재시도는 전부 낮은 고정 온도"였다.
# 같은 오류가 반복되면 재시도 프롬프트도 글자까지 같으므로, 같은 입력 + 같은 낮은
# 온도 = 같은 출력이 되어 10회 예산이 실제로는 2회였다. 2026-03-04/PM에서 한
# agent가 attempt 2~10에 걸쳐 응답 해시가 아홉 번 동일했다(고정점). 안내문을
# 아무리 정확히 써도 고정점에서는 빠져나올 수 없다.
#
# 검증 통과 기준은 조금도 완화하지 않는다. 바꾸는 것은 표본 분산뿐이다.
# 실측: 위 고착 호출을 이 스케줄로 재현하니 attempt 4(T=0.6)에서 탈출했다.
_RETRY_TEMPERATURE_RAMP = (0.30, 0.45, 0.60, 0.75, 0.90, 1.00)


def retry_temperature_schedule(
    first_temperature: float,
    validation_attempts: int,
) -> list[float]:
    """1회차는 stage가 정한 온도를 쓰고, 재시도는 1.0까지 단계적으로 올린다.

    1회차 온도는 바꾸지 않으므로 첫 시도에 통과하는 대다수 호출은 영향이 없다.
    """

    if validation_attempts < 1:
        raise ValueError("validation_attempts must be at least 1")
    first = float(first_temperature)
    ramp = [value for value in _RETRY_TEMPERATURE_RAMP if value > first] or [1.0]
    schedule = [first]
    for index in range(validation_attempts - 1):
        schedule.append(ramp[min(index, len(ramp) - 1)])
    return schedule


def build_validation_retry_prompt(
    original_prompt: str,
    *,
    errors: list[str],
    schema_hint: str,
    json_only: bool = True,
) -> str:
    """Ask for a corrected serialization without inventing an experiment fallback."""
    return (
        original_prompt
        + "\n\n[이전 응답 형식 오류]\n"
        + "검증 오류: "
        + json.dumps(errors, ensure_ascii=False)
        + "\n아래 형식에 맞춰 응답 전체를 다시 작성하세요."
        + (" JSON 밖의 설명은 쓰지 마세요.\n" if json_only else "\n")
        + schema_hint.strip()
    )


def validation_audit_path() -> Path:
    return Path(config.OPENROUTER_AUDIT_LOG).with_name("llm_validation_errors.jsonl")


def summarize_validation_audit() -> dict[str, Any]:
    path = validation_audit_path()
    if not path.exists():
        return {
            "validation_audit_path": str(path),
            "validation_retry_events": 0,
            "validation_retry_by_label": {},
        }
    by_label: dict[str, int] = {}
    total = 0
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Invalid validation audit JSON at {path}:{line_number}"
                ) from exc
            label = str(row.get("label") or "unknown")
            by_label[label] = by_label.get(label, 0) + 1
            total += 1
    return {
        "validation_audit_path": str(path),
        "validation_retry_events": total,
        "validation_retry_by_label": dict(sorted(by_label.items())),
    }


def record_validation_failure(
    *,
    label: str,
    attempt: int,
    errors: list[str],
    raw_content: str,
    seed: int | None,
) -> None:
    """Persist a bounded response excerpt for schema debugging, never a prompt or API key."""
    path = validation_audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = str(raw_content or "")
    payload: dict[str, Any] = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "label": label,
        "attempt": int(attempt),
        "seed": seed,
        "validation_errors": list(errors),
        "response_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "response_length": len(raw),
        "response_excerpt": raw[:MAX_RESPONSE_EXCERPT_CHARS],
        "response_excerpt_truncated": len(raw) > MAX_RESPONSE_EXCERPT_CHARS,
    }
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            handle.flush()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def normalize_string_list(value: Any) -> Any:
    """Normalize the two observed Qwen encodings while rejecting unrelated types."""
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if not isinstance(value, list):
        return value
    normalized: list[Any] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            normalized.append(item)
            continue
        stripped = item.strip()
        if stripped and stripped not in seen:
            normalized.append(stripped)
            seen.add(stripped)
    return normalized


def valid_string_list(value: Any, *, allow_empty: bool) -> bool:
    if not isinstance(value, list):
        return False
    if not allow_empty and not value:
        return False
    return all(isinstance(item, str) and bool(item.strip()) for item in value)
