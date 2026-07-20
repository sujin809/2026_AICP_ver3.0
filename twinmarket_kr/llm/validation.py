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
