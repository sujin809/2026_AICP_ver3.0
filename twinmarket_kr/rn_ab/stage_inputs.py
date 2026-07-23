"""Sealed timestamp and market inputs for RN STB and decision stages.

The resolved calendar identifies *which* decision event exists, while this
registry pins the timestamp-level news cutoff and the values that can enter a
decision prompt.  Keeping it separate makes the trust boundary explicit:
stage builders receive a registry, never a caller supplied cutoff or market
dictionary.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from twinmarket_kr.rn_ab.memory import EventSchedule, PaperMemoryError, scientific_sha256


class StageInputRegistryError(ValueError):
    """Raised when a timestamp/market registry is not a sealed RN input."""


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_SEOUL = ZoneInfo("Asia/Seoul")
_ROOT_FIELDS = {
    "artifact_type",
    "version",
    "calendar_event_registry_sha256",
    "events",
}
_EVENT_FIELDS = {
    "event_id",
    "date",
    "subturn",
    "news_cutoff_timestamp",
    "market_feature_as_of",
    "market",
}
_MARKET_FIELDS = {
    "reference_price",
    "previous_close",
    "open_price",
    "as_of_timestamp",
}


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise StageInputRegistryError("Stage input registry must be canonical JSON") from exc


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise StageInputRegistryError(f"{label} must be a SHA-256 hex digest")
    return value.lower()


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StageInputRegistryError(f"{label} must be a non-empty string")
    return value.strip()


def _date(value: Any, label: str) -> str:
    text = _text(value, label)
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise StageInputRegistryError(f"{label} must be YYYY-MM-DD") from exc
    return text


def _timestamp(value: Any, label: str, *, expected_date: str) -> str:
    text = _text(value, label)
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise StageInputRegistryError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise StageInputRegistryError(f"{label} must carry an explicit timezone")
    if parsed.astimezone(_SEOUL).date().isoformat() != expected_date:
        raise StageInputRegistryError(f"{label} date differs from its decision event")
    # Store one normalized representation so all downstream hashes are stable.
    return parsed.isoformat()


def _timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _positive_number(value: Any, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StageInputRegistryError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise StageInputRegistryError(f"{label} must be finite and positive")
    return value


@dataclass(frozen=True)
class SealedMarketSnapshot:
    """Only numeric market facts admitted to an RN decision prompt."""

    reference_price: int | float
    previous_close: int | float
    open_price: int | float
    as_of_timestamp: str

    def stage_projection(self, *, subturn: str) -> dict[str, Any]:
        return {
            "reference_price": self.reference_price,
            "previous_close": self.previous_close,
            "open_price": self.open_price,
            "subturn": subturn,
            "as_of_timestamp": self.as_of_timestamp,
        }


@dataclass(frozen=True)
class SealedEventStageInput:
    event_id: str
    date: str
    subturn: str
    news_cutoff_timestamp: str
    market_feature_as_of: str
    market: SealedMarketSnapshot

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "date": self.date,
            "subturn": self.subturn,
            "news_cutoff_timestamp": self.news_cutoff_timestamp,
            "market_feature_as_of": self.market_feature_as_of,
            "market": {
                "reference_price": self.market.reference_price,
                "previous_close": self.market.previous_close,
                "open_price": self.market.open_price,
                "as_of_timestamp": self.market.as_of_timestamp,
            },
        }


@dataclass(frozen=True)
class SealedStageInputRegistry:
    """Calendar-bound stage inputs with no runtime-editable fields."""

    version: str
    calendar_event_registry_sha256: str
    events: tuple[SealedEventStageInput, ...]
    canonical_sha256: str
    file_sha256: str | None = None

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        expected_calendar_event_registry_sha256: str | None = None,
    ) -> "SealedStageInputRegistry":
        if not isinstance(value, Mapping) or set(value) != _ROOT_FIELDS:
            raise StageInputRegistryError("Stage input registry has an invalid root schema")
        if _text(value["artifact_type"], "artifact_type") != "rn_stage_input_registry":
            raise StageInputRegistryError("stage input registry artifact_type is invalid")
        version = _text(value["version"], "version")
        calendar_sha = _sha256(
            value["calendar_event_registry_sha256"], "calendar_event_registry_sha256"
        )
        if (
            expected_calendar_event_registry_sha256 is not None
            and calendar_sha != _sha256(
                expected_calendar_event_registry_sha256,
                "expected calendar_event_registry_sha256",
            )
        ):
            raise StageInputRegistryError("Stage input registry is bound to a different calendar")
        raw_events = value["events"]
        if not isinstance(raw_events, list) or not raw_events:
            raise StageInputRegistryError("stage input registry events must be a non-empty ordered array")
        events: list[SealedEventStageInput] = []
        event_ids: set[str] = set()
        for index, raw in enumerate(raw_events):
            if not isinstance(raw, Mapping) or set(raw) != _EVENT_FIELDS:
                raise StageInputRegistryError(f"stage input event[{index}] has an invalid schema")
            event_id = _text(raw["event_id"], f"stage input event[{index}].event_id")
            if event_id in event_ids:
                raise StageInputRegistryError("stage input registry has duplicate event IDs")
            event_ids.add(event_id)
            day = _date(raw["date"], f"stage input event[{index}].date")
            subturn = _text(raw["subturn"], f"stage input event[{index}].subturn").lower()
            if subturn not in {"am", "pm"}:
                raise StageInputRegistryError("stage input event subturn must be am or pm")
            cutoff = _timestamp(
                raw["news_cutoff_timestamp"],
                f"stage input event[{index}].news_cutoff_timestamp",
                expected_date=day,
            )
            market_as_of = _timestamp(
                raw["market_feature_as_of"],
                f"stage input event[{index}].market_feature_as_of",
                expected_date=day,
            )
            if _timestamp_value(cutoff) > _timestamp_value(market_as_of):
                raise StageInputRegistryError("news cutoff may not be later than market feature as-of")
            raw_market = raw["market"]
            if not isinstance(raw_market, Mapping) or set(raw_market) != _MARKET_FIELDS:
                raise StageInputRegistryError(f"stage input event[{index}].market has an invalid schema")
            market_as_of_value = _timestamp(
                raw_market["as_of_timestamp"],
                f"stage input event[{index}].market.as_of_timestamp",
                expected_date=day,
            )
            if _timestamp_value(market_as_of_value) != _timestamp_value(market_as_of):
                raise StageInputRegistryError("market.as_of_timestamp differs from market_feature_as_of")
            events.append(
                SealedEventStageInput(
                    event_id=event_id,
                    date=day,
                    subturn=subturn,
                    news_cutoff_timestamp=cutoff,
                    market_feature_as_of=market_as_of,
                    market=SealedMarketSnapshot(
                        reference_price=_positive_number(
                            raw_market["reference_price"],
                            f"stage input event[{index}].market.reference_price",
                        ),
                        previous_close=_positive_number(
                            raw_market["previous_close"],
                            f"stage input event[{index}].market.previous_close",
                        ),
                        open_price=_positive_number(
                            raw_market["open_price"],
                            f"stage input event[{index}].market.open_price",
                        ),
                        as_of_timestamp=market_as_of_value,
                    ),
                )
            )
        canonical = {
            "artifact_type": "rn_stage_input_registry",
            "version": version,
            "calendar_event_registry_sha256": calendar_sha,
            "events": [event.to_dict() for event in events],
        }
        return cls(
            version=version,
            calendar_event_registry_sha256=calendar_sha,
            events=tuple(events),
            canonical_sha256=scientific_sha256(canonical),
        )

    @classmethod
    def load(
        cls,
        path: Path | str,
        *,
        expected_file_sha256: str,
        expected_calendar_event_registry_sha256: str | None = None,
        allowed_root: Path | str | None = None,
    ) -> "SealedStageInputRegistry":
        candidate = Path(path)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise StageInputRegistryError(f"Cannot resolve stage input registry: {candidate}") from exc
        if candidate.is_symlink() or resolved.is_symlink() or not resolved.is_file():
            raise StageInputRegistryError("Stage input registry must be a regular non-symlink file")
        if allowed_root is not None:
            try:
                root = Path(allowed_root).resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError) as exc:
                raise StageInputRegistryError("Stage input registry is outside the approved input root") from exc
        try:
            raw = resolved.read_bytes()
        except OSError as exc:
            raise StageInputRegistryError(f"Cannot read stage input registry: {resolved}") from exc
        actual_file_sha = hashlib.sha256(raw).hexdigest()
        if actual_file_sha != _sha256(expected_file_sha256, "expected stage input registry file sha256"):
            raise StageInputRegistryError("Stage input registry file hash differs from the sealed input")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StageInputRegistryError("Stage input registry is not valid UTF-8 JSON") from exc
        registry = cls.from_mapping(
            parsed,
            expected_calendar_event_registry_sha256=expected_calendar_event_registry_sha256,
        )
        return cls(
            version=registry.version,
            calendar_event_registry_sha256=registry.calendar_event_registry_sha256,
            events=registry.events,
            canonical_sha256=registry.canonical_sha256,
            file_sha256=actual_file_sha,
        )

    def event(self, event_id: str) -> SealedEventStageInput:
        requested = _text(event_id, "event_id")
        for event in self.events:
            if event.event_id == requested:
                return event
        raise StageInputRegistryError(f"Event is absent from sealed stage input registry: {requested}")

    def assert_matches_schedule(self, event_schedule: EventSchedule) -> None:
        """Require exactly the same event order, identity, session, and price."""
        try:
            scheduled = tuple(event_schedule.events)
        except (AttributeError, PaperMemoryError) as exc:
            raise StageInputRegistryError("A valid EventSchedule is required") from exc
        if len(scheduled) != len(self.events):
            raise StageInputRegistryError("Stage input registry event count differs from EventSchedule")
        for schedule_event, stage_event in zip(scheduled, self.events, strict=True):
            if (
                schedule_event["event_id"] != stage_event.event_id
                or schedule_event["date"] != stage_event.date
                or schedule_event["subturn"] != stage_event.subturn
            ):
                raise StageInputRegistryError("Stage input registry event identity differs from EventSchedule")
            if abs(
                float(schedule_event["execution_price"]) - float(stage_event.market.reference_price)
            ) > 1e-9:
                raise StageInputRegistryError(
                    "Stage input market reference price differs from EventSchedule execution price"
                )


__all__ = [
    "SealedEventStageInput",
    "SealedMarketSnapshot",
    "SealedStageInputRegistry",
    "StageInputRegistryError",
]
