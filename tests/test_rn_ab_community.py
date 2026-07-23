from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from twinmarket_kr.db.connection import connect
from twinmarket_kr.rn_ab.community import CommunityContractError, RNCommunityService
from twinmarket_kr.rn_ab.memory import EventSchedule, PaperMemoryStore
from twinmarket_kr.rn_ab.news import (
    SealedNewsRegistry,
    article_payload_sha256,
    bundle_content_sha256,
    fake_registry_sha256,
)
from twinmarket_kr.rn_ab.spec import RN_COMM_OFF, RN_COMM_ON
from twinmarket_kr.rn_ab.stage_inputs import SealedStageInputRegistry


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _schedule(*, include_next_am: bool = True) -> EventSchedule:
    rows = [
        {
            "event_id": "2026-02-27/am",
            "turn": 1,
            "date": "2026-02-27",
            "subturn": "am",
            "execution_price": 70_000,
        },
        {
            "event_id": "2026-02-27/pm",
            "turn": 2,
            "date": "2026-02-27",
            "subturn": "pm",
            "execution_price": 70_100,
        },
    ]
    if include_next_am:
        rows.append(
            {
                "event_id": "2026-03-03/am",
                "turn": 3,
                "date": "2026-03-03",
                "subturn": "am",
                "execution_price": 70_200,
            }
        )
    return EventSchedule.from_rows(rows)


def _sealed_inputs(schedule: EventSchedule) -> tuple[SealedNewsRegistry, SealedStageInputRegistry]:
    """Minimal complete sealed input artifacts; every schedule event has one real article."""

    articles: list[dict] = []
    slots: list[dict] = []
    stage_events: list[dict] = []
    for index, event in enumerate(schedule.events, start=1):
        day = str(event["date"])
        subturn = str(event["subturn"])
        article_base = {
            "article_id": f"article-{index}",
            "title": f"Samsung operating update {index}",
            "summary": f"A neutral pre-session operating update {index}.",
            "published_at": f"{day}T07:00:00+09:00",
            "observed_at": f"{day}T07:30:00+09:00",
            "last_modified_at": f"{day}T07:15:00+09:00",
            "source_url": f"https://example.com/news/{index}",
            "source": "ExampleWire",
            "raw_body_sha256": _hash(f"article-body-{index}"),
            "version_sha256": _hash(f"article-version-{index}"),
            "cutoff_version_sha256": _hash(f"article-cutoff-version-{index}"),
        }
        payload_sha = article_payload_sha256(article_base)
        articles.append({**article_base, "payload_sha256": payload_sha})
        slots.append(
            {
                "event_id": str(event["event_id"]),
                "slot_ordinal": 1,
                "article_id": article_base["article_id"],
                "payload_sha256": payload_sha,
            }
        )
        if subturn == "am":
            as_of = f"{day}T08:30:00+09:00"
        else:
            as_of = f"{day}T15:30:00+09:00"
        stage_events.append(
            {
                "event_id": str(event["event_id"]),
                "date": day,
                "subturn": subturn,
                "news_cutoff_timestamp": as_of,
                "market_feature_as_of": as_of,
                "market": {
                    "reference_price": event["execution_price"],
                    "previous_close": event["execution_price"],
                    "open_price": event["execution_price"],
                    "as_of_timestamp": as_of,
                },
            }
        )
    fake_hash = fake_registry_sha256(known_fake_ids=[], known_fake_payload_hashes=[])
    bundle = {
        "artifact_type": "real_news_bundle_manifest",
        "bundle_sha256": "0" * 64,
        "stock_code": "005930",
        "target_real_news_per_event": 1,
        "fake_news_per_event": 0,
        "articles": articles,
        "slots": slots,
        "accepted_shortages": {},
        "known_fake_ids": [],
        "known_fake_payload_hashes": [],
        "fake_registry_sha256": fake_hash,
    }
    bundle["bundle_sha256"] = bundle_content_sha256(bundle)
    stage = {
        "artifact_type": "rn_stage_input_registry",
        "version": "stage-v1",
        "calendar_event_registry_sha256": _hash("calendar-registry"),
        "events": stage_events,
    }
    return SealedNewsRegistry.from_mapping(bundle), SealedStageInputRegistry.from_mapping(stage)


