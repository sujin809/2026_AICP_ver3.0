from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from twinmarket_kr.experiment_runtime import (
    EventCheckpointRuntime,
    build_run_signature_payload,
    canonical_sha256,
    file_sha256,
)
from twinmarket_kr.llm.analysis import analyze_market, depth2_pre_search
from twinmarket_kr.llm.call_policy import (
    INTEGRATED_STAGE_MAX_TOKENS_V1,
    INTEGRATED_STAGE_SCHEMA_VERSIONS_V1,
)
from twinmarket_kr.llm.client import OpenRouterClient
from twinmarket_kr.llm.decision import build_trading_constraints, make_decision
from twinmarket_kr.llm.response_journal import (
    LogicalCallKey,
    ResponseJournal,
    ResponseJournalDriftError,
    canonical_sha256 as response_sha256,
    response_journal_scope,
)


VALID_PRE_SEARCH = {
    "key_findings": ["확인된 사실"],
    "curiosity_points": ["추가 확인"],
    "search_rationale": "근거 보강",
    "search_keywords": ["반도체"],
}

# Deliberately not alphabetical: accepted journal JSON is sorted before it is
# persisted, and the first caller must use the same representation before a
# downstream prompt creates its own journal request.
VALID_ANALYSIS = {
    "market_view": "가격 변동성은 제한적이다.",
    "valuation_view": "밸류에이션은 중립이다.",
    "technical_view": "추세는 관망 구간이다.",
    "news_view": "새 뉴스의 영향은 제한적이다.",
    "portfolio_view": "현금과 보유 수량을 함께 고려한다.",
    "key_risks": ["변동성 확대"],
    "opportunity": ["저가 매수"],
    "caution": ["과도한 집중"],
    "confidence": "medium",
    "directional_stance": "uncertain",
    "evidence_references": [
        {"source": "previous_ltb", "field": "dim_1"},
        {"source": "current_stb", "field": "dim_1"},
        {"source": "market", "field": "close"},
        {"source": "execution_state", "field": "available_cash"},
    ],
}

VALID_DECISION = {
    "action": "buy",
    "requested_quantity": 1,
    "reason": "제약 범위 안에서 소량 매수한다.",
    "risk_control": "단일 주문 한도를 지킨다.",
}


class FakeClient:
    model = "fixed-model"
    is_offline = True

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request_policy(self) -> dict[str, Any]:
        return {
            "reasoning": {"effort": "none", "exclude": True},
            "provider": {"only": ["fixed-provider"]},
        }

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"messages": messages, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected provider call")
        value = self.responses.pop(0)
        return {
            "choices": [
                {"message": {"content": json.dumps(value, ensure_ascii=False)}}
            ]
        }


class AcceptanceFakeClient(FakeClient):
    is_offline = False

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__(responses)
        self.acceptances: list[dict[str, str]] = []

    def record_experiment_acceptance(self, **kwargs: str) -> None:
        self.acceptances.append(dict(kwargs))


def _scope(
    journal: ResponseJournal,
    *,
    phase: str,
    agent_id: str = "A001",
):
    return response_journal_scope(
        journal=journal,
        run_id="run-1",
        condition_id="RN_COMM_ON",
        event_id="2026-02-27/AM",
        phase_attempt_id=phase,
        event_attempt_number=1 if phase == "phase-1" else 2,
        agent_id=agent_id,
    )


def _agent(prompt: str = "고정 페르소나") -> dict[str, Any]:
    return {
        "agent_id": "A001",
        "persona_prompt": prompt,
        "news_depth": 2,
    }


def _dimensions(prefix: str) -> dict[str, str]:
    return {f"dim_{index}": f"{prefix} {index}" for index in range(1, 7)}


