"""Make data/event.pkl the canonical 30-slot fake-news event schedule."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVENT_PATH = PROJECT_ROOT / "data" / "event.pkl"
SOURCE_PATH = PROJECT_ROOT / "data" / "fake_news_bearish_phase_review.pkl"

CLAIM_PATTERN_KO = {
    "rumor_as_fact": "오해·루머 사실화",
    "confirmation_quantity_distortion": "확정·수치 조작",
    "selective_context_emphasis": "선택적 맥락 강조",
}


def main() -> None:
    source = pd.read_pickle(SOURCE_PATH).sort_values("date").reset_index(drop=True)
    if len(source) != 30 or source["base_pair_id"].nunique() != 9:
        raise ValueError("Expected the approved 30-slot schedule across 9 source pairs.")

    backup = EVENT_PATH.with_name(
        f"event_legacy_before_schedule_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pkl"
    )
    shutil.copy2(EVENT_PATH, backup)

    schedule = pd.DataFrame(
        {
            "schedule_id": source["base_pair_id"] + "_" + source["date"].str.replace("-", "", regex=False),
            "event_id": source["event_id"],
            "base_pair_id": source["base_pair_id"],
            "linked_base_pair_ids": source["linked_base_pair_ids"],
            "date": source["date"],
            "event_date": source["event_date"],
            "injection_offset": source["injection_offset"].astype(int),
            "injection_phase": source["injection_phase"],
            "misinformation_type": source["claim_pattern"].map(CLAIM_PATTERN_KO),
            "misinformation_type_code": source["claim_pattern"],
            "event_label": source["event_label"],
            "related_event": source["related_event"],
            "source_news_id": source["source_news_id"],
            "source_date": source["source_date"],
            "source_title": source["source_title"],
            "source_url": source["source_url"],
        }
    )
    if schedule["misinformation_type"].isna().any() or not schedule["schedule_id"].is_unique:
        raise ValueError("Schedule contains an unsupported claim pattern or duplicate slot.")
    schedule.to_pickle(EVENT_PATH)
    print(f"Wrote {len(schedule)} schedule rows to {EVENT_PATH}")
    print(f"Backup: {backup}")


if __name__ == "__main__":
    main()
