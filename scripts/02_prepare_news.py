#!/usr/bin/env python3
"""Validate the sealed news profile used by the numbered experiment pipeline.

The old version of this script sampled a legacy pickle into mutable CSV files.
That output is not consumed by the integrated runner and could be mistaken for
the experiment input.  The default numbered step is now deliberately
read-only: news construction remains an explicit data-maintenance operation in
``13_bind_news_provenance.py`` and ``14_seal_news_bundle.py``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from twinmarket_kr.agents.news_agent import SealedNewsBundle
from twinmarket_kr.outcome_schedule import FrozenEventSchedule


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"{label} must be a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must contain a JSON object: {path}")
    return value


def validate_sealed_news(profile_root: Path) -> dict[str, Any]:
    """Return a compact, read-only validation report for one sealed profile."""

    root = profile_root.resolve()
    news_path = root / "news.json"
    calendar_path = root / "calendar.json"
    prices_path = root / "prices.json"
    study_spec_path = root / "study_spec.json"

    bundle = SealedNewsBundle.load(news_path, expected_stock_code=None)
    schedule = FrozenEventSchedule.from_sealed_files(
        calendar_path,
        prices_path,
        expected_stock_code=bundle.stock_code,
    )
    expected_event_ids = [str(event["event_id"]) for event in schedule.events]
    observed_event_ids = list(bundle.slots_by_event)
    if observed_event_ids != expected_event_ids:
        raise RuntimeError(
            "sealed news event order differs from the calendar/price registry"
        )

    study_spec = _read_json_object(study_spec_path, "study spec")
    if (
        str(study_spec.get("real_news_bundle_manifest_sha256") or "")
        != bundle.bundle_sha256
    ):
        raise RuntimeError("study spec does not pin the loaded news bundle hash")
    exposure_policy = study_spec.get("news_exposure_policy")
    if not isinstance(exposure_policy, dict):
        raise RuntimeError("study spec has no news_exposure_policy object")
    if int(exposure_policy.get("target_real_news_per_event", -1)) != int(
        bundle.target_real_count
    ):
        raise RuntimeError(
            "study spec news target differs from the sealed bundle target"
        )
    if int(exposure_policy.get("fake_news_per_event", -1)) != 0:
        raise RuntimeError("the current baseline must remain real-news-only")

    slot_counts = {
        event_id: len(slots)
        for event_id, slots in bundle.slots_by_event.items()
    }
    shortage_event_ids = [
        event_id
        for event_id in expected_event_ids
        if slot_counts[event_id] < bundle.target_real_count
    ]
    if set(shortage_event_ids) != set(bundle.accepted_shortages):
        raise RuntimeError(
            "accepted_shortages does not exactly match the short event set"
        )

    return {
        "validation_pass": True,
        "mode": "read_only_sealed_news_validation",
        "profile_root": str(root),
        "stock_code": bundle.stock_code,
        "bundle_sha256": bundle.bundle_sha256,
        "bundle_file_sha256": bundle.file_sha256,
        "calendar_sha256": schedule.calendar_sha256,
        "prices_sha256": schedule.prices_sha256,
        "event_count": len(expected_event_ids),
        "article_count": len(bundle.articles),
        "slot_count": sum(slot_counts.values()),
        "target_real_news_per_event": bundle.target_real_count,
        "shortage_event_count": len(shortage_event_ids),
        "shortage_event_ids": shortage_event_ids,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "봉인된 실제뉴스·달력·가격·StudySpec 연결을 파일 수정 없이 검증합니다."
        )
    )
    parser.add_argument(
        "--profile-root",
        type=Path,
        default=config.SEALED_REAL_NEWS_BUNDLE.parent,
    )
    args = parser.parse_args(argv)
    print(
        json.dumps(
            validate_sealed_news(args.profile_root),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