def test_validated_response_replays_without_second_provider_call(
    tmp_path: Path,
) -> None:
    journal = ResponseJournal(
        tmp_path / "responses.sqlite",
        manifest_sha256="a" * 64,
    )
    first = FakeClient([VALID_PRE_SEARCH])
    with _scope(journal, phase="phase-1"):
        result = asyncio.run(
            depth2_pre_search(
                _agent(),
                {"articles": [{"id": "n1"}]},
                client=first,
                seed=7,
            )
        )
    assert result["generation_attempts"] == 1
    assert len(first.calls) == 1
    assert first.calls[0]["max_tokens"] == 1024
    assert first.calls[0]["logical_call_id"].startswith("llm:")

    replay_client = FakeClient([])
    with _scope(journal, phase="phase-2"):
        replayed = asyncio.run(
            depth2_pre_search(
                _agent(),
                {"articles": [{"id": "n1"}]},
                client=replay_client,
                seed=7,
            )
        )
    assert replayed == result
    assert replay_client.calls == []
    assert journal.summary()["logical_counts"] == {"accepted/pending": 1}


def test_canonical_provider_response_replays_through_downstream_decision(
    tmp_path: Path,
) -> None:
    """A replayed analysis must produce the exact same decision request."""

    journal = ResponseJournal(
        tmp_path / "responses.sqlite",
        manifest_sha256="9" * 64,
    )
    agent = _agent()
    previous_ltb = {"dimensions": _dimensions("previous")}
    current_stb = {"dimensions": _dimensions("current")}
    market_features = {"close": 100.0}
    constraints = build_trading_constraints(
        available_cash=1_000.0,
        current_quantity=5,
        current_price=100.0,
    )
    first = FakeClient([VALID_ANALYSIS, VALID_DECISION])
    with _scope(journal, phase="phase-1"):
        analysis = asyncio.run(
            analyze_market(
                agent,
                previous_ltb=previous_ltb,
                current_stb=current_stb,
                market_features=market_features,
                portfolio_summary="cash=1000, quantity=5",
                execution_state=constraints,
                client=first,
                seed=17,
            )
        )
        decision = asyncio.run(
            make_decision(
                agent,
                previous_ltb=previous_ltb,
                current_stb=current_stb,
                market_analysis=analysis,
                portfolio_summary="cash=1000, quantity=5",
                trading_constraints=constraints,
                client=first,
                seed=17,
                validation_attempts=1,
            )
        )
    assert len(first.calls) == 2
    assert decision["action"] == "buy"

    replay_client = FakeClient([])
    with _scope(journal, phase="phase-2"):
        replayed_analysis = asyncio.run(
            analyze_market(
                agent,
                previous_ltb=previous_ltb,
                current_stb=current_stb,
                market_features=market_features,
                portfolio_summary="cash=1000, quantity=5",
                execution_state=constraints,
                client=replay_client,
                seed=17,
            )
        )
        replayed_decision = asyncio.run(
            make_decision(
                agent,
                previous_ltb=previous_ltb,
                current_stb=current_stb,
                market_analysis=replayed_analysis,
                portfolio_summary="cash=1000, quantity=5",
                trading_constraints=constraints,
                client=replay_client,
                seed=17,
                validation_attempts=1,
            )
        )
    assert replayed_analysis == analysis
    assert replayed_decision == decision
    assert replay_client.calls == []


def test_request_drift_fails_before_provider_call(tmp_path: Path) -> None:
    journal = ResponseJournal(
        tmp_path / "responses.sqlite",
        manifest_sha256="b" * 64,
    )
    with _scope(journal, phase="phase-1"):
        asyncio.run(
            depth2_pre_search(
                _agent(),
                {"articles": [{"id": "n1"}]},
                client=FakeClient([VALID_PRE_SEARCH]),
                seed=7,
            )
        )
    drift_client = FakeClient([])
    with _scope(journal, phase="phase-2"):
        with pytest.raises(ResponseJournalDriftError, match="semantic request drift"):
            asyncio.run(
                depth2_pre_search(
                    _agent(prompt="변경된 페르소나"),
                    {"articles": [{"id": "n1"}]},
                    client=drift_client,
                    seed=7,
                )
            )
    assert drift_client.calls == []


