from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from twinmarket_kr.db.schema import AGENTS_DDL
from twinmarket_kr.rn_ab.persona_snapshot import (
    PersonaSnapshotError,
    SealedPersonaSnapshot,
    assert_persona_snapshot_identity,
    build_persona_snapshot,
    parse_persona_v1,
)


def _row(agent_id: str, *, depth: int, cash: int, gender: str = "female") -> tuple[object, ...]:
    return (
        agent_id,
        f"source-{agent_id}",
        "ordinary",
        gender,
        21,
        "20대",
        "전남",
        "low",
        "low",
        "low",
        "medium",
        "value",
        0,
        '["전기전자", "반도체"]',
        cash,
        depth,
        "female_20대_일반",
        12,
        # A deliberately legacy-shaped blob: A001 is malformed without LFs,
        # and both rows can have an incorrect depth statement.  It must never
        # become the RN runtime prompt.
        "당신은 한국의 삼성전자 개인투자자입니다.뉴스는 당일 헤드라인과 10개 요약본을 모두 확인하는 요약 전독형입니다.",
    )


class PersonaSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "legacy.db"
        with sqlite3.connect(self.source) as connection:
            connection.execute(AGENTS_DDL)
            connection.executemany(
                """
                INSERT INTO agents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    _row("A001", depth=0, cash=100_000_000),
                    _row("A002", depth=1, cash=1_000_000_000, gender="male"),
                ],
            )
            connection.commit()

    def test_repairs_only_prompt_and_seals_round_trippable_snapshot(self) -> None:
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        artifacts = build_persona_snapshot(
            source_db_path=self.source,
            snapshot_dir=self.root / "snapshot",
            expected_agent_count=2,
            expected_depth_counts={0: 1, 1: 1},
        )
        self.assertEqual(before, hashlib.sha256(self.source.read_bytes()).hexdigest())
        sealed = SealedPersonaSnapshot.load(artifacts.snapshot_dir)
        self.assertEqual(set(sealed.personas), {"A001", "A002"})
        a001 = sealed.persona("A001")
        self.assertEqual(a001.news_depth, 0)
        self.assertTrue(a001.persona_prompt.endswith("\n"))
        self.assertNotIn("\r", a001.persona_prompt)
        self.assertEqual(parse_persona_v1(a001.persona_prompt)["news_depth"], 0)
        repair = json.loads(artifacts.repair_manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(repair["legacy_prompt_depth_mismatch_count"], 1)
        self.assertEqual(repair["post_repair_prompt_depth_mismatch_count"], 0)
        self.assertEqual(repair["depth_changed_agent_count"], 0)
        self.assertEqual(repair["non_prompt_structured_field_change_count"], 0)
        self.assertEqual(repair["canonical_roundtrip_count"], 2)

    def test_snapshot_load_rejects_prompt_tampering(self) -> None:
        artifacts = build_persona_snapshot(
            source_db_path=self.source,
            snapshot_dir=self.root / "snapshot",
            expected_agent_count=2,
        )
        with sqlite3.connect(artifacts.snapshot_db_path) as connection:
            connection.execute("UPDATE agents SET persona_prompt = 'tampered\\n' WHERE agent_id = 'A001'")
            connection.commit()
        with self.assertRaisesRegex(PersonaSnapshotError, "hash differs"):
            SealedPersonaSnapshot.load(artifacts.snapshot_dir)

    def test_arm_identity_requires_identical_prompt_maps(self) -> None:
        first = build_persona_snapshot(
            source_db_path=self.source,
            snapshot_dir=self.root / "snapshot-a",
            expected_agent_count=2,
        )
        second = build_persona_snapshot(
            source_db_path=self.source,
            snapshot_dir=self.root / "snapshot-b",
            expected_agent_count=2,
        )
        assert_persona_snapshot_identity(
            SealedPersonaSnapshot.load(first.snapshot_dir),
            SealedPersonaSnapshot.load(second.snapshot_dir),
        )

    def test_rejects_existing_destination_and_wrong_depth_requirements(self) -> None:
        with self.assertRaisesRegex(PersonaSnapshotError, "depth counts differ"):
            build_persona_snapshot(
                source_db_path=self.source,
                snapshot_dir=self.root / "bad-depth",
                expected_agent_count=2,
                expected_depth_counts={0: 2},
            )
        target = self.root / "exists"
        target.mkdir()
        with self.assertRaisesRegex(PersonaSnapshotError, "already exists"):
            build_persona_snapshot(
                source_db_path=self.source,
                snapshot_dir=target,
                expected_agent_count=2,
            )
