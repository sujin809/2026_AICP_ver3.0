from __future__ import annotations

import hashlib
import unittest

from twinmarket_kr.rn_ab.news import (
    NewsBundleError,
    SealedNewsRegistry,
    article_payload_sha256,
    bundle_content_sha256,
    fake_registry_sha256,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _bundle(*, title: str = "Samsung operating update", observed_at: str = "2026-02-27T07:30:00+09:00") -> dict:
    article = {
        "article_id": "real-1",
        "title": title,
        "summary": "A neutral operating update available before the session.",
        "published_at": "2026-02-27T07:00:00+09:00",
        "observed_at": observed_at,
        "last_modified_at": "2026-02-27T07:15:00+09:00",
        "source_url": "https://example.com/real-1",
        "source": "ExampleWire",
        "raw_body_sha256": _digest("raw-body"),
        "version_sha256": _digest("source-version"),
        "cutoff_version_sha256": _digest("cutoff-version"),
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
                "event_id": "2026-02-27/am",
                "slot_ordinal": 1,
                "article_id": article["article_id"],
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
    return bundle


class SealedNewsRegistryTests(unittest.TestCase):
    def test_invisible_character_cannot_bypass_target_leakage_scan(self) -> None:
        # U+200B used to split the Korean individual-flow phrase and evade the
        # raw regex.  Hashing a fully self-consistent bundle must not help.
        with self.assertRaisesRegex(NewsBundleError, "leakage"):
            SealedNewsRegistry.from_mapping(_bundle(title="개\u200b인 순매수 전망"))

    def test_required_snapshot_provenance_cannot_be_omitted(self) -> None:
        bundle = _bundle()
        del bundle["articles"][0]["raw_body_sha256"]
        bundle["bundle_sha256"] = bundle_content_sha256(bundle)
        with self.assertRaisesRegex(NewsBundleError, "provenance schema"):
            SealedNewsRegistry.from_mapping(bundle)

    def test_delivery_rejects_a_snapshot_observed_after_the_event_cutoff(self) -> None:
        bundle = _bundle(observed_at="2026-02-27T10:00:00+09:00")
        registry = SealedNewsRegistry.from_mapping(bundle)
        article = registry.articles["real-1"]
        with self.assertRaisesRegex(NewsBundleError, "observed after"):
            registry.validate_delivery(
                event_id="2026-02-27/am",
                delivered=[article.stage_projection()],
                cutoff_timestamp="2026-02-27T09:00:00+09:00",
            )


if __name__ == "__main__":
    unittest.main()