def test_rejected_validation_attempt_is_not_replayed(tmp_path: Path) -> None:
    journal = ResponseJournal(
        tmp_path / "responses.sqlite",
        manifest_sha256="c" * 64,
    )
    first = FakeClient([{}, VALID_PRE_SEARCH])
    with _scope(journal, phase="phase-1"):
        result = asyncio.run(
            depth2_pre_search(
                _agent(),
                {"articles": [{"id": "n1"}]},
                client=first,
                seed=11,
            )
        )
    assert result["generation_attempts"] == 2
    assert len(first.calls) == 2
    assert journal.summary()["physical_attempt_counts"] == {
        "accepted": 1,
        "rejected": 1,
    }
    replay_client = FakeClient([])
    with _scope(journal, phase="phase-2"):
        replayed = asyncio.run(
            depth2_pre_search(
                _agent(),
                {"articles": [{"id": "n1"}]},
                client=replay_client,
                seed=11,
            )
        )
    assert replayed["generation_attempts"] == 2
    assert replay_client.calls == []


def test_only_final_validated_json_gets_experiment_acceptance(
    tmp_path: Path,
) -> None:
    journal = ResponseJournal(
        tmp_path / "responses.sqlite",
        manifest_sha256="e" * 64,
    )
    client = AcceptanceFakeClient([{}, VALID_PRE_SEARCH])
    with _scope(journal, phase="phase-1"):
        asyncio.run(
            depth2_pre_search(
                _agent(),
                {"articles": [{"id": "n1"}]},
                client=client,
                seed=23,
            )
        )
    assert len(client.acceptances) == 1
    assert client.acceptances[0]["accepted_response_sha256"] == response_sha256(
        VALID_PRE_SEARCH
    )
    assert client.acceptances[0]["logical_call_id"] == (
        client.calls[-1]["logical_call_id"]
    )


def _database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE scientific_state (event_id TEXT PRIMARY KEY)"
        )


def _signature(tmp_path: Path, database: Path) -> dict[str, Any]:
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "stage.txt").write_text("fixed", encoding="utf-8")
    sealed = tmp_path / "sealed.json"
    sealed.write_text('{"fixed":true}', encoding="utf-8")
    engine = tmp_path / "engine.py"
    engine.write_text("VALUE=1\n", encoding="utf-8")
    return build_run_signature_payload(
        parameters={"event_ids": ["2026-02-27/AM"]},
        input_files={"sealed": sealed},
        prompt_dir=prompt_dir,
        code_files=[engine],
        call_policy={"model": "fixed-model"},
        initial_database_sha256=file_sha256(database),
    )


def test_running_rollback_keeps_accepted_response_pending_and_commit_recovery_marks_it(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    database = run_dir / ".runtime" / "runtime.db"
    _database(database)
    signature = _signature(tmp_path, database)
    journal = ResponseJournal(
        run_dir / ".runtime" / "responses.sqlite",
        manifest_sha256=canonical_sha256(signature),
    )
    key = LogicalCallKey(
        run_id="run",
        condition_id="RN_COMM_ON",
        agent_id="A001",
        event_id="2026-02-27/AM",
        stage="short_term_belief",
        schema_version="integrated-stb-v1",
    )
    request = {"fixed": True}
    logical_id = journal.begin_attempt(
        key,
        request,
        phase_attempt_id="failed-phase",
        event_attempt_number=1,
        validation_attempt=1,
    )
    digest = journal.record_success(
        logical_id,
        {"accepted": True},
        phase_attempt_id="failed-phase",
        validation_attempt=1,
    )
    runtime = EventCheckpointRuntime(
        run_dir,
        runtime_db=database,
        event_ids=("2026-02-27/AM",),
        signature_payload=signature,
        response_journal=journal,
    )
    runtime.initialize_new()
    runtime.begin_event("2026-02-27/AM")
    runtime.pause_running_event(
        "2026-02-27/AM",
        RuntimeError("interrupted after accepted response"),
    )
    assert journal.summary()["logical_counts"] == {"accepted/pending": 1}

    runtime.begin_event("2026-02-27/AM")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO scientific_state(event_id) VALUES (?)",
            ("2026-02-27/AM",),
        )
    runtime.prepare_event_commit(
        "2026-02-27/AM",
        logical_response_digests={logical_id: digest},
    )
    resumed = EventCheckpointRuntime(
        run_dir,
        runtime_db=database,
        event_ids=("2026-02-27/AM",),
        signature_payload=signature,
        response_journal=journal,
    )
    checkpoint = resumed.open_for_resume()
    assert checkpoint["completed_events"] == ["2026-02-27/AM"]
    assert checkpoint["event_response_sha256"] == {
        "2026-02-27/AM": {logical_id: digest}
    }
    assert journal.summary()["logical_counts"] == {"accepted/committed": 1}


