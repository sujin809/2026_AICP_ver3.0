from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from twinmarket_kr.rn_ab.evidence_provider import (
    RNEvidenceProviderError,
    RNRunContextEvidenceProvider,
)
from twinmarket_kr.rn_ab.memory import EventSchedule
from twinmarket_kr.rn_ab.news import (
    SealedNewsRegistry,
    article_payload_sha256,
    bundle_content_sha256,
    fake_registry_sha256,
)
from twinmarket_kr.rn_ab.persona_snapshot import FrozenPersona, SealedPersonaSnapshot
from twinmarket_kr.rn_ab.run_context import RNRunContext
from twinmarket_kr.rn_ab.stage_inputs import SealedStageInputRegistry
from twinmarket_kr.rn_ab.stages import CurrentEvidencePacket, StageContractError


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class _NoClaims:
    def validate_claims_for_agent(self, *, agent_id, event_id, claims):
        if claims:
            raise AssertionError("unexpected claims")
        return ()


class RNRunContextEvidenceProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _context(self, *, observed_at: str = "2026-02-27T07:20:00+09:00") -> RNRunContext:
        schedule = EventSchedule.from_rows(
            [
                {
                    "event_id": "e1",
                    "turn": 1,
                    "date": "2026-02-27",
                    "subturn": "am",
                    "execution_price": 100,
                }
            ]
        )
        article = {
            "article_id": "real-1",
            "title": "Samsung operating update",
            "summary": "A sealed summary that D0 must never receive.",
            "published_at": "2026-02-27T07:00:00+09:00",
            "observed_at": observed_at,
            "last_modified_at": "2026-02-27T07:10:00+09:00",
            "source_url": "https://example.com/real-1",
            "source": "ExampleWire",
            "raw_body_sha256": _sha("raw"),
            "version_sha256": _sha("version"),
            "cutoff_version_sha256": _sha("cutoff"),
        }
        article["payload_sha256"] = article_payload_sha256(article)
        bundle = {
            "artifact_type": "real_news_bundle_manifest",
            "bundle_sha256": "0" * 64,
            "stock_code": "005930",
            "target_real_news_per_event": 1,
            "fake_news_per_event": 0,
            "articles": [article],
            "slots": [
                {
                    "event_id": "e1",
                    "slot_ordinal": 1,
                    "article_id": "real-1",
                    "payload_sha256": article["payload_sha256"],
                }
            ],
            "accepted_shortages": {},
            "known_fake_ids": [],
            "known_fake_payload_hashes": [],
            "fake_registry_sha256": fake_registry_sha256(
                known_fake_ids=[], known_fake_payload_hashes=[]
            ),
        }
        bundle["bundle_sha256"] = bundle_content_sha256(bundle)
        news = SealedNewsRegistry.from_mapping(bundle)
        stage_inputs = SealedStageInputRegistry.from_mapping(
            {
                "artifact_type": "rn_stage_input_registry",
                "version": "v1",
                "calendar_event_registry_sha256": _sha("calendar"),
                "events": [
                    {
                        "event_id": "e1",
                        "date": "2026-02-27",
                        "subturn": "am",
                        "news_cutoff_timestamp": "2026-02-27T09:00:00+09:00",
                        "market_feature_as_of": "2026-02-27T09:00:00+09:00",
                        "market": {
                            "reference_price": 100,
                            "previous_close": 99,
                            "open_price": 100,
                            "as_of_timestamp": "2026-02-27T09:00:00+09:00",
                        },
                    }
                ],
            }
        )
        personas = {
            agent_id: FrozenPersona(
                agent_id=agent_id,
                news_depth=depth,
                initial_cash=1_000_000,
                persona_prompt=f"sealed persona {agent_id}\n",
                persona_sha256=_sha(f"persona:{agent_id}"),
                structured_row_sha256=_sha(f"row:{agent_id}"),
            )
            for agent_id, depth in (("D0", 0), ("D1", 1), ("D2", 2))
        }
        snapshot = SealedPersonaSnapshot(
            snapshot_dir=self.root / "personas",
            snapshot_db_path=self.root / "personas" / "persona.sqlite",
            manifest_sha256=_sha("persona-manifest"),
            source_db_sha256=_sha("persona-source"),
            snapshot_db_sha256=_sha("persona-db"),
            prompt_map_sha256=_sha("persona-map"),
            depth_manifest_sha256=_sha("depth-map"),
            repair_manifest_sha256=_sha("repair"),
            personas=personas,
        )
        resolved = SimpleNamespace(
            agent_ids=("D0", "D1", "D2"),
            sha256=_sha("manifest"),
            spec=SimpleNamespace(belief_limits={}),
        )
        return RNRunContext(
            run_dir=self.root,
            run_id="run-evidence",
            resolved=resolved,
            evaluator_contract={},
            event_schedule=schedule,
            personas=snapshot,
            prompt_bundle=None,  # type: ignore[arg-type]
            news_registry=news,
            stage_inputs=stage_inputs,
            leakage_review=None,  # type: ignore[arg-type]
            initial_portfolios={agent: {"cash": 1_000_000, "quantity": 0} for agent in personas},
            condition_db_paths={},
            journal_paths={},
            source_hashes={},
        )

    def test_sealed_depth_projection_is_d0_headline_only_and_d1_d2_summary(self) -> None:
        provider = RNRunContextEvidenceProvider(self._context())

        async def collect():
            packets = []
            for agent_id in ("D0", "D1", "D2"):
                packets.append(await provider.current_evidence(
                    condition_id="RN_COMM_OFF", agent_id=agent_id, event_id="e1"
                ))
            return tuple(packets)

        d0, d1, d2 = asyncio.run(collect())
        self.assertEqual(d0.news_depth, 0)
        self.assertNotIn("summary", d0.news[0])
        self.assertEqual(d1.news_depth, 1)
        self.assertEqual(d2.news_depth, 2)
        self.assertEqual(d1.news[0]["summary"], d2.news[0]["summary"])
        self.assertEqual(provider.network_requests, 0)
        self.assertEqual(provider.paid_api_calls, 0)
        self.assertEqual(
            provider.missing_full_run_dependencies(),
            (
                "sealed_depth2_recent_search_registry_and_projection",
                "verified_run_local_community_lifecycle_for_RN_COMM_ON",
            ),
        )

    def test_d0_full_summary_and_late_snapshot_fail_closed(self) -> None:
        context = self._context()
        full_projection = context.news_registry.articles["real-1"].stage_projection(news_depth=1)
        with self.assertRaisesRegex(StageContractError, "exact D0 projection"):
            CurrentEvidencePacket(
                event_id="e1",
                date="2026-02-27",
                subturn="am",
                news=(full_projection,),
                community_claims=(),
                news_depth=0,
            )
        with self.assertRaisesRegex(StageContractError, "projection schema"):
            CurrentEvidencePacket.from_mapping(
                {
                    "event_id": "e1",
                    "date": "2026-02-27",
                    "subturn": "am",
                    "news": [full_projection],
                    "depth2_search_results": [],
                    "community_claims": [],
                },
                agent_id="D0",
                event_schedule=context.event_schedule,
                news_registry=context.news_registry,
                stage_input_registry=context.stage_inputs,
                claim_verifier=_NoClaims(),
                expected_news_depth=0,
            )

        late_provider = RNRunContextEvidenceProvider(
            self._context(observed_at="2026-02-27T10:00:00+09:00")
        )
        with self.assertRaisesRegex(RNEvidenceProviderError, "observed after"):
            asyncio.run(
                late_provider.current_evidence(
                    condition_id="RN_COMM_OFF", agent_id="D1", event_id="e1"
                )
            )

    def test_unknown_condition_agent_and_event_are_rejected(self) -> None:
        provider = RNRunContextEvidenceProvider(self._context())
        for arguments in (
            {"condition_id": "legacy", "agent_id": "D0", "event_id": "e1"},
            {"condition_id": "RN_COMM_OFF", "agent_id": "missing", "event_id": "e1"},
            {"condition_id": "RN_COMM_OFF", "agent_id": "D0", "event_id": "missing"},
        ):
            with self.assertRaises(RNEvidenceProviderError):
                asyncio.run(provider.current_evidence(**arguments))

    def test_unsupported_sealed_persona_depth_is_rejected_at_construction(self) -> None:
        context = self._context()
        bad_personas = dict(context.personas.personas)
        bad_personas["D2"] = replace(bad_personas["D2"], news_depth=3)
        bad_snapshot = replace(context.personas, personas=bad_personas)
        with self.assertRaisesRegex(RNEvidenceProviderError, "unsupported news depth"):
            RNRunContextEvidenceProvider(replace(context, personas=bad_snapshot))


if __name__ == "__main__":
    unittest.main()