def _policy() -> dict:
    return {
        "best_k": 5,
        "best_selection_policy": "top_k_or_fewer_available_no_forced_posting",
        "permissions_from_cohort_depth_map": True,
        "depth1_selective_read_cap": 5,
        "depth2_selective_read_cap": 10,
        "best_payload": "title_plus_full_frozen_body",
        "visibility": "next_approved_am_decision_event",
    }


def _trade_policy() -> dict:
    return {
        "stock_code": "005930",
        "decision_space": ["buy", "sell"],
        "allow_hold": False,
        "max_single_trade_cash_ratio": 0.5,
        "fill_policy": "full_fill_at_sealed_event_price",
        "commission_rate": 0.0,
        "commission_applies_to": [],
        "sell_tax_rate": 0.0,
        "fee_policy": "zero_fee_v1",
        "target_direction_notional": "gross_signed_fill_value",
    }


def _phase_registry() -> list[dict]:
    return [
        {
            "phase_id": "2026-02-27/community",
            "date": "2026-02-27",
            "after_event_id": "2026-02-27/pm",
        }
    ]


def _timing_policy() -> dict:
    return {
        "timezone": "Asia/Seoul",
        "pm_phase_not_before": "15:30:00",
        "pm_phase_not_after": "23:59:59",
        "next_am_delivery_not_before": "08:00:00",
        "next_am_delivery_not_after": "09:00:00",
    }


def _profiles() -> dict:
    return {
        "A1": {
            "schema_version": "rn-public-profile-v1",
            "public_badges": ["verified"],
            "public_direction": "neutral",
            "public_reliability_score": 70,
        },
        "A2": {
            "schema_version": "rn-public-profile-v1",
            "public_badges": ["analyst"],
            "public_direction": "bullish",
            "public_reliability_score": 85,
        },
    }


def _phase(*, final: bool = False) -> dict:
    return {
        "phase_id": "2026-02-27/community",
        "after_event": {
            "event_id": "2026-02-27/pm",
            "turn": 2,
            "date": "2026-02-27",
            "subturn": "pm",
        },
        "observed_at": "2026-02-27T15:31:00+09:00",
        "next_am_event": None
        if final
        else {
            "event_id": "2026-03-03/am",
            "turn": 3,
            "date": "2026-03-03",
            "subturn": "am",
        },
    }


def _draft(author: str, *, title: str, body: str, score: int, likes: int = 0) -> dict:
    return {
        "author_agent_id": author,
        "title": title,
        "body": body,
        "body_sha256": _sha256(body),
        "post_type": "analysis",
        "score": score,
        "like_count": likes,
    }


def _reader_traces(
    service: RNCommunityService,
    phase: dict,
    drafts: list[dict] | tuple[dict, ...],
    *,
    selections: list[dict] | tuple[dict, ...] = (),
) -> tuple[dict, ...]:
    """Local-fixture trace preserving the exact title board with no reactions."""

    candidates = service.preview_candidate_posts(phase, post_drafts=drafts)
    author_by_title = {str(draft["title"]): str(draft["author_agent_id"]) for draft in drafts}
    if len(author_by_title) != len(drafts):
        raise AssertionError("community test fixture requires unique titles")
    selected = {
        (str(row["reader_agent_id"]), str(row["post_id"]))
        for row in selections
    }
    return tuple(
        {
            "reader_agent_id": reader,
            "candidates": [
                {
                    "post_id": candidate["post_id"],
                    "title": candidate["title"],
                    "post_type": candidate["post_type"],
                    "score": candidate["score"],
                    "like_count": candidate["like_count"],
                    "selected": (reader, candidate["post_id"]) in selected,
                    "reaction": (
                        "none"
                        if (reader, candidate["post_id"]) in selected
                        else None
                    ),
                }
                for candidate in candidates
                if author_by_title[candidate["title"]] != reader
            ],
        }
        for reader in service.cohort_agent_ids
        if service.cohort_depths[reader] in {1, 2}
    )


class RNCommunityServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "run.sqlite"
        self.depths = {"A0": 0, "A1": 1, "A2": 2}

    def _service(self, condition: str, *, final_schedule: bool = False) -> RNCommunityService:
        schedule = _schedule(include_next_am=not final_schedule)
        news_registry, stage_input_registry = _sealed_inputs(schedule)
        memory = PaperMemoryStore(
            self.db_path,
            run_id="run-community",
            condition_id=condition,
            manifest_sha256=_hash("manifest"),
            event_schedule=schedule,
            news_registry=news_registry,
            stage_input_registry=stage_input_registry,
            initial_portfolios={
                "A0": {"cash": 1_000_000, "quantity": 0},
                "A1": {"cash": 1_000_000, "quantity": 0},
                "A2": {"cash": 1_000_000, "quantity": 0},
            },
            trade_policy=_trade_policy(),
            belief_limits={"dim_1": 150, **{f"dim_{index}": 100 for index in range(2, 7)}},
        )
        return RNCommunityService(
            memory,
            cohort_depths=self.depths,
            public_profiles=_profiles(),
            community_policy=_policy(),
            community_phase_registry=_phase_registry(),
            community_timing_policy=_timing_policy(),
        )

    def _count(self, table: str) -> int:
        with connect(self.db_path, read_only=True) as connection:
            return int(connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])

    def test_off_only_writes_explicit_noop_and_never_exposes(self) -> None:
        service = self._service(RN_COMM_OFF)

        checkpoint = service.run_pm_phase(_phase())

        self.assertEqual(checkpoint.mode, "off")
        self.assertEqual(self._count("agent_exposures"), 0)
        self.assertEqual(self._count("observation_events"), 1)
        with connect(self.db_path, read_only=True) as connection:
            self.assertEqual(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM observation_events "
                        "WHERE stage LIKE 'community_reader_trace:%'"
                    ).fetchone()[0]
                ),
                0,
            )
        self.assertEqual(service.validate_claims_for_agent(agent_id="A0", event_id="2026-03-03/am", claims=[]), ())
        with self.assertRaises(CommunityContractError):
            service.run_pm_phase(
                _phase(),
                post_drafts=[_draft("A1", title="forbidden", body="must not leak", score=1)],
            )
        with self.assertRaises(CommunityContractError):
            service.validate_claims_for_agent(
                agent_id="A0",
                event_id="2026-03-03/am",
                claims=[
                    {
                        "claim_text": "fake off-arm claim",
                        "stance": "neutral",
                        "source_exposure_ids": ["exp:forged"],
                    }
                ],
            )
        with self.assertRaises(CommunityContractError):
            service.run_pm_phase(
                _phase(),
                private_reader_traces=[
                    {"reader_agent_id": "A1", "candidates": []}
                ],
            )

    def test_best_broadcasts_full_body_to_all_and_deduplicates_overlap(self) -> None:
        service = self._service(RN_COMM_ON)
        body_one = "FULL ORIGINAL BODY: the selected post must not be truncated."
        body_two = "SECOND FULL ORIGINAL BODY: every Best reader receives this exact payload."
        drafts = [
            _draft("A1", title="First", body=body_one, score=9, likes=1),
            _draft("A2", title="Second", body=body_two, score=8, likes=4),
        ]
        candidates = service.preview_candidate_posts(_phase(), post_drafts=drafts)
        first_post_id = next(item["post_id"] for item in candidates if item["title"] == "First")
        selections = [{"reader_agent_id": "A2", "post_id": first_post_id}]
        checkpoint = service.run_pm_phase(
            _phase(),
            post_drafts=drafts,
            selective_reads=selections,
            private_reader_traces=_reader_traces(
                service,
                _phase(),
                drafts,
                selections=selections,
            ),
        )
        self.assertEqual(len(checkpoint.best_post_ids), 2)
        self.assertEqual(self._count("agent_exposures"), 1)  # only the PM selected-body read so far

        all_deliveries = service.deliver_scheduled_best(
            _phase(), delivered_at="2026-03-03T08:55:00+09:00"
        )
        self.assertEqual(set(all_deliveries), {"A0", "A1", "A2"})
        self.assertTrue(all(len(rows) == 2 for rows in all_deliveries.values()))
        payloads = service.interpretation_payloads(agent_id="A2", event_id="2026-03-03/am")
        overlap = next(
            item
            for item in payloads
            if item["post_id"] == first_post_id
            and item["content_level"] == "full_body"
        )
        self.assertEqual(overlap["body"], body_one)
        self.assertEqual(overlap["body_sha256"], _sha256(body_one))
        self.assertEqual(overlap["exposure_channel"], "selected_and_best_overlap")
        self.assertEqual(len(overlap["source_exposure_ids"]), 2)
        self.assertEqual(set(overlap["public_author_profile"]), {
            "schema_version",
            "public_badges",
            "public_direction",
            "public_reliability_score",
        })
        self.assertNotIn("author_agent_id", overlap)
        self.assertNotIn("portfolio", overlap["public_author_profile"])
        self.assertEqual(self._count("agent_exposures"), 1 + len(self.depths) * 2)

    def test_adversarial_full_body_hash_profile_and_depth_privacy_are_enforced(self) -> None:
        service = self._service(RN_COMM_ON)
        with self.assertRaises(CommunityContractError):
            service.run_pm_phase(
                _phase(),
                post_drafts=[
                    {
                        **_draft("A1", title="title only", body="full body", score=1),
                        "body_sha256": _sha256("title only"),
                    }
                ],
            )
        with self.assertRaises(CommunityContractError):
            RNCommunityService(
                service.memory,
                cohort_depths=self.depths,
                public_profiles={
                    **_profiles(),
                    "A2": {**_profiles()["A2"], "portfolio": {"cash": 999}},
                },
                community_policy=_policy(),
                community_phase_registry=_phase_registry(),
                community_timing_policy=_timing_policy(),
            )

        post = _draft("A1", title="Private test", body="This entire body is delivered.", score=1)
        # D0 cannot use selective reads, and a caller cannot attach a hidden
        # profile to a read request because the schema is exact.
        with self.assertRaises(CommunityContractError):
            service.run_pm_phase(
                _phase(),
                post_drafts=[post],
                selective_reads=[{"reader_agent_id": "A0", "post_id": "post:fake"}],
            )
        with self.assertRaises(CommunityContractError):
            service.run_pm_phase(
                _phase(),
                post_drafts=[post],
                selective_reads=[
                    {
                        "reader_agent_id": "A2",
                        "post_id": "post:fake",
                        "public_profile": {"portfolio": "leak"},
                    }
                ],
            )

    def test_forged_or_cross_agent_exposures_are_rejected_before_claim_edges(self) -> None:
        service = self._service(RN_COMM_ON)
        body = "FULL BODY used to produce a validated community claim."
        service.run_pm_phase(
            _phase(),
            post_drafts=[
                _draft("A1", title="Source", body=body, score=10)
            ],
            private_reader_traces=_reader_traces(
                service,
                _phase(),
                [_draft("A1", title="Source", body=body, score=10)],
            ),
        )
        deliveries = service.deliver_scheduled_best(_phase(), delivered_at="2026-03-03T08:55:00+09:00")
        a0_exposure = deliveries["A0"][0]["source_exposure_ids"][0]
        a1_exposure = deliveries["A1"][0]["source_exposure_ids"][0]

        with self.assertRaises(CommunityContractError):
            service.validate_claims_for_agent(
                agent_id="A1",
                event_id="2026-03-03/am",
                claims=[
                    {
                        "claim_text": "cross-agent leakage",
                        "stance": "neutral",
                        "source_exposure_ids": [a0_exposure],
                        "supporting_quote": "FULL BODY",
                    }
                ],
            )
        with self.assertRaises(CommunityContractError):
            service.validate_claims_for_agent(
                agent_id="A1",
                event_id="2026-03-03/am",
                claims=[
                    {
                        "claim_text": "forged source",
                        "stance": "neutral",
                        "source_exposure_ids": ["exp:forged"],
                        "supporting_quote": "forged",
                    }
                ],
            )

        claims = service.validate_claims_for_agent(
            agent_id="A1",
            event_id="2026-03-03/am",
            claims=[
                {
                    "claim_text": "The delivered full body supports a cautious view.",
                    "stance": "neutral",
                    "source_exposure_ids": [a1_exposure],
                    "supporting_quote": "FULL BODY",
                }
            ],
        )
        with connect(self.db_path, read_only=True) as connection:
            claim_payload = json.loads(
                connection.execute(
                    """
                    SELECT payload_json FROM observation_events
                    WHERE run_id = ? AND condition_id = ? AND event_id = ? AND stage = ?
                    """,
                    ("run-community", RN_COMM_ON, "2026-03-03/am", "community_claims:A1"),
                ).fetchone()["payload_json"]
            )
        self.assertEqual(
            set(claim_payload),
            {"schema_version", "agent_id", "event_id", "claims"},
        )
        evidence = service.stb_evidence_rows(
            agent_id="A1",
            event_id="2026-03-03/am",
            claims=claims,
        )
        with self.assertRaises(CommunityContractError):
            service.stb_evidence_rows(
                agent_id="A0",
                event_id="2026-03-03/am",
                claims=claims,
            )
        dimensions = {f"dim_{index}": f"belief {index}" for index in range(1, 7)}
        event_news = [
            service.memory.news_registry.articles[slot.article_id].stage_projection()
            for slot in service.memory.news_registry.slots_by_event["2026-03-03/am"]
        ]
        stb_id = service.memory.save_stb(
            agent_id="A1",
            event_id="2026-03-03/am",
            turn=3,
            date="2026-03-03",
            dimensions=dimensions,
            dimension_evidence={
                f"dim_{index}": {"support": [], "contradict": []}
                for index in range(1, 7)
            },
            current_evidence={
                "event_id": "2026-03-03/am",
                "date": "2026-03-03",
                "subturn": "am",
                "news": event_news,
                "depth2_search_results": [],
                "community_claims": [claim.stage_projection() for claim in claims],
            },
        )
        edges = service.attach_claims_to_stb(
            agent_id="A1",
            event_id="2026-03-03/am",
            stb_id=stb_id,
            claims=claims,
            dimensions_by_claim={claims[0].claim_id: ["dim_4"]},
        )
        self.assertEqual(len(edges), 1)
        # save_stb records one prompt-consumption edge for each sealed news /
        # claim source; attach_claims_to_stb adds the selected dimension edge.
        self.assertEqual(self._count("memory_evidence_edges"), 3)

    def test_final_pm_best_is_append_only_right_censored_not_delivered(self) -> None:
        service = self._service(RN_COMM_ON, final_schedule=True)
        drafts = [
            _draft("A1", title="Last", body="Final PM full body", score=2)
        ]
        checkpoint = service.run_pm_phase(
            _phase(final=True),
            post_drafts=drafts,
            private_reader_traces=_reader_traces(
                service,
                _phase(final=True),
                drafts,
            ),
        )
        self.assertTrue(checkpoint.right_censored)
        with connect(self.db_path, read_only=True) as connection:
            rows = connection.execute(
                "SELECT status, delivered_at FROM agent_exposures WHERE channel = 'community_best'"
            ).fetchall()
        self.assertEqual(len(rows), len(self.depths))
        self.assertTrue(all(str(row["status"]) == "right_censored" for row in rows))
        self.assertTrue(all(row["delivered_at"] is None for row in rows))
        with self.assertRaises(CommunityContractError):
            service.deliver_scheduled_best(_phase(final=True), delivered_at="2026-03-03T08:55:00+09:00")

    def test_adversarial_phase_identity_and_time_bypasses_are_rejected(self) -> None:
        service = self._service(RN_COMM_ON)
        post = _draft("A1", title="Timing", body="Full immutable body", score=2)

        invented = _phase()
        invented["phase_id"] = "invented-phase"
        with self.assertRaises(CommunityContractError):
            service.run_pm_phase(invented, post_drafts=[post])

        pre_pm = _phase()
        pre_pm["observed_at"] = "2026-02-27T00:01:00+09:00"
        with self.assertRaises(CommunityContractError):
            service.run_pm_phase(pre_pm, post_drafts=[post])

        falsely_censored = _phase(final=True)
        with self.assertRaises(CommunityContractError):
            service.run_pm_phase(falsely_censored, post_drafts=[post])

        service.run_pm_phase(
            _phase(),
            post_drafts=[post],
            private_reader_traces=_reader_traces(
                service,
                _phase(),
                [post],
            ),
        )
        with self.assertRaises(CommunityContractError):
            service.deliver_scheduled_best(_phase(), delivered_at="2026-03-03T23:59:00+09:00")

        with self.assertRaises(CommunityContractError):
            RNCommunityService(
                service.memory,
                cohort_depths=self.depths,
                public_profiles=_profiles(),
                community_policy=_policy(),
                community_phase_registry=_phase_registry(),
                community_timing_policy={
                    **_timing_policy(),
                    "pm_phase_not_before": "00:00:00",
                    "next_am_delivery_not_after": "23:59:59",
                },
            )


if __name__ == "__main__":
    unittest.main()