def test_response_journal_supports_concurrent_agent_writes(
    tmp_path: Path,
) -> None:
    journal = ResponseJournal(
        tmp_path / "responses.sqlite",
        manifest_sha256="d" * 64,
    )

    def write(index: int) -> tuple[str, str]:
        key = LogicalCallKey(
            run_id="run",
            condition_id="RN_COMM_ON",
            agent_id=f"A{index:03d}",
            event_id="2026-02-27/AM",
            stage="short_term_belief",
            schema_version="integrated-stb-v1",
        )
        request = {"agent": index}
        logical_id = journal.begin_attempt(
            key,
            request,
            phase_attempt_id=f"phase-{index}",
            event_attempt_number=1,
            validation_attempt=1,
        )
        digest = journal.record_success(
            logical_id,
            {"agent": index},
            phase_attempt_id=f"phase-{index}",
            validation_attempt=1,
        )
        return logical_id, digest

    with ThreadPoolExecutor(max_workers=8) as pool:
        expected = dict(pool.map(write, range(20)))
    assert journal.accepted_event_digests("2026-02-27/AM") == dict(
        sorted(expected.items())
    )


def test_response_journal_enforces_physical_attempt_foreign_key(
    tmp_path: Path,
) -> None:
    journal = ResponseJournal(
        tmp_path / "responses.sqlite",
        manifest_sha256="f" * 64,
    )
    with journal._connect() as connection, pytest.raises(
        sqlite3.IntegrityError
    ):
        connection.execute(
            """
            INSERT INTO physical_attempts (
                logical_call_id, phase_attempt_id, event_attempt_number,
                validation_attempt, status, created_at, updated_at
            ) VALUES ('missing', 'phase-1', 1, 1, 'started', 'now', 'now')
            """
        )


def test_integrated_stage_policy_covers_every_main_provider_label() -> None:
    expected = {
        "news_interpretation",
        "depth2_pre_search",
        "depth2_post_search",
        "short_term_belief",
        "market_analysis",
        "trading_decision",
        "post_fill_long_term_belief",
        "community_posting",
        "community_read_select",
        "community_read_react",
        "community_thinking",
    }
    assert set(INTEGRATED_STAGE_MAX_TOKENS_V1) == expected
    assert set(INTEGRATED_STAGE_SCHEMA_VERSIONS_V1) == expected
    assert all(value > 0 for value in INTEGRATED_STAGE_MAX_TOKENS_V1.values())


def test_integrated_live_client_rejects_missing_max_tokens() -> None:
    client = object.__new__(OpenRouterClient)
    client.offline = False
    client.model = "fixed-model"
    client.audit_context = {
        "artifact": "integrated_experiment_openrouter_attempt"
    }
    with pytest.raises(ValueError, match="explicit max_tokens"):
        asyncio.run(
            client.chat(
                [{"role": "user", "content": "{}"}],
                logical_call_id="logical",
                phase_attempt_id="phase",
            )
        )


def _load_runner_module() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "05_run_simulation.py"
    spec = importlib.util.spec_from_file_location("integrated_runner_for_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("max_agents", "max_days", "message"),
    [
        (0, None, "--max-agents"),
        (-1, None, "--max-agents"),
        (None, 0, "--max-days"),
        (None, -1, "--max-days"),
    ],
)
def test_runner_rejects_nonpositive_limits(
    max_agents: int | None,
    max_days: int | None,
    message: str,
) -> None:
    runner = _load_runner_module()
    args = SimpleNamespace(
        resume=False,
        run_dir=None,
        max_agents=max_agents,
        max_days=max_days,
        no_logs=False,
    )
    with pytest.raises(ValueError, match=message):
        asyncio.run(runner._run(args))


