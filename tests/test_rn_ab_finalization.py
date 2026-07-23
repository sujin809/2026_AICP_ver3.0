from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.test_rn_ab_preflight_bundle import RNPreflightBundleTests
from twinmarket_kr.rn_ab.finalization import (
    RNFinalizationError,
    expected_agent_event_keys,
    finalize_rn_run_artifacts,
    validate_rn_finalization_artifacts,
)
from twinmarket_kr.rn_ab.call_policy import (
    RN_STRICT_RESPONSE_FORMAT,
    RN_STRICT_TEMPERATURE,
)
from twinmarket_kr.rn_ab.community_provider import (
    COMMUNITY_CALL_MAX_TOKENS,
    POST_TYPES_GUIDE,
)
from twinmarket_kr.rn_ab.execution import strict_policy_from_context
from twinmarket_kr.rn_ab.journal import LogicalCallKey, ResponseJournal
from twinmarket_kr.rn_ab.memory import RN_AUXILIARY_STAGE_SCHEMA_VERSIONS
from twinmarket_kr.rn_ab.prompt_registry import COMMUNITY_POSTING_STAGE
from twinmarket_kr.rn_ab.run_context import RNRunContext
from twinmarket_kr.rn_ab.stage_adapter import (
    RN_TRUSTED_SYSTEM_INSTRUCTION_SHA256,
    RN_TRUSTED_SYSTEM_INSTRUCTION_VERSION,
)
from twinmarket_kr.rn_ab.spec import (
    RN_COMM_OFF,
    RN_COMM_ON,
    RN_CONDITIONS,
    canonical_sha256,
)


class _CompleteStore:
    def __init__(
        self,
        condition_id: str,
        manifest_sha256: str,
        phase_consumptions: dict[str, str],
        expected_keys: tuple[tuple[str, str], ...],
    ) -> None:
        self.condition_id = condition_id
        self.manifest_sha256 = manifest_sha256
        self.phase_consumptions = dict(phase_consumptions)
        self.expected_keys = expected_keys

    def assert_complete_lineage(self, expected_keys: object, *, require_finalized_outcomes: bool = True) -> dict[str, int]:
        keys = list(expected_keys)
        if not require_finalized_outcomes or keys != list(self.expected_keys):
            raise AssertionError("unexpected finalization lineage request")
        return {
            "expected_keys": len(keys),
            "paper_fill_ledger": len(keys),
            "finalized_outcomes": len(keys) * 3,
        }

    def phase_consumption_digests(self) -> dict[str, str]:
        return dict(self.phase_consumptions)

    def export_canonical_final_fill_ledger(
        self, path: Path | str, *, evaluator_contract_sha256: str
    ) -> Path:
        destination = Path(path)
        with destination.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["fill_id", "condition_id", "manifest_hash", "fill_status"],
            )
            writer.writeheader()
            for index, _key in enumerate(self.expected_keys):
                writer.writerow(
                    {
                        "fill_id": f"fill-{self.condition_id}-{index}",
                        "condition_id": self.condition_id,
                        "manifest_hash": evaluator_contract_sha256,
                        "fill_status": "filled",
                    }
                )
        return destination


def _committed_journal(
    path: Path,
    *,
    run_id: str,
    manifest: str,
    condition_id: str,
    agent_id: str,
    event_id: str,
) -> ResponseJournal:
    journal = ResponseJournal(path, manifest_sha256=manifest)
    key = LogicalCallKey(run_id, condition_id, agent_id, event_id, "stb", "v1")
    logical_id = journal.begin_attempt(key, {"input": "x"}, phase_attempt_id="phase-1", attempt_number=1)
    journal.record_success(logical_id, {"dim_1": "x"}, phase_attempt_id="phase-1", attempt_number=1)
    journal.mark_committed([logical_id])
    return journal


