from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


OUTCOME_HORIZONS = ("next_turn", "h1", "h5")
OUTCOME_MARKOUT_RELATION_EPSILON = 1e-12

_CALENDAR_FIELDS = {"artifact_type", "version", "dates"}
_CALENDAR_DATE_FIELDS = {
    "date",
    "timezone",
    "decision_events",
    "post_decision_phases",
}
_DECISION_EVENT_FIELDS = {
    "consume_scheduled_community",
    "decision_enabled",
    "decision_event_id",
    "decision_timestamp",
    "event_ordinal_in_date",
    "execution_price_field",
    "market_feature_as_of",
    "news_window",
    "subturn",
}
_NEWS_WINDOW_FIELDS = {"start_exclusive", "end_inclusive"}
_COMMUNITY_PHASE_FIELDS = {
    "after_event_id",
    "next_visible_event_rule",
    "phase_id",
}
_PRICE_REGISTRY_FIELDS = {
    "artifact_type",
    "version",
    "stock_code",
    "calendar_event_registry_sha256",
    "events",
}
_PRICE_EVENT_FIELDS = {
    "date",
    "decision_event_id",
    "execution_price",
    "execution_price_field",
    "subturn",
}
_SCHEDULE_ROW_FIELDS = {
    "event_id",
    "turn",
    "date",
    "subturn",
    "execution_price",
    "execution_price_field",
}


class OutcomeScheduleError(ValueError):
    """Raised when a frozen event or price registry is not self-consistent."""


def outcome_evidence_relation(
    action_aligned_markout: Any,
) -> str | None:
    """Map a realized action-aligned return to its dim-6 evidence relation.

    A positive markout supports the direction of the recorded action; a
    negative markout contradicts it.  An effectively zero return has no
    defensible polarity, so callers must still consume the outcome but may put
    it in either relation.
    """

    if isinstance(action_aligned_markout, bool):
        raise OutcomeScheduleError(
            "action_aligned_markout must be a finite number"
        )
    try:
        numeric = float(action_aligned_markout)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OutcomeScheduleError(
            "action_aligned_markout must be a finite number"
        ) from exc
    if not math.isfinite(numeric):
        raise OutcomeScheduleError(
            "action_aligned_markout must be a finite number"
        )
    if numeric > OUTCOME_MARKOUT_RELATION_EPSILON:
        return "support"
    if numeric < -OUTCOME_MARKOUT_RELATION_EPSILON:
        return "contradict"
    return None


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OutcomeScheduleError("sealed registry must be canonical JSON data") from exc


def _exact_mapping(
    value: Any,
    *,
    label: str,
    fields: set[str],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise OutcomeScheduleError(f"{label} must be a JSON object")
    if set(value) != fields:
        raise OutcomeScheduleError(
            f"{label} fields differ from the sealed contract: "
            f"missing={sorted(fields - set(value))}, "
            f"unknown={sorted(set(value) - fields)}"
        )
    return value


def _nonempty_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OutcomeScheduleError(f"{label} must be a non-empty string")
    return value.strip()


def _positive_price(value: Any, *, label: str) -> float:
    if isinstance(value, bool):
        raise OutcomeScheduleError(f"{label} must be a positive finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OutcomeScheduleError(
            f"{label} must be a positive finite number"
        ) from exc
    if not math.isfinite(result) or result <= 0:
        raise OutcomeScheduleError(f"{label} must be a positive finite number")
    return result


def _positive_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool):
        raise OutcomeScheduleError(f"{label} must be a positive integer")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OutcomeScheduleError(f"{label} must be a positive integer") from exc
    if not math.isfinite(numeric) or numeric < 1 or not numeric.is_integer():
        raise OutcomeScheduleError(f"{label} must be a positive integer")
    return int(numeric)


def _iso_date(value: Any, *, label: str) -> str:
    text = _nonempty_text(value, label=label)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise OutcomeScheduleError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != text:
        raise OutcomeScheduleError(f"{label} must use canonical YYYY-MM-DD")
    return text


def _timestamp(value: Any, *, label: str) -> datetime:
    text = _nonempty_text(value, label=label)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise OutcomeScheduleError(f"{label} must be an ISO timestamp") from exc
    if parsed.utcoffset() is None:
        raise OutcomeScheduleError(f"{label} must include its timezone offset")
    return parsed