def test_runner_forbids_no_logs_for_online_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module()
    monkeypatch.delenv("TWINMARKET_OFFLINE_LLM", raising=False)
    args = SimpleNamespace(
        resume=False,
        run_dir=None,
        max_agents=1,
        max_days=1,
        no_logs=True,
    )
    with pytest.raises(ValueError, match="development-only"):
        asyncio.run(runner._run(args))


def test_event_retry_reseeds_only_unaccepted_calls(tmp_path: Path) -> None:
    """미수락 호출만 retry 계보로 재시드되고, 수락 호출은 정확히 replay된다.

    라이브 resume에서 base 시드를 통째로 바꾸는 이전 설계는 수락된 호출의
    replay까지 drift로 깨뜨려 100 agent 전원이 실패했다.
    """

    from twinmarket_kr.community.reading import community_reading_select

    journal = ResponseJournal(tmp_path / "journal.sqlite", manifest_sha256="b" * 64)
    posts = [{"post_id": 1, "title": "t", "post_type": "analysis"}]

    # event attempt 1: 항상 무효(post 999)만 내는 클라이언트 → 10회 소진
    bad = FakeClient([{"selected_post_ids": [999]} for _ in range(10)])
    with _scope(journal, phase="phase-1"):
        with pytest.raises(Exception):
            asyncio.run(
                community_reading_select(_agent(), posts, 5, client=bad, seed=7)
            )
    attempt1_seeds = [call["seed"] for call in bad.calls]
    assert len(attempt1_seeds) == 10

    # event attempt 2: 유효 응답 → retry 계보(새 논리 호출, 변주 시드)로 수락
    good = FakeClient([{"selected_post_ids": [1]}])
    with _scope(journal, phase="phase-2"):
        selected = asyncio.run(
            community_reading_select(_agent(), posts, 5, client=good, seed=7)
        )
    assert selected == [1]
    attempt2_seeds = [call["seed"] for call in good.calls]
    assert attempt2_seeds and attempt2_seeds[0] != attempt1_seeds[0]
    assert set(attempt2_seeds).isdisjoint(attempt1_seeds)

    with sqlite3.connect(journal.path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT schema_version, validation_status FROM logical_responses"
            " WHERE stage='community_read_select' ORDER BY schema_version"
        ).fetchall()
    versions = {str(r["schema_version"]): str(r["validation_status"]) for r in rows}
    assert len(versions) == 2
    base = [v for v in versions if "+retry" not in v][0]
    retry = [v for v in versions if v.endswith("+retry1")][0]
    assert versions[base] == "pending"      # 소진된 계보는 그대로 보존
    assert versions[retry] == "accepted"    # 새 계보에서 수락

    # event attempt 3에서는 수락된 retry1 계보가 그대로 replay돼야 한다.
    silent = FakeClient([])  # 호출되면 AssertionError
    scope3 = response_journal_scope(
        journal=journal,
        run_id="run-1",
        condition_id="RN_COMM_ON",
        event_id="2026-02-27/AM",
        phase_attempt_id="phase-3",
        event_attempt_number=3,
        agent_id="A001",
    )
    with scope3:
        replayed = asyncio.run(
            community_reading_select(_agent(), posts, 5, client=silent, seed=7)
        )
    assert replayed == [1]
    assert silent.calls == []


def test_event_retry_replays_accepted_base_call_without_drift(tmp_path: Path) -> None:
    """attempt 1에서 수락된 호출은 attempt 2에서 base key로 무료 replay된다."""

    from twinmarket_kr.community.reading import community_reading_select

    journal = ResponseJournal(tmp_path / "journal.sqlite", manifest_sha256="c" * 64)
    posts = [{"post_id": 1, "title": "t", "post_type": "analysis"}]
    good = FakeClient([{"selected_post_ids": [1]}])
    with _scope(journal, phase="phase-1"):
        assert asyncio.run(
            community_reading_select(_agent(), posts, 5, client=good, seed=7)
        ) == [1]

    silent = FakeClient([])
    with _scope(journal, phase="phase-2"):
        assert asyncio.run(
            community_reading_select(_agent(), posts, 5, client=silent, seed=7)
        ) == [1]
    assert silent.calls == []  # drift 없이 replay — 이전 설계에서는 여기서 실패