def _insert_private_post_trace(
    context: RNRunContext,
    *,
    journal: ResponseJournal,
    store: _CompleteStore,
) -> tuple[str, ...]:
    active_agents = tuple(
        agent_id
        for agent_id in context.agent_ids
        if context.personas.persona(agent_id).news_depth in {1, 2}
    )
    template_sha = context.prompt_bundle.support_template(
        COMMUNITY_POSTING_STAGE
    ).sha256
    policy = strict_policy_from_context(context)
    timing_sha = context.resolved.spec.community_timing_policy_sha256
    events = tuple(context.resolved.decision_events)
    by_id = {event.decision_event_id: event for event in events}
    phase_rows: list[tuple[dict[str, object], object]] = []
    for registered in context.resolved.calendar.community_phases:
        source = by_id[registered.after_event_id]
        next_am = next(
            (
                event
                for event in events
                if event.global_ordinal is not None
                and source.global_ordinal is not None
                and event.global_ordinal > source.global_ordinal
                and event.subturn.upper() == "AM"
            ),
            None,
        )
        phase_rows.append(
            (
                {
                    "phase_id": registered.phase_id,
                    "after_event": {
                        "event_id": source.decision_event_id,
                        "turn": source.global_ordinal,
                        "date": source.date,
                        "subturn": "pm",
                    },
                    "observed_at": source.decision_timestamp,
                    "next_am_event": (
                        None
                        if next_am is None
                        else {
                            "event_id": next_am.decision_event_id,
                            "turn": next_am.global_ordinal,
                            "date": next_am.date,
                            "subturn": "am",
                        }
                    ),
                },
                source,
            )
        )
    committed_ids: list[str] = []

    def insert_observation(
        connection: sqlite3.Connection,
        *,
        condition_id: str,
        event_id: str,
        turn: int,
        date: str,
        stage: str,
        payload: dict[str, object],
    ) -> None:
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            """
            INSERT INTO observation_events (
                observation_id, run_id, condition_id, event_id, turn, date,
                stage, payload_json, payload_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "obs:" + canonical_sha256(
                    {
                        "run_id": context.run_id,
                        "condition_id": condition_id,
                        "event_id": event_id,
                        "stage": stage,
                    }
                )[:40],
                context.run_id,
                condition_id,
                event_id,
                turn,
                date,
                stage,
                payload_json,
                canonical_sha256(payload),
            ),
        )

    with sqlite3.connect(context.condition_db_paths[RN_COMM_ON]) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        for phase, source in phase_rows:
            phase_id = str(phase["phase_id"])
            event_id = str(source.decision_event_id)
            turn = int(source.global_ordinal)
            date = str(source.date)
            for attempt, author in enumerate(active_agents, start=1):
                suffix = canonical_sha256(
                    {"phase_id": phase_id, "author": author}
                )[:16]
                ltb_id = f"ltb-fixture-{suffix}"
                fill_id = f"fill-fixture-{suffix}"
                ltb_dimensions = {
                    f"dim_{index}": f"{author} durable view {index}"
                    for index in range(1, 7)
                }
                ltb_sha = canonical_sha256(ltb_dimensions)
                view_change: list[dict[str, str]] = []
                view_change_sha = canonical_sha256(view_change)
                committed_fill = {
                    "action": "buy",
                    "requested_quantity": 1,
                    "filled_quantity": 1,
                    "executed_price": 70000.0,
                    "fee_amount": 0.0,
                    "pre_portfolio": {"cash": 1000000, "quantity": 0},
                    "post_portfolio": {"cash": 930000, "quantity": 1},
                }
                prompt_values = {
                    "persona_prompt": context.personas.persona(
                        author
                    ).persona_prompt,
                    "ltb_dimensions": ltb_dimensions,
                    "view_change": view_change,
                    "committed_pm_fill": committed_fill,
                    "date": date,
                    "post_types_guide": POST_TYPES_GUIDE,
                }
                seed_material = "|".join(
                    (
                        str(context.resolved.spec.study_seed),
                        context.resolved.spec.seed_namespace,
                        "community_posting",
                        author,
                        event_id,
                    )
                ).encode("utf-8")
                seed = int.from_bytes(
                    hashlib.sha256(seed_material).digest()[:4], "big"
                ) & 0x7FFFFFFF
                request = {
                    "contract_version": "rn-community-stage-request-v1",
                    "prompt_template_stage": COMMUNITY_POSTING_STAGE,
                    "prompt_template_sha256": template_sha,
                    "prompt_values": prompt_values,
                    "response_schema_version": RN_AUXILIARY_STAGE_SCHEMA_VERSIONS[
                        "community_posting"
                    ],
                    "trusted_system_instruction": {
                        "version": RN_TRUSTED_SYSTEM_INSTRUCTION_VERSION,
                        "sha256": RN_TRUSTED_SYSTEM_INSTRUCTION_SHA256,
                    },
                    "model": policy.model,
                    "provider": policy.provider,
                    "reasoning": {"effort": "none", "exclude": True},
                    "provider_policy": policy.as_request_policy(),
                    "temperature": RN_STRICT_TEMPERATURE,
                    "response_format": dict(RN_STRICT_RESPONSE_FORMAT),
                    "seed": seed,
                    "max_tokens": COMMUNITY_CALL_MAX_TOKENS[
                        "community_posting"
                    ],
                }
                key = LogicalCallKey(
                    context.run_id,
                    RN_COMM_ON,
                    author,
                    event_id,
                    "community_posting",
                    RN_AUXILIARY_STAGE_SCHEMA_VERSIONS[
                        "community_posting"
                    ],
                )
                logical_call_id = journal.begin_attempt(
                    key,
                    request,
                    phase_attempt_id=f"post-phase-{phase_id}",
                    attempt_number=attempt,
                )
                accepted_response_sha = journal.record_success(
                    logical_call_id,
                    {"will_post": False},
                    phase_attempt_id=f"post-phase-{phase_id}",
                    attempt_number=attempt,
                )
                journal.mark_committed([logical_call_id])
                store.phase_consumptions[
                    logical_call_id
                ] = accepted_response_sha
                committed_ids.append(logical_call_id)
                connection.execute(
                    """
                    INSERT INTO paper_ltb_states (
                        ltb_id, run_id, condition_id, manifest_sha256, agent_id,
                        event_id, turn, visible_from_turn, date, parent_ltb_id,
                        current_stb_id, dim_1, dim_2, dim_3, dim_4, dim_5, dim_6,
                        scientific_sha256, belief_summary, view_change_json,
                        human_log_renderer_version, human_log_renderer_sha256,
                        human_log_sha256
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        ltb_id,
                        context.run_id,
                        RN_COMM_ON,
                        context.manifest_sha256,
                        author,
                        event_id,
                        turn,
                        turn + 1,
                        date,
                        *(
                            ltb_dimensions[f"dim_{index}"]
                            for index in range(1, 7)
                        ),
                        ltb_sha,
                        "fixture",
                        json.dumps(
                            view_change,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "fixture",
                        "6" * 64,
                        "7" * 64,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO paper_fill_ledger (
                        fill_id, run_id, condition_id, manifest_sha256, agent_id,
                        event_id, turn, date, subturn, stock_code, action,
                        requested_quantity, filled_quantity, executed_price, fee_amount,
                        source_ltb_id, source_stb_id, decision_id,
                        pre_portfolio_json, post_portfolio_json, scientific_sha256
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, 'pm', '005930', 'buy',
                        1, 1, 70000.0, 0.0, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        fill_id,
                        context.run_id,
                        RN_COMM_ON,
                        context.manifest_sha256,
                        author,
                        event_id,
                        turn,
                        date,
                        ltb_id,
                        f"stb-{suffix}",
                        f"decision-{suffix}",
                        json.dumps(
                            committed_fill["pre_portfolio"],
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        json.dumps(
                            committed_fill["post_portfolio"],
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "8" * 64,
                    ),
                )
                identity = {
                    "run_id": context.run_id,
                    "condition_id": RN_COMM_ON,
                    "phase_id": phase_id,
                    "author_agent_id": author,
                }
                values = {
                    "trace_id": "post-trace:"
                    + canonical_sha256(identity)[:40],
                    **identity,
                    "manifest_sha256": context.manifest_sha256,
                    "event_id": event_id,
                    "turn": turn,
                    "date": date,
                    "eligibility_status": "eligible",
                    "posting_status": "skipped",
                    "post_id": None,
                    "ltb_id": ltb_id,
                    "ltb_sha256": ltb_sha,
                    "view_change_id": "view-change:"
                    + view_change_sha[:40],
                    "view_change_sha256": view_change_sha,
                    "fill_id": fill_id,
                    "prompt_template_sha256": template_sha,
                    "prompt_values_sha256": canonical_sha256(prompt_values),
                    "logical_call_id": logical_call_id,
                    "accepted_response_sha256": accepted_response_sha,
                    "title_sha256": None,
                    "body_sha256": None,
                }
                values["trace_sha256"] = canonical_sha256(values)
                columns = tuple(values)
                connection.execute(
                    f"INSERT INTO community_post_trace ({','.join(columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)})",
                    tuple(values[column] for column in columns),
                )

            base_payload = {
                "phase": phase,
                "community_timing_policy_sha256": timing_sha,
            }
            observations = (
                (
                    f"community_posts:{phase_id}",
                    {
                        "schema_version": "rn-community-posts-v1",
                        **base_payload,
                        "posts": [],
                    },
                ),
                (
                    f"community_selected:{phase_id}",
                    {
                        "schema_version": "rn-community-selected-v1",
                        **base_payload,
                        "selections": [],
                    },
                ),
                (
                    f"community_reader_trace:{phase_id}",
                    {
                        "schema_version": "rn-community-reader-trace-v1",
                        **base_payload,
                        "visible_from_event_id": (
                            None
                            if phase["next_am_event"] is None
                            else phase["next_am_event"]["event_id"]
                        ),
                        "readers": [
                            {
                                "reader_agent_id": agent_id,
                                "candidates": [],
                            }
                            for agent_id in active_agents
                        ],
                    },
                ),
                (
                    f"community_best_schedule:{phase_id}",
                    {
                        "schema_version": "rn-community-best-schedule-v1",
                        **base_payload,
                        "status": "empty",
                        "best_posts": [],
                        "audience_agent_ids": list(context.agent_ids),
                    },
                ),
                (
                    f"community_checkpoint:{phase_id}",
                    {
                        "schema_version": "rn-community-checkpoint-v1",
                        **base_payload,
                        "mode": "on",
                        "post_count": 0,
                        "selected_exposure_count": 0,
                        "best_post_ids": [],
                        "best_status": "empty",
                    },
                ),
            )
            for stage, payload in observations:
                insert_observation(
                    connection,
                    condition_id=RN_COMM_ON,
                    event_id=event_id,
                    turn=turn,
                    date=date,
                    stage=stage,
                    payload=payload,
                )
            next_am = phase["next_am_event"]
            if next_am is not None:
                delivered_at = next(
                    event.decision_timestamp
                    for event in events
                    if event.decision_event_id == next_am["event_id"]
                )
                insert_observation(
                    connection,
                    condition_id=RN_COMM_ON,
                    event_id=str(next_am["event_id"]),
                    turn=int(next_am["turn"]),
                    date=str(next_am["date"]),
                    stage=f"community_best_delivery:{phase_id}",
                    payload={
                        "schema_version": "rn-community-best-delivery-v1",
                        **base_payload,
                        "delivered_at": delivered_at,
                        "deliveries": [],
                    },
                )
        connection.commit()
    with sqlite3.connect(context.condition_db_paths[RN_COMM_OFF]) as connection:
        for phase, source in phase_rows:
            insert_observation(
                connection,
                condition_id=RN_COMM_OFF,
                event_id=str(source.decision_event_id),
                turn=int(source.global_ordinal),
                date=str(source.date),
                stage=f"community_checkpoint:{phase['phase_id']}",
                payload={
                    "schema_version": "rn-community-checkpoint-v1",
                    "mode": "off",
                    "phase": phase,
                    "post_count": 0,
                    "selected_exposure_count": 0,
                    "best_post_ids": [],
                    "best_status": "no_op",
                    "community_timing_policy_sha256": timing_sha,
                },
            )
        connection.commit()
    return tuple(committed_ids)


def _fixture_on_committed_count(context: RNRunContext) -> int:
    active = sum(
        context.personas.persona(agent_id).news_depth in {1, 2}
        for agent_id in context.agent_ids
    )
    return 1 + active * len(context.resolved.calendar.community_phases)


class RNFinalizationTests(RNPreflightBundleTests):
    def _context(self, run_id: str) -> RNRunContext:
        return RNRunContext.load(self._preflight(run_id=run_id).run_dir)

    def test_finalization_requires_complete_journals_and_writes_immutable_handoff(self) -> None:
        context = self._context("rn-finalization-complete")
        keys = expected_agent_event_keys(context)
        journals = {
            condition: _committed_journal(
                context.journal_paths[condition],
                run_id=context.run_id,
                manifest=context.manifest_sha256,
                condition_id=condition,
                agent_id=keys[0][0],
                event_id=keys[0][1],
            )
            for condition in RN_CONDITIONS
        }
        stores = {
            condition: _CompleteStore(
                condition,
                context.manifest_sha256,
                journals[condition].committed_accepted_response_digests(),
                keys,
            )
            for condition in RN_CONDITIONS
        }
        _insert_private_post_trace(
            context,
            journal=journals[RN_COMM_ON],
            store=stores[RN_COMM_ON],
        )
        artifacts = finalize_rn_run_artifacts(
            context,
            stores=stores,
            journals=journals,
            expected_committed_calls_by_condition={
                RN_COMM_OFF: 1,
                RN_COMM_ON: _fixture_on_committed_count(context),
            },
        )
        record = json.loads(artifacts.run_record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "local_integrity_passed_pending_evaluator_only_target_join")
        self.assertEqual(record["expected_agent_event_key_count"], len(keys))
        self.assertEqual(record["journals"][RN_COMM_OFF]["committed"], 1)
        self.assertEqual(
            record["lineage"][RN_COMM_OFF]["journal_phase_consumptions_verified"],
            1,
        )
        self.assertTrue(artifacts.export_index_path.is_file())
        private_path = artifacts.export_index_path.parent / "community_post_trace.jsonl"
        private_rows = [
            json.loads(line)
            for line in private_path.read_text(encoding="utf-8").splitlines()
        ]
        expected_private_rows = _fixture_on_committed_count(context) - 1
        self.assertEqual(len(private_rows), expected_private_rows)
        self.assertEqual(private_rows[0]["condition_id"], RN_COMM_ON)
        self.assertNotIn("created_at", private_rows[0])
        self.assertEqual(
            record["private_community_post_trace"]["row_count"],
            expected_private_rows,
        )
        self.assertEqual(
            record["community_mechanism_artifacts"][
                "community_interactions"
            ]["row_count"],
            0,
        )
        self.assertEqual(
            record["community_mechanism_artifacts"][
                "community_best_posts"
            ]["row_count"],
            0,
        )
        self.assertEqual(
            record["community_mechanism_artifacts"][
                "community_exposure_trace"
            ]["row_count"],
            0,
        )
        export_index = json.loads(
            artifacts.export_index_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            export_index["private_artifacts"]["community_post_trace"],
            record["private_community_post_trace"],
        )
        again = finalize_rn_run_artifacts(
            context,
            stores=stores,
            journals=journals,
            expected_committed_calls_by_condition={
                RN_COMM_OFF: 1,
                RN_COMM_ON: _fixture_on_committed_count(context),
            },
        )
        self.assertEqual(again, artifacts)
        self.assertEqual(
            validate_rn_finalization_artifacts(
                context,
                stores=stores,
                journals=journals,
                expected_committed_calls_by_condition={
                    RN_COMM_OFF: 1,
                    RN_COMM_ON: _fixture_on_committed_count(context),
                },
            ),
            artifacts,
        )
        private_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(
            RNFinalizationError,
            "private post trace|Private community post trace",
        ):
            validate_rn_finalization_artifacts(
                context,
                stores=stores,
                journals=journals,
                expected_committed_calls_by_condition={
                    RN_COMM_OFF: 1,
                    RN_COMM_ON: _fixture_on_committed_count(context),
                },
            )

    def test_finalization_rejects_pending_response_before_export(self) -> None:
        context = self._context("rn-finalization-pending")
        keys = expected_agent_event_keys(context)
        journals = {
            condition: _committed_journal(
                context.journal_paths[condition],
                run_id=context.run_id,
                manifest=context.manifest_sha256,
                condition_id=condition,
                agent_id=keys[0][0],
                event_id=keys[0][1],
            )
            for condition in RN_CONDITIONS
        }
        stores = {
            condition: _CompleteStore(
                condition,
                context.manifest_sha256,
                journals[condition].committed_accepted_response_digests(),
                keys,
            )
            for condition in RN_CONDITIONS
        }
        pending_key = LogicalCallKey(
            context.run_id,
            RN_COMM_ON,
            keys[1][0],
            keys[1][1],
            "stb",
            "v1",
        )
        journals[RN_COMM_ON].begin_attempt(
            pending_key, {"input": "pending"}, phase_attempt_id="phase-2", attempt_number=1
        )
        with self.assertRaisesRegex(RNFinalizationError, "not terminally committed"):
            finalize_rn_run_artifacts(
                context,
                stores=stores,
                journals=journals,
            )
        self.assertFalse((context.run_dir / "RUN_FINALIZATION.json").exists())

    def test_finalization_rejects_phase_consumption_set_or_hash_mismatch(self) -> None:
        mutations = ("forged_hash", "missing", "extra")
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation):
                context = self._context(f"rn-finalization-mismatch-{index}")
                keys = expected_agent_event_keys(context)
                journals = {
                    condition: _committed_journal(
                        context.journal_paths[condition],
                        run_id=context.run_id,
                        manifest=context.manifest_sha256,
                        condition_id=condition,
                        agent_id=keys[0][0],
                        event_id=keys[0][1],
                    )
                    for condition in RN_CONDITIONS
                }
                stores = {
                    condition: _CompleteStore(
                        condition,
                        context.manifest_sha256,
                        journals[condition].committed_accepted_response_digests(),
                        keys,
                    )
                    for condition in RN_CONDITIONS
                }
                target = stores[RN_COMM_ON].phase_consumptions
                logical_call_id = next(iter(target))
                if mutation == "forged_hash":
                    target[logical_call_id] = "c" * 64
                elif mutation == "missing":
                    del target[logical_call_id]
                else:
                    target[
                        f"{context.run_id}|RN_COMM_ON|agent-extra|event-1|stb|v1"
                    ] = "d" * 64

                with self.assertRaisesRegex(
                    RNFinalizationError,
                    "journal/phase consumption mismatch",
                ):
                    finalize_rn_run_artifacts(
                        context,
                        stores=stores,
                        journals=journals,
                    )
                self.assertFalse((context.run_dir / "RUN_FINALIZATION.json").exists())

    def test_finalization_rejects_forged_post_prompt_hash_or_false_response_public_post(
        self,
    ) -> None:
        for index, mutation in enumerate(("prompt_hash", "false_response_public_post")):
            with self.subTest(mutation=mutation):
                context = self._context(f"rn-finalization-post-binding-{index}")
                keys = expected_agent_event_keys(context)
                journals = {
                    condition: _committed_journal(
                        context.journal_paths[condition],
                        run_id=context.run_id,
                        manifest=context.manifest_sha256,
                        condition_id=condition,
                        agent_id=keys[0][0],
                        event_id=keys[0][1],
                    )
                    for condition in RN_CONDITIONS
                }
                stores = {
                    condition: _CompleteStore(
                        condition,
                        context.manifest_sha256,
                        journals[
                            condition
                        ].committed_accepted_response_digests(),
                        keys,
                    )
                    for condition in RN_CONDITIONS
                }
                _insert_private_post_trace(
                    context,
                    journal=journals[RN_COMM_ON],
                    store=stores[RN_COMM_ON],
                )
                with sqlite3.connect(
                    context.condition_db_paths[RN_COMM_ON]
                ) as connection:
                    if mutation == "prompt_hash":
                        connection.execute(
                            """
                            UPDATE community_post_trace
                            SET prompt_values_sha256 = ?
                            """,
                            ("f" * 64,),
                        )
                    else:
                        row = connection.execute(
                            """
                            SELECT author_agent_id, phase_id
                            FROM community_post_trace
                            ORDER BY phase_id, author_agent_id
                            LIMIT 1
                            """
                        ).fetchone()
                        public_post = {
                            "post_id": "post-forged",
                            "author_agent_id": str(row[0]),
                            "title": "forged",
                            "body": "forged",
                            "body_sha256": hashlib.sha256(
                                b"forged"
                            ).hexdigest(),
                            "content_version": 1,
                            "content_version_sha256": "e" * 64,
                            "post_type": "analysis",
                            "score": 0,
                            "like_count": 0,
                        }
                        payload = {
                            "schema_version": "rn-community-posts-v1",
                            "phase": {"phase_id": str(row[1])},
                            "posts": [public_post],
                        }
                        payload_json = json.dumps(
                            payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        connection.execute(
                            """
                            UPDATE observation_events
                            SET payload_json = ?, payload_sha256 = ?
                            WHERE stage = ?
                            """,
                            (
                                payload_json,
                                canonical_sha256(payload),
                                f"community_posts:{row[1]}",
                            ),
                        )
                    connection.commit()

                with self.assertRaisesRegex(
                    RNFinalizationError,
                    "prompt hashes|will_post=false|public posts",
                ):
                    finalize_rn_run_artifacts(
                        context,
                        stores=stores,
                        journals=journals,
                    )
                self.assertFalse(
                    (context.run_dir / "RUN_FINALIZATION.json").exists()
                )