def _load_json_object(path: Path | str, *, label: str) -> tuple[dict[str, Any], bytes]:
    source = Path(path)
    if source.is_symlink():
        raise OutcomeScheduleError(f"{label} may not be loaded through a symlink")
    if not source.is_file():
        raise OutcomeScheduleError(f"{label} is not a regular file: {source}")
    raw = source.read_bytes()
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OutcomeScheduleError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise OutcomeScheduleError(f"{label} must contain a JSON object")
    canonical = _canonical_json_bytes(decoded)
    if raw != canonical:
        raise OutcomeScheduleError(
            f"{label} must use the sealed canonical JSON byte representation"
        )
    return decoded, canonical


@dataclass(frozen=True)
class FrozenEventSchedule:
    """Validated AM/PM event order and its only permitted mark prices.

    This main-runtime implementation is deliberately independent of
    ``twinmarket_kr.rn_ab``.  The RN study supplied the semantics, while the
    integrated simulator owns its persistence and execution path.
    """

    events: tuple[dict[str, Any], ...]
    stock_code: str
    calendar_sha256: str
    prices_sha256: str

    @classmethod
    def from_rows(
        cls,
        rows: Iterable[Mapping[str, Any]],
        *,
        stock_code: str,
        calendar_sha256: str = "unsealed-test-schedule",
        prices_sha256: str = "unsealed-test-prices",
    ) -> "FrozenEventSchedule":
        parsed: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_turns: set[int] = set()
        for index, raw in enumerate(rows, start=1):
            row = _exact_mapping(
                raw,
                label=f"event schedule row {index}",
                fields=_SCHEDULE_ROW_FIELDS,
            )
            event_id = _nonempty_text(row["event_id"], label="event_id")
            turn = _positive_int(row["turn"], label="event turn")
            event_date = _iso_date(row["date"], label="event date")
            subturn = _nonempty_text(row["subturn"], label="event subturn").lower()
            if subturn not in {"am", "pm"}:
                raise OutcomeScheduleError("event subturn must be am or pm")
            expected_id = f"{event_date}/{subturn.upper()}"
            if event_id != expected_id:
                raise OutcomeScheduleError(
                    f"event_id must be {expected_id}, got {event_id}"
                )
            expected_price_field = (
                "actual_open" if subturn == "am" else "actual_close"
            )
            price_field = _nonempty_text(
                row["execution_price_field"],
                label="execution_price_field",
            )
            if price_field != expected_price_field:
                raise OutcomeScheduleError(
                    f"{event_id} must use {expected_price_field}"
                )
            if event_id in seen_ids or turn in seen_turns:
                raise OutcomeScheduleError("event IDs and turns must be unique")
            seen_ids.add(event_id)
            seen_turns.add(turn)
            parsed.append(
                {
                    "event_id": event_id,
                    "turn": turn,
                    "date": event_date,
                    "subturn": subturn,
                    "execution_price": _positive_price(
                        row["execution_price"],
                        label=f"{event_id} execution_price",
                    ),
                    "execution_price_field": price_field,
                }
            )
        if not parsed:
            raise OutcomeScheduleError("event schedule must not be empty")
        parsed.sort(key=lambda item: int(item["turn"]))
        turns = [int(item["turn"]) for item in parsed]
        if turns != list(range(1, len(parsed) + 1)):
            raise OutcomeScheduleError(
                "event turns must be contiguous and start at one"
            )
        previous_date: str | None = None
        for offset in range(0, len(parsed), 2):
            pair = parsed[offset : offset + 2]
            if len(pair) != 2:
                raise OutcomeScheduleError(
                    "every trading date must contain one AM and one PM event"
                )
            if (
                pair[0]["date"] != pair[1]["date"]
                or pair[0]["subturn"] != "am"
                or pair[1]["subturn"] != "pm"
            ):
                raise OutcomeScheduleError(
                    "events must be ordered as one AM/PM pair per trading date"
                )
            if previous_date is not None and str(pair[0]["date"]) <= previous_date:
                raise OutcomeScheduleError(
                    "trading dates must be strictly increasing"
                )
            previous_date = str(pair[0]["date"])
        return cls(
            events=tuple(parsed),
            stock_code=_nonempty_text(stock_code, label="stock_code"),
            calendar_sha256=_nonempty_text(
                calendar_sha256,
                label="calendar_sha256",
            ),
            prices_sha256=_nonempty_text(
                prices_sha256,
                label="prices_sha256",
            ),
        )

    @classmethod
    def from_sealed_files(
        cls,
        calendar_path: Path | str,
        prices_path: Path | str,
        *,
        expected_stock_code: str | None = None,
    ) -> "FrozenEventSchedule":
        calendar, calendar_bytes = _load_json_object(
            calendar_path,
            label="calendar registry",
        )
        prices, prices_bytes = _load_json_object(
            prices_path,
            label="price registry",
        )
        calendar_root = _exact_mapping(
            calendar,
            label="calendar registry",
            fields=_CALENDAR_FIELDS,
        )
        if calendar_root["artifact_type"] != "calendar_event_registry":
            raise OutcomeScheduleError("calendar artifact_type is invalid")
        if calendar_root["version"] != "calendar-v1":
            raise OutcomeScheduleError("calendar version is unsupported")
        date_rows = calendar_root["dates"]
        if not isinstance(date_rows, list) or not date_rows:
            raise OutcomeScheduleError("calendar dates must be a non-empty list")

        calendar_events: list[dict[str, Any]] = []
        previous_date: str | None = None
        for date_index, raw_date in enumerate(date_rows, start=1):
            date_row = _exact_mapping(
                raw_date,
                label=f"calendar date {date_index}",
                fields=_CALENDAR_DATE_FIELDS,
            )
            event_date = _iso_date(
                date_row["date"],
                label=f"calendar date {date_index}.date",
            )
            if previous_date is not None and event_date <= previous_date:
                raise OutcomeScheduleError(
                    "calendar dates must be strictly increasing"
                )
            previous_date = event_date
            if date_row["timezone"] != "Asia/Seoul":
                raise OutcomeScheduleError(
                    f"{event_date} timezone must be Asia/Seoul"
                )
            raw_events = date_row["decision_events"]
            if not isinstance(raw_events, list) or len(raw_events) != 2:
                raise OutcomeScheduleError(
                    f"{event_date} must contain exactly AM and PM decision events"
                )
            for ordinal, (raw_event, expected_subturn) in enumerate(
                zip(raw_events, ("AM", "PM")),
                start=1,
            ):
                event = _exact_mapping(
                    raw_event,
                    label=f"{event_date} {expected_subturn}",
                    fields=_DECISION_EVENT_FIELDS,
                )
                event_id = f"{event_date}/{expected_subturn}"
                if (
                    event["decision_event_id"] != event_id
                    or event["subturn"] != expected_subturn
                    or event["event_ordinal_in_date"] != ordinal
                    or event["decision_enabled"] is not True
                ):
                    raise OutcomeScheduleError(
                        f"{event_id} identity, ordinal, or enabled flag is invalid"
                    )
                expected_consume = expected_subturn == "AM"
                if event["consume_scheduled_community"] is not expected_consume:
                    raise OutcomeScheduleError(
                        f"{event_id} community-consumption flag is invalid"
                    )
                expected_field = (
                    "actual_open" if expected_subturn == "AM" else "actual_close"
                )
                if event["execution_price_field"] != expected_field:
                    raise OutcomeScheduleError(
                        f"{event_id} execution price field is invalid"
                    )
                decision_time = _timestamp(
                    event["decision_timestamp"],
                    label=f"{event_id}.decision_timestamp",
                )
                feature_time = _timestamp(
                    event["market_feature_as_of"],
                    label=f"{event_id}.market_feature_as_of",
                )
                expected_clock = (
                    (9, 0) if expected_subturn == "AM" else (15, 30)
                )
                if (
                    decision_time.date().isoformat() != event_date
                    or (decision_time.hour, decision_time.minute) != expected_clock
                    or decision_time.second != 0
                    or decision_time.utcoffset().total_seconds() != 9 * 3600
                    or feature_time != decision_time
                ):
                    raise OutcomeScheduleError(
                        f"{event_id} timestamp or market-feature cutoff is invalid"
                    )
                news_window = _exact_mapping(
                    event["news_window"],
                    label=f"{event_id}.news_window",
                    fields=_NEWS_WINDOW_FIELDS,
                )
                start = _timestamp(
                    news_window["start_exclusive"],
                    label=f"{event_id}.news_window.start_exclusive",
                )
                end = _timestamp(
                    news_window["end_inclusive"],
                    label=f"{event_id}.news_window.end_inclusive",
                )
                if not start < end <= decision_time:
                    raise OutcomeScheduleError(
                        f"{event_id} news window is not time-safe"
                    )
                calendar_events.append(
                    {
                        "event_id": event_id,
                        "date": event_date,
                        "subturn": expected_subturn.lower(),
                        "execution_price_field": expected_field,
                    }
                )
            phases = date_row["post_decision_phases"]
            if not isinstance(phases, list) or len(phases) != 1:
                raise OutcomeScheduleError(
                    f"{event_date} must contain one post-PM community phase"
                )
            phase = _exact_mapping(
                phases[0],
                label=f"{event_date}.post_decision_phase",
                fields=_COMMUNITY_PHASE_FIELDS,
            )
            if phase != {
                "after_event_id": f"{event_date}/PM",
                "next_visible_event_rule": "next-approved-AM",
                "phase_id": f"{event_date}/community",
            }:
                raise OutcomeScheduleError(
                    f"{event_date} post-decision community phase is invalid"
                )

        price_root = _exact_mapping(
            prices,
            label="price registry",
            fields=_PRICE_REGISTRY_FIELDS,
        )
        if price_root["artifact_type"] != "event_price_registry":
            raise OutcomeScheduleError("price registry artifact_type is invalid")
        if price_root["version"] != "prices-v1":
            raise OutcomeScheduleError("price registry version is unsupported")
        calendar_sha256 = hashlib.sha256(calendar_bytes).hexdigest()
        if price_root["calendar_event_registry_sha256"] != calendar_sha256:
            raise OutcomeScheduleError(
                "price registry is not bound to the supplied calendar registry"
            )
        stock_code = _nonempty_text(
            price_root["stock_code"],
            label="price registry stock_code",
        )
        if (
            expected_stock_code is not None
            and stock_code
            != _nonempty_text(expected_stock_code, label="expected_stock_code")
        ):
            raise OutcomeScheduleError(
                f"price registry stock_code={stock_code} does not match "
                f"expected_stock_code={expected_stock_code}"
            )
        price_events = price_root["events"]
        if not isinstance(price_events, list):
            raise OutcomeScheduleError("price registry events must be a list")
        if len(price_events) != len(calendar_events):
            raise OutcomeScheduleError(
                "calendar and price registry event counts differ"
            )
        schedule_rows: list[dict[str, Any]] = []
        for turn, (calendar_event, raw_price) in enumerate(
            zip(calendar_events, price_events),
            start=1,
        ):
            price_event = _exact_mapping(
                raw_price,
                label=f"price event {turn}",
                fields=_PRICE_EVENT_FIELDS,
            )
            identity = {
                "event_id": price_event["decision_event_id"],
                "date": price_event["date"],
                "subturn": str(price_event["subturn"]).lower(),
                "execution_price_field": price_event["execution_price_field"],
            }
            if identity != calendar_event:
                raise OutcomeScheduleError(
                    f"price event {turn} does not match the ordered calendar event"
                )
            schedule_rows.append(
                {
                    **identity,
                    "turn": turn,
                    "execution_price": price_event["execution_price"],
                }
            )
        return cls.from_rows(
            schedule_rows,
            stock_code=stock_code,
            calendar_sha256=calendar_sha256,
            prices_sha256=hashlib.sha256(prices_bytes).hexdigest(),
        )

    def event(self, event_id: str) -> dict[str, Any]:
        target = _nonempty_text(event_id, label="event_id")
        for event in self.events:
            if event["event_id"] == target:
                return dict(event)
        raise OutcomeScheduleError(
            f"event is not in the frozen schedule: {target}"
        )

    def event_for_identity(
        self,
        *,
        turn: int,
        event_date: str,
        subturn: str,
    ) -> dict[str, Any]:
        event_turn = _positive_int(turn, label="event turn")
        if event_turn > len(self.events):
            raise OutcomeScheduleError(
                f"turn is outside the frozen schedule: {event_turn}"
            )
        event = dict(self.events[event_turn - 1])
        normalized_subturn = _nonempty_text(
            subturn,
            label="event subturn",
        ).lower()
        if (
            event["date"] != event_date
            or event["subturn"] != normalized_subturn
        ):
            raise OutcomeScheduleError(
                "turn/date/subturn differ from the frozen event schedule"
            )
        return event

    def due_event_id(self, *, fill_event_id: str, horizon: str) -> str | None:
        if horizon not in OUTCOME_HORIZONS:
            raise OutcomeScheduleError(f"unknown outcome horizon: {horizon}")
        fill = self.event(fill_event_id)
        fill_turn = int(fill["turn"])
        if horizon == "next_turn":
            if fill_turn >= len(self.events):
                return None
            return str(self.events[fill_turn]["event_id"])
        later_same_subturn = [
            event
            for event in self.events
            if int(event["turn"]) > fill_turn
            and event["subturn"] == fill["subturn"]
        ]
        offset = 0 if horizon == "h1" else 4
        if len(later_same_subturn) <= offset:
            return None
        return str(later_same_subturn[offset]["event_id"])

    @property
    def first_event_id(self) -> str:
        return str(self.events[0]["event_id"])

    @property
    def last_event_id(self) -> str:
        return str(self.events[-1]["event_id"])
