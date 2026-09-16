# v10 커뮤니티–belief 분석 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** v10 ON/OFF 2-arm 실행 결과에서 "뉴스·커뮤니티 글의 토픽 구조 → 에이전트 belief 변화 → 거래 방향"을 무과금·로컬 계산만으로 측정하는 분석 파이프라인을 만든다.

**Architecture:** 원본 run-dir은 읽기 전용으로만 접근하고, `analysis/community_belief_v10/` 아래에 자체 venv·스크립트·중간 parquet을 둔다. S0(패널) → S1(클러스터링) → S2(belief 연결) → S3(arm 대조) → S4(메커니즘) → S5(강건성) 순서로 각 단계가 앞 단계의 parquet만 소비한다. 모든 임베딩은 로컬 CPU에서 수행한다.

**Tech Stack:** Python 3.12, sqlite3(stdlib), pandas, pyarrow, numpy, scikit-learn, sentence-transformers(+torch, CPU), matplotlib, pytest

---

## Global Constraints

이 절의 제약은 **모든 Task에 암묵적으로 포함**된다.

1. **추가 과금 0원.** 어떤 유료 API도 호출하지 않는다. 사용자 지시(2026-08-03)이며 협상 대상이 아니다.
2. **`openai` 패키지를 analysis venv에 설치 금지.** import 자체가 불가능해야 한다.
3. 모든 스크립트는 첫 import로 `guard.py`를 부른다. `guard.py`는 `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `HF_TOKEN`을 `os.environ`에서 제거하고, `.env`를 로드하지 않는다.
4. 모델 최초 다운로드 이후 `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`을 고정한다.
5. **원본 run-dir(`outputs/logs/experiment_matrix_45day_v10/`)에 어떤 파일도 쓰지 않는다.** DB는 `sqlite3.connect(f"file:{path}?immutable=1", uri=True)`로만 연다 (WAL 상태라 `mode=ro`는 실패한다).
6. 루트 `.venv`와 루트 `requirements.txt`를 수정하지 않는다 (재봉인 이슈 회피). analysis venv는 `analysis/community_belief_v10/.venv`.
7. **커밋은 사용자 명시 승인이 필요하다** (AGENTS.md). 각 Task의 커밋 단계는 "승인 시 실행"이며, 승인 전에는 커밋하지 않고 변경만 남긴다.
8. 임베딩 모델은 `intfloat/multilingual-e5-small`, 입력에 `query: ` prefix, 벡터 L2 정규화.
9. 조용한 누락 금지: 분류되지 않은 ID, 예상과 다른 행 수, 빈 결과는 경고가 아니라 **예외로 실패**시킨다.
10. burn-in은 turn 1~6 (첫 3거래일 × AM/PM). 삭제하지 않고 `is_burnin` 플래그로만 표시한다.

**경로 상수** (모든 스크립트가 공유):

```python
REPO = Path("/Users/sujinjung/Desktop/felab/aicp/2026_AICP_ver3.0")
RUN_ROOT = REPO / "outputs/logs/experiment_matrix_45day_v10"
NEWS_JSON = REPO / "preparation/rn_ab_sealed_v1/news.json"
OUT = REPO / "analysis/community_belief_v10"
ARMS = ("RN_COMM_ON", "RN_COMM_OFF")
```

---

## 확인된 데이터 구조 (실측, 2026-08-03)

구현 시 이 구조를 가정해도 된다. 다르면 실패시킨다.

**`simulation_stb_states.evidence_json`** — 그 turn에 인용 가능한 근거 화이트리스트:

```json
{
  "news": [{"evidence_id": "news_20260306_종목_73d80d98", "kind": "news",
            "payload_sha256": "...", "published_at": "...", "title": "..."}],
  "community_claims": [{"evidence_id": "community_claim:A002:t005:01",
            "kind": "community_claim", "claim_text": "...", "stance": "bearish",
            "supporting_quote": "...", "source_exposure_ids": ["community:2026-03-03:t4:post:73:best_full_body:A002:delivered_t5"]}],
  "depth2_search_results": [{"evidence_id": "news_20260227_종목_a0bca00c",
            "kind": "depth2_recent_search", "title": "...", "summary": "...", "published_at": "..."}]
}
```

**`simulation_stb_states.dimension_evidence_json`** / **`simulation_ltb_states.integration_evidence_json`**:

```json
{"dim_1": {"support": ["news_..."], "contradict": ["news_..."]}, ..., 
 "dim_6": {"support": ["outcome:fill_A001_t010:h1"], "contradict": []}}
```

### ⚠️ 반드시 알아야 할 두 가지 함정

**함정 1 — `depth2_recent_search`의 evidence_id는 뉴스 article_id와 형태가 같다.**
`news_20260227_종목_a0bca00c`처럼 생겼다. **접두어로는 `news`와 `depth2_recent_search`를 구분할 수 없다.**
→ 반드시 그 (arm, agent_id, turn)의 `evidence_json` 화이트리스트를 조회해 `kind`를 결정한다.

**함정 2 — LTB에는 `evidence_json` 화이트리스트 열이 없다.**
LTB는 `integration_evidence_json`만 있다. 따라서 LTB의 ID는:
- `outcome:` 접두어 → `kind="outcome"`
- `community_claim:` 접두어 → `kind="community_claim"`
- 그 외 → **`source_stb_id`로 해당 STB 행을 조인해 그 STB의 화이트리스트로 `news` / `depth2_recent_search`를 판정**한다. 화이트리스트에 없으면 `kind="unknown"`으로 두고 Task 6의 게이트에서 실패시킨다.

**`community_logs.community_thinking`**:

```json
{"agreement_disagreement": "...",
 "claims": [{"claim_stance": "bullish", "claim_text": "...",
             "supporting_quote": "...", "supporting_quote_ref": 3,
             "source_exposure_ids": ["community:2026-02-27:t2:post:2:best_full_body:A001:delivered_t3"]}],
 "delivery_date": "..."}
```

**`source_exposure_ids` 형식**: `community:<source_date>:t<source_turn>:post:<post_id>:<relation>:<reader_agent>:delivered_t<delivery_turn>`
관계 값: `best_full_body`, `selected_full_body_replay`, `title_only` 등.

**실측 행 수** (Task 6 게이트 기준값):

| 테이블 | RN_COMM_ON | RN_COMM_OFF |
|---|---|---|
| `simulation_stb_states` | 9,000 | 9,000 |
| `simulation_ltb_states` | 9,100 | 9,100 |
| `simulation_decisions` / `simulation_fills` | 9,000 | 9,000 |
| `simulation_trade_outcomes` | 27,000 | 27,000 |
| `community_posts` | 3,150 (Best 225) | 0 |
| `community_interactions` | 15,745 | 0 |
| `community_logs` | 4,500 | 0 |

봉인 뉴스: `articles` 760, `slots` 760, 90 event.

---

## File Structure

| 파일 | 책임 |
|---|---|
| `analysis/community_belief_v10/guard.py` | 무과금 강제 (§Global 2~4). 모든 스크립트가 첫 줄에 import |
| `analysis/community_belief_v10/paths.py` | 경로 상수, arm 목록, burn-in 정의 |
| `analysis/community_belief_v10/dbio.py` | `immutable=1` DB 커넥션, 테이블 → DataFrame 로더 |
| `analysis/community_belief_v10/s0_beliefs.py` | belief_panel 생성 |
| `analysis/community_belief_v10/s0_evidence.py` | evidence_panel 생성 (kind 판정 포함) |
| `analysis/community_belief_v10/s0_community.py` | exposure/post/claim panel 생성 |
| `analysis/community_belief_v10/s0_news.py` | news_panel 생성 |
| `analysis/community_belief_v10/s0_actions.py` | action_panel 생성 |
| `analysis/community_belief_v10/s0_gate.py` | S0 검증 게이트 (실패 시 예외) |
| `analysis/community_belief_v10/embed.py` | 로컬 e5-small 임베딩 + 텍스트 해시 캐시 |
| `analysis/community_belief_v10/s1_cluster.py` | 뉴스+게시글 공동 군집, k 선택, 진단, 라벨 CSV 출력 |
| `analysis/community_belief_v10/s2_citation.py` | 1층 인용 집계 (토픽 × 차원) |
| `analysis/community_belief_v10/s2_movement.py` | 2층 Δ_semantic, Δ_topic |
| `analysis/community_belief_v10/s3_contrast.py` | ON/OFF paired 대조 + agent 클러스터 부트스트랩 |
| `analysis/community_belief_v10/s3_behavior.py` | 거래 방향 집계 + 실제 개인 순매수 비교 |
| `analysis/community_belief_v10/s4_mechanism.py` | 도달 대비 채택률, Best vs 비Best, 준외생 변이 |
| `analysis/community_belief_v10/s5_robustness.py` | 대체 모델·k·metric·burn-in·shortage 민감도 |
| `analysis/community_belief_v10/report.py` | REPORT.md 생성 |
| `analysis/community_belief_v10/tests/` | pytest. 소형 in-memory sqlite fixture 사용 |

**테스트 원칙:** 388MB 실 DB를 테스트에 쓰지 않는다. `tests/fixtures.py`가 동일 스키마의 in-memory sqlite를 만들어 3~5행짜리 소형 데이터로 검증한다. 실 DB는 Task 6의 게이트 스크립트에서만 통째로 읽는다.

---

## Task 1: 환경 구축과 무과금 강제

**Files:**
- Create: `analysis/community_belief_v10/guard.py`
- Create: `analysis/community_belief_v10/paths.py`
- Create: `analysis/community_belief_v10/requirements.txt`
- Test: `analysis/community_belief_v10/tests/test_guard.py`

**Interfaces:**
- Produces: `guard.enforce_no_paid_api() -> None`, `guard.assert_no_openai_package() -> None`
- Produces: `paths.REPO`, `paths.RUN_ROOT`, `paths.NEWS_JSON`, `paths.OUT`, `paths.ARMS`, `paths.BURNIN_TURNS = frozenset(range(1, 7))`

- [ ] **Step 1: venv 생성과 패키지 설치**

```bash
cd /Users/sujinjung/Desktop/felab/aicp/2026_AICP_ver3.0
mkdir -p analysis/community_belief_v10/tests
python3.12 -m venv analysis/community_belief_v10/.venv
cat > analysis/community_belief_v10/requirements.txt <<'EOF'
pandas>=2.0
pyarrow>=15.0
numpy>=1.26
scikit-learn>=1.4
sentence-transformers>=3.0
matplotlib>=3.8
pytest>=8.0
EOF
analysis/community_belief_v10/.venv/bin/pip install -r analysis/community_belief_v10/requirements.txt
```

`openai`는 이 목록에 없다. 절대 추가하지 않는다.

- [ ] **Step 2: 실패하는 테스트 작성**

`analysis/community_belief_v10/tests/test_guard.py`:

```python
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import guard


def test_enforce_removes_api_keys(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-should-be-removed")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-be-removed")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-removed")
    monkeypatch.setenv("HF_TOKEN", "hf-should-be-removed")
    guard.enforce_no_paid_api()
    for key in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "HF_TOKEN"):
        assert key not in os.environ


def test_enforce_sets_offline_flags(monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    guard.enforce_no_paid_api(offline=True)
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"


def test_openai_package_is_not_installed():
    """유료 SDK가 analysis venv에 들어오면 즉시 실패한다."""
    result = subprocess.run(
        [sys.executable, "-c", "import openai"], capture_output=True
    )
    assert result.returncode != 0, "openai 패키지가 analysis venv에 설치되어 있다"


def test_guard_does_not_load_dotenv(monkeypatch):
    """.env를 읽어 키를 되살리지 않아야 한다."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    guard.enforce_no_paid_api()
    assert "OPENROUTER_API_KEY" not in os.environ
```

- [ ] **Step 3: 테스트 실패 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_guard.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'guard'`

- [ ] **Step 4: guard.py 구현**

```python
"""무과금 강제. 모든 분석 스크립트가 첫 import로 이 모듈을 부른다.

이유: 사용자 지시(2026-08-03) — 이 분석은 어떤 유료 API도 호출하지 않는다.
.env를 절대 로드하지 않으며, 이미 환경에 있는 키도 제거한다.
"""

import os

PAID_KEY_NAMES = (
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "HF_TOKEN",
)


def enforce_no_paid_api(offline: bool = False) -> None:
    for name in PAID_KEY_NAMES:
        os.environ.pop(name, None)
    if offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"


def assert_no_openai_package() -> None:
    try:
        import openai  # noqa: F401
    except ImportError:
        return
    raise RuntimeError(
        "openai 패키지가 analysis venv에 설치되어 있다. "
        "무과금 제약 위반 가능성이 있으므로 제거할 것."
    )


enforce_no_paid_api()
```

- [ ] **Step 5: paths.py 구현**

```python
from pathlib import Path

REPO = Path("/Users/sujinjung/Desktop/felab/aicp/2026_AICP_ver3.0")
RUN_ROOT = REPO / "outputs/logs/experiment_matrix_45day_v10"
NEWS_JSON = REPO / "preparation/rn_ab_sealed_v1/news.json"
OUT = REPO / "analysis/community_belief_v10"
PANELS = OUT / "panels"
FIGURES = OUT / "figures"

ARMS = ("RN_COMM_ON", "RN_COMM_OFF")

# 첫 3거래일 × AM/PM. 삭제하지 않고 플래그로만 표시한다.
BURNIN_TURNS = frozenset(range(1, 7))

DIMS = tuple(f"dim_{i}" for i in range(1, 7))


def db_path(arm: str) -> Path:
    return RUN_ROOT / arm / ".runtime" / "runtime_sim.db"


def csv_path(arm: str, name: str) -> Path:
    return RUN_ROOT / arm / name
```

- [ ] **Step 6: 테스트 통과 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_guard.py -v`
Expected: 4 passed

- [ ] **Step 7: 모델 1회 다운로드 (여기서만 네트워크 사용)**

```bash
analysis/community_belief_v10/.venv/bin/python -c "
from sentence_transformers import SentenceTransformer
m = SentenceTransformer('intfloat/multilingual-e5-small')
print('downloaded', m.get_sentence_embedding_dimension())
"
```

Expected: `downloaded 384`
이후 모든 실행은 `HF_HUB_OFFLINE=1`로 돌아 네트워크를 쓰지 않는다.

- [ ] **Step 8: 커밋 (사용자 승인 시에만 실행)**

```bash
git add analysis/community_belief_v10/guard.py analysis/community_belief_v10/paths.py \
        analysis/community_belief_v10/requirements.txt analysis/community_belief_v10/tests/test_guard.py
git commit -m "analysis: v10 커뮤니티-belief 분석 환경 + 무과금 강제 guard"
```

---

## Task 2: DB 접근 계층과 테스트 fixture

**Files:**
- Create: `analysis/community_belief_v10/dbio.py`
- Create: `analysis/community_belief_v10/tests/fixtures.py`
- Test: `analysis/community_belief_v10/tests/test_dbio.py`

**Interfaces:**
- Consumes: `paths.db_path`
- Produces: `dbio.connect(db_file: Path) -> sqlite3.Connection`, `dbio.read_table(conn, name: str) -> pd.DataFrame`, `dbio.table_count(conn, name: str) -> int`
- Produces: `fixtures.make_run_db(tmp_path, arm: str, *, with_community: bool) -> Path` — 실제와 동일한 스키마의 소형 sqlite 생성

- [ ] **Step 1: 실패하는 테스트 작성**

`analysis/community_belief_v10/tests/test_dbio.py`:

```python
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dbio
from fixtures import make_run_db


def test_connect_is_readonly(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    conn = dbio.connect(db)
    with pytest.raises(Exception):
        conn.execute("insert into simulation_stb_states (stb_id) values ('x')")


def test_read_table_returns_all_rows(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    conn = dbio.connect(db)
    df = dbio.read_table(conn, "simulation_stb_states")
    assert len(df) == 4
    assert set(["stb_id", "agent_id", "turn", "dim_1", "evidence_json",
                "dimension_evidence_json"]).issubset(df.columns)


def test_table_count_matches(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_OFF", with_community=False)
    conn = dbio.connect(db)
    assert dbio.table_count(conn, "community_posts") == 0
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_dbio.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dbio'`

- [ ] **Step 3: fixtures.py 구현**

```python
"""실제 run DB와 같은 스키마의 소형 sqlite를 만든다.

실 DB(388MB)를 테스트에 쓰지 않기 위한 것이다. 컬럼 구성은
2026-08-03 실측 스키마를 그대로 따른다.
"""

import json
import sqlite3
from pathlib import Path

STB_COLS = ("stb_id, agent_id, turn, date, subturn, dim_1, dim_2, dim_3, dim_4, "
            "dim_5, dim_6, evidence_json, dimension_evidence_json, "
            "scientific_sha256, created_at")
LTB_COLS = ("ltb_id, agent_id, turn, visible_from_turn, date, subturn, parent_ltb_id, "
            "source_stb_id, source_decision_id, source_fill_id, dim_1, dim_2, dim_3, "
            "dim_4, dim_5, dim_6, integration_evidence_json, scientific_sha256, "
            "belief_summary, view_change_json, human_log_sha256, created_at")


def _whitelist(news_ids, claim_ids=(), search_ids=()):
    return json.dumps({
        "news": [{"evidence_id": i, "kind": "news", "title": f"t-{i}",
                  "published_at": "2026-03-06T09:00:00+09:00",
                  "payload_sha256": "x"} for i in news_ids],
        "community_claims": [{"evidence_id": i, "kind": "community_claim",
                              "claim_text": f"claim-{i}", "stance": "bullish",
                              "supporting_quote": f"quote-{i}",
                              "source_exposure_ids": [
                                  "community:2026-03-03:t4:post:73:best_full_body:A001:delivered_t5"]}
                             for i in claim_ids],
        "depth2_search_results": [{"evidence_id": i, "kind": "depth2_recent_search",
                                   "title": f"s-{i}", "summary": "s",
                                   "published_at": "2026-02-27T16:00:00+09:00",
                                   "payload_sha256": "y"} for i in search_ids],
    }, ensure_ascii=False)


def _dim_ev(mapping):
    """mapping: {"dim_1": (support_ids, contradict_ids), ...}"""
    out = {}
    for i in range(1, 7):
        key = f"dim_{i}"
        support, contradict = mapping.get(key, ([], []))
        out[key] = {"support": list(support), "contradict": list(contradict)}
    return json.dumps(out, ensure_ascii=False)


def make_run_db(tmp_path: Path, arm: str, *, with_community: bool) -> Path:
    db = tmp_path / f"{arm}.db"
    conn = sqlite3.connect(db)
    conn.execute(f"create table simulation_stb_states ({STB_COLS})")
    conn.execute(f"create table simulation_ltb_states ({LTB_COLS})")
    conn.execute("create table simulation_decisions "
                 "(decision_id, agent_id, turn, date, action, requested_quantity, "
                 "decision_json, analysis_id)")
    conn.execute("create table simulation_fills "
                 "(fill_id, agent_id, turn, date, filled_quantity, executed_price, "
                 "fee, decision_id)")
    conn.execute("create table community_posts "
                 "(post_id, agent_id, anonymous_code, turn, date, post_type, title, "
                 "content, like_count, unlike_count, score, is_best, source_ltb_id, "
                 "source_fill_id, source_decision_id)")
    conn.execute("create table community_interactions "
                 "(interaction_id, agent_id, post_id, turn, date, reaction)")
    conn.execute("create table community_logs "
                 "(log_id, agent_id, turn, date, best_posts_seen, posts_read, "
                 "community_thinking, candidate_posts_seen)")

    news_a, news_b = "news_20260306_종목_aaa", "news_20260306_경제_bbb"
    search_c = "news_20260227_종목_ccc"   # 검색 결과지만 뉴스와 형태가 같다
    claim_d = "community_claim:A001:t005:01"

    stb_rows = [
        # (agent, turn) = 두 에이전트 × 두 turn
        ("stb_A001_t005", "A001", 5, "2026-03-03", "AM", "d1", "d2", "d3", "d4", "d5", "d6",
         _whitelist([news_a, news_b], [claim_d] if with_community else [], [search_c]),
         _dim_ev({"dim_1": ([news_a], [news_b]),
                  "dim_4": ([claim_d] if with_community else [], []),
                  "dim_5": ([search_c], [])}), "h", "2026-03-03"),
        ("stb_A001_t006", "A001", 6, "2026-03-03", "PM", "d1b", "d2", "d3", "d4", "d5", "d6",
         _whitelist([news_a]), _dim_ev({"dim_1": ([news_a], [])}), "h", "2026-03-03"),
        ("stb_A002_t005", "A002", 5, "2026-03-03", "AM", "e1", "e2", "e3", "e4", "e5", "e6",
         _whitelist([news_a]), _dim_ev({"dim_2": ([news_a], [])}), "h", "2026-03-03"),
        ("stb_A002_t006", "A002", 6, "2026-03-03", "PM", "e1", "e2", "e3", "e4", "e5", "e6",
         _whitelist([news_a]), _dim_ev({"dim_2": ([news_a], [])}), "h", "2026-03-03"),
    ]
    conn.executemany(
        f"insert into simulation_stb_states ({STB_COLS}) values ({','.join('?' * 15)})",
        stb_rows)

    ltb_rows = [
        ("ltb_A001_t005", "A001", 5, 6, "2026-03-03", "AM", None, "stb_A001_t005",
         "dec_A001_t005", "fill_A001_t005", "L1", "L2", "L3", "L4", "L5", "L6",
         _dim_ev({"dim_1": ([news_a], []),
                  "dim_6": (["outcome:fill_A001_t002:h1"], [])}),
         "h", "요약", "{}", "h", "2026-03-03"),
        ("ltb_A001_t006", "A001", 6, 7, "2026-03-03", "PM", "ltb_A001_t005",
         "stb_A001_t006", "dec_A001_t006", "fill_A001_t006",
         "L1b", "L2", "L3", "L4", "L5", "L6",
         _dim_ev({"dim_1": ([news_a], [])}), "h", "요약", "{}", "h", "2026-03-03"),
    ]
    conn.executemany(
        f"insert into simulation_ltb_states ({LTB_COLS}) values ({','.join('?' * 22)})",
        ltb_rows)

    conn.executemany(
        "insert into simulation_decisions values (?,?,?,?,?,?,?,?)",
        [("dec_A001_t005", "A001", 5, "2026-03-03", "buy", 10, "{}", "an_1"),
         ("dec_A002_t005", "A002", 5, "2026-03-03", "sell", 5, "{}", "an_2")])
    conn.executemany(
        "insert into simulation_fills values (?,?,?,?,?,?,?,?)",
        [("fill_A001_t005", "A001", 5, "2026-03-03", 10, 70000.0, 0.0, "dec_A001_t005"),
         ("fill_A002_t005", "A002", 5, "2026-03-03", 5, 70000.0, 0.0, "dec_A002_t005")])

    if with_community:
        conn.execute(
            "insert into community_posts values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("73", "A003", "곰-1801", 4, "2026-03-03", "trade_share", "제목",
             "본문 첫 문장. 본문 둘째 문장.", 3, 1, 2, 1,
             "ltb_A003_t004", "fill_A003_t004", "dec_A003_t004"))
        conn.execute(
            "insert into community_interactions values (?,?,?,?,?,?)",
            ("i1", "A001", "73", 4, "2026-03-03", "like"))
        thinking = json.dumps({
            "agreement_disagreement": "동의",
            "delivery_date": "2026-03-03",
            "claims": [{"claim_stance": "bullish", "claim_text": "claim-1",
                        "supporting_quote": "본문 첫 문장.", "supporting_quote_ref": 1,
                        "source_exposure_ids": [
                            "community:2026-03-03:t4:post:73:best_full_body:A001:delivered_t5"]}],
        }, ensure_ascii=False)
        conn.execute("insert into community_logs values (?,?,?,?,?,?,?,?)",
                     ("l1", "A001", 5, "2026-03-03", "[]", "[]", thinking, "[]"))

    conn.commit()
    conn.close()
    return db
```

- [ ] **Step 4: dbio.py 구현**

```python
import sqlite3
from pathlib import Path

import pandas as pd

import guard  # noqa: F401  무과금 강제

guard.enforce_no_paid_api()


def connect(db_file: Path) -> sqlite3.Connection:
    """원본 DB를 읽기 전용으로 연다.

    run DB는 WAL 상태라 mode=ro로는 열리지 않는다(실측 2026-08-03).
    immutable=1을 써야 하며, 이는 원본을 절대 수정하지 않겠다는 보증이기도 하다.
    """
    if not db_file.exists():
        raise FileNotFoundError(f"DB가 없다: {db_file}")
    return sqlite3.connect(f"file:{db_file}?immutable=1", uri=True)


def read_table(conn: sqlite3.Connection, name: str) -> pd.DataFrame:
    return pd.read_sql_query(f"select * from {name}", conn)


def table_count(conn: sqlite3.Connection, name: str) -> int:
    return int(conn.execute(f"select count(*) from {name}").fetchone()[0])
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_dbio.py -v`
Expected: 3 passed

- [ ] **Step 6: 실 DB로 연결 스모크 확인**

```bash
analysis/community_belief_v10/.venv/bin/python -c "
import sys; sys.path.insert(0, 'analysis/community_belief_v10')
import dbio, paths
for arm in paths.ARMS:
    c = dbio.connect(paths.db_path(arm))
    print(arm, dbio.table_count(c, 'simulation_stb_states'), dbio.table_count(c, 'community_posts'))
"
```

Expected:
```
RN_COMM_ON 9000 3150
RN_COMM_OFF 9000 0
```

- [ ] **Step 7: 커밋 (사용자 승인 시에만 실행)**

```bash
git add analysis/community_belief_v10/dbio.py analysis/community_belief_v10/tests/
git commit -m "analysis: 읽기 전용 DB 접근 계층 + 소형 fixture"
```

---

## Task 3: belief_panel

**Files:**
- Create: `analysis/community_belief_v10/s0_beliefs.py`
- Test: `analysis/community_belief_v10/tests/test_s0_beliefs.py`

**Interfaces:**
- Consumes: `dbio.connect`, `dbio.read_table`, `paths.ARMS`, `paths.DIMS`, `paths.BURNIN_TURNS`
- Produces: `s0_beliefs.build_belief_panel(conn, arm: str) -> pd.DataFrame`
  컬럼: `arm, agent_id, turn, date, layer, dim, text, state_id, source_stb_id, is_burnin`
  `layer` ∈ {`STB`, `LTB`}, `dim` ∈ {`dim_1`..`dim_6`}
- Produces: `s0_beliefs.main() -> None` — 두 arm을 처리해 `panels/belief_panel.parquet` 저장

- [ ] **Step 1: 실패하는 테스트 작성**

`analysis/community_belief_v10/tests/test_s0_beliefs.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dbio
import s0_beliefs
from fixtures import make_run_db


def test_panel_is_long_format_one_row_per_dim(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_beliefs.build_belief_panel(dbio.connect(db), "RN_COMM_ON")
    # STB 4행 × 6차원 + LTB 2행 × 6차원
    assert len(df) == 4 * 6 + 2 * 6
    assert set(df["dim"]) == {f"dim_{i}" for i in range(1, 7)}
    assert set(df["layer"]) == {"STB", "LTB"}


def test_burnin_flag_covers_turns_1_to_6(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_beliefs.build_belief_panel(dbio.connect(db), "RN_COMM_ON")
    assert df.loc[df["turn"] == 5, "is_burnin"].all()
    assert df.loc[df["turn"] == 6, "is_burnin"].all()


def test_ltb_keeps_source_stb_id_for_kind_lookup(tmp_path):
    """LTB에는 화이트리스트가 없으므로 source_stb_id가 반드시 보존돼야 한다."""
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_beliefs.build_belief_panel(dbio.connect(db), "RN_COMM_ON")
    ltb = df[df["layer"] == "LTB"]
    assert ltb["source_stb_id"].notna().all()
    stb = df[df["layer"] == "STB"]
    assert stb["source_stb_id"].isna().all()


def test_text_is_carried_verbatim(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_beliefs.build_belief_panel(dbio.connect(db), "RN_COMM_ON")
    row = df[(df["layer"] == "STB") & (df["agent_id"] == "A001")
             & (df["turn"] == 5) & (df["dim"] == "dim_1")]
    assert row["text"].iloc[0] == "d1"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s0_beliefs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 's0_beliefs'`

- [ ] **Step 3: 구현**

```python
"""belief_panel: agent × turn × layer × dim 하나당 한 행.

STB(당일 정보 반응)와 LTB(사후 통합)는 의미가 다르므로 layer 열로 구분만 하고
절대 합산하지 않는다. dim_6은 STB와 LTB의 의미 자체가 다르므로
(STB=정보 한계, LTB=누적 자기평가) 층 간 비교를 금지한다 —
ANALYSIS_FIELD_GUIDE.md §1 참조.
"""

import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api()

import dbio
import paths


def _melt(df: pd.DataFrame, arm: str, layer: str, state_col: str) -> pd.DataFrame:
    keep = ["agent_id", "turn", "date", state_col]
    if layer == "LTB":
        keep.append("source_stb_id")
    out = df[keep + list(paths.DIMS)].melt(
        id_vars=keep, value_vars=list(paths.DIMS),
        var_name="dim", value_name="text")
    out = out.rename(columns={state_col: "state_id"})
    if layer == "STB":
        out["source_stb_id"] = pd.NA
    out.insert(0, "layer", layer)
    out.insert(0, "arm", arm)
    return out


def build_belief_panel(conn, arm: str) -> pd.DataFrame:
    stb = dbio.read_table(conn, "simulation_stb_states")
    ltb = dbio.read_table(conn, "simulation_ltb_states")
    panel = pd.concat(
        [_melt(stb, arm, "STB", "stb_id"), _melt(ltb, arm, "LTB", "ltb_id")],
        ignore_index=True)
    panel["is_burnin"] = panel["turn"].isin(paths.BURNIN_TURNS)
    cols = ["arm", "agent_id", "turn", "date", "layer", "dim", "text",
            "state_id", "source_stb_id", "is_burnin"]
    return panel[cols].sort_values(
        ["arm", "agent_id", "turn", "layer", "dim"]).reset_index(drop=True)


def main() -> None:
    paths.PANELS.mkdir(parents=True, exist_ok=True)
    frames = [build_belief_panel(dbio.connect(paths.db_path(arm)), arm)
              for arm in paths.ARMS]
    panel = pd.concat(frames, ignore_index=True)
    expected = (9000 + 9100) * 6 * len(paths.ARMS)
    if len(panel) != expected:
        raise AssertionError(f"belief_panel 행 수 {len(panel)} != 기대 {expected}")
    panel.to_parquet(paths.PANELS / "belief_panel.parquet", index=False)
    print(f"belief_panel {len(panel):,}행 저장")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s0_beliefs.py -v`
Expected: 4 passed

- [ ] **Step 5: 실 데이터로 실행**

Run: `analysis/community_belief_v10/.venv/bin/python analysis/community_belief_v10/s0_beliefs.py`
Expected: `belief_panel 217,200행 저장`

- [ ] **Step 6: 커밋 (사용자 승인 시에만 실행)**

```bash
git add analysis/community_belief_v10/s0_beliefs.py analysis/community_belief_v10/tests/test_s0_beliefs.py
git commit -m "analysis: belief_panel 생성 (STB/LTB × 6차원 long format)"
```

---

## Task 4: evidence_panel — kind 판정이 핵심

**Files:**
- Create: `analysis/community_belief_v10/s0_evidence.py`
- Test: `analysis/community_belief_v10/tests/test_s0_evidence.py`

**Interfaces:**
- Consumes: `dbio`, `paths`
- Produces: `s0_evidence.build_whitelist(conn) -> dict[tuple[str, int], dict[str, str]]`
  `(agent_id, turn) -> {evidence_id: kind}`
- Produces: `s0_evidence.classify(evidence_id: str, whitelist: dict[str, str]) -> str`
  반환값 ∈ {`news`, `community_claim`, `depth2_recent_search`, `outcome`, `unknown`}
- Produces: `s0_evidence.build_evidence_panel(conn, arm) -> pd.DataFrame`
  컬럼: `arm, agent_id, turn, layer, dim, relation, evidence_id, kind, state_id`
  `relation` ∈ {`support`, `contradict`}
- Produces: `s0_evidence.main() -> None` → `panels/evidence_panel.parquet`

- [ ] **Step 1: 실패하는 테스트 작성**

`analysis/community_belief_v10/tests/test_s0_evidence.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dbio
import s0_evidence
from fixtures import make_run_db


def test_search_result_is_not_misread_as_news(tmp_path):
    """검색 결과의 evidence_id는 뉴스 article_id와 형태가 같다.
    화이트리스트를 봐야만 구분할 수 있다."""
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_evidence.build_evidence_panel(dbio.connect(db), "RN_COMM_ON")
    row = df[df["evidence_id"] == "news_20260227_종목_ccc"]
    assert len(row) == 1
    assert row["kind"].iloc[0] == "depth2_recent_search"


def test_news_is_classified_as_news(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_evidence.build_evidence_panel(dbio.connect(db), "RN_COMM_ON")
    kinds = set(df.loc[df["evidence_id"] == "news_20260306_종목_aaa", "kind"])
    assert kinds == {"news"}


def test_community_claim_prefix(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_evidence.build_evidence_panel(dbio.connect(db), "RN_COMM_ON")
    row = df[df["evidence_id"] == "community_claim:A001:t005:01"]
    assert row["kind"].iloc[0] == "community_claim"
    assert row["dim"].iloc[0] == "dim_4"


def test_ltb_outcome_id_classified(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_evidence.build_evidence_panel(dbio.connect(db), "RN_COMM_ON")
    row = df[df["evidence_id"] == "outcome:fill_A001_t002:h1"]
    assert row["kind"].iloc[0] == "outcome"
    assert row["layer"].iloc[0] == "LTB"


def test_ltb_news_id_resolved_via_source_stb(tmp_path):
    """LTB에는 화이트리스트가 없다. source_stb_id로 STB 화이트리스트를 봐야 한다."""
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_evidence.build_evidence_panel(dbio.connect(db), "RN_COMM_ON")
    ltb_news = df[(df["layer"] == "LTB")
                  & (df["evidence_id"] == "news_20260306_종목_aaa")]
    assert len(ltb_news) >= 1
    assert set(ltb_news["kind"]) == {"news"}


def test_relations_are_preserved(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_evidence.build_evidence_panel(dbio.connect(db), "RN_COMM_ON")
    contradict = df[(df["evidence_id"] == "news_20260306_경제_bbb")
                    & (df["layer"] == "STB")]
    assert contradict["relation"].iloc[0] == "contradict"


def test_unknown_kind_is_reported_not_silently_dropped(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    wl = {"news_x": "news"}
    assert s0_evidence.classify("news_unlisted", wl) == "unknown"
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s0_evidence.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 's0_evidence'`

- [ ] **Step 3: 구현**

```python
"""evidence_panel: belief가 인용한 근거 1건 = 1행.

kind 판정의 두 함정 (2026-08-03 실측):

1. depth2_recent_search의 evidence_id는 뉴스 article_id와 형태가 같다
   (예: news_20260227_종목_a0bca00c). 접두어로는 구분 불가하므로
   반드시 그 (agent_id, turn)의 evidence_json 화이트리스트를 본다.
2. LTB에는 evidence_json 열이 없다. source_stb_id로 STB 행을 조인해
   그 화이트리스트를 빌려 쓴다. outcome:/community_claim: 은 접두어로 판정한다.
"""

import json

import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api()

import dbio
import paths

WHITELIST_KEYS = ("news", "community_claims", "depth2_search_results")


def build_whitelist(conn) -> dict:
    """(agent_id, turn) -> {evidence_id: kind}"""
    out = {}
    by_stb_id = {}
    rows = conn.execute(
        "select stb_id, agent_id, turn, evidence_json from simulation_stb_states")
    for stb_id, agent_id, turn, ev_json in rows:
        mapping = {}
        payload = json.loads(ev_json) if ev_json else {}
        for key in WHITELIST_KEYS:
            for item in payload.get(key) or []:
                mapping[item["evidence_id"]] = item["kind"]
        out[(agent_id, int(turn))] = mapping
        by_stb_id[stb_id] = mapping
    out["__by_stb_id__"] = by_stb_id
    return out


def classify(evidence_id: str, whitelist: dict) -> str:
    if evidence_id.startswith("outcome:"):
        return "outcome"
    if evidence_id.startswith("community_claim:"):
        return "community_claim"
    return whitelist.get(evidence_id, "unknown")


def _explode(ev_json: str, layer: str, meta: dict, whitelist: dict) -> list:
    rows = []
    payload = json.loads(ev_json) if ev_json else {}
    for dim in paths.DIMS:
        entry = payload.get(dim) or {}
        for relation in ("support", "contradict"):
            for evidence_id in entry.get(relation) or []:
                rows.append({**meta, "layer": layer, "dim": dim,
                             "relation": relation, "evidence_id": evidence_id,
                             "kind": classify(evidence_id, whitelist)})
    return rows


def build_evidence_panel(conn, arm: str) -> pd.DataFrame:
    whitelists = build_whitelist(conn)
    by_stb_id = whitelists["__by_stb_id__"]
    rows = []

    stb = conn.execute("select stb_id, agent_id, turn, dimension_evidence_json "
                       "from simulation_stb_states")
    for stb_id, agent_id, turn, dim_json in stb:
        meta = {"arm": arm, "agent_id": agent_id, "turn": int(turn),
                "state_id": stb_id}
        rows += _explode(dim_json, "STB", meta,
                         whitelists.get((agent_id, int(turn)), {}))

    ltb = conn.execute("select ltb_id, agent_id, turn, source_stb_id, "
                       "integration_evidence_json from simulation_ltb_states")
    for ltb_id, agent_id, turn, source_stb_id, dim_json in ltb:
        meta = {"arm": arm, "agent_id": agent_id, "turn": int(turn),
                "state_id": ltb_id}
        rows += _explode(dim_json, "LTB", meta, by_stb_id.get(source_stb_id, {}))

    df = pd.DataFrame(rows, columns=["arm", "agent_id", "turn", "layer", "dim",
                                     "relation", "evidence_id", "kind", "state_id"])
    df["is_burnin"] = df["turn"].isin(paths.BURNIN_TURNS)
    return df


def main() -> None:
    paths.PANELS.mkdir(parents=True, exist_ok=True)
    frames = [build_evidence_panel(dbio.connect(paths.db_path(arm)), arm)
              for arm in paths.ARMS]
    panel = pd.concat(frames, ignore_index=True)
    unknown = panel[panel["kind"] == "unknown"]
    if len(unknown):
        sample = unknown["evidence_id"].head(10).tolist()
        raise AssertionError(
            f"kind 미분류 {len(unknown)}건. 조용히 넘기지 않는다. 예: {sample}")
    panel.to_parquet(paths.PANELS / "evidence_panel.parquet", index=False)
    print(f"evidence_panel {len(panel):,}행 저장")
    print(panel.groupby(["arm", "kind"]).size())


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s0_evidence.py -v`
Expected: 7 passed

- [ ] **Step 5: 실 데이터로 실행**

Run: `analysis/community_belief_v10/.venv/bin/python analysis/community_belief_v10/s0_evidence.py`

Expected: 행 수 출력 + arm × kind 집계. **미분류가 있으면 예외로 죽는다.**
죽으면 그 ID들이 무엇인지 조사한 뒤 `classify`에 규칙을 추가한다 — 조용히 버리지 않는다.

- [ ] **Step 6: OFF arm에 community_claim이 0인지 즉시 확인**

```bash
analysis/community_belief_v10/.venv/bin/python -c "
import sys; sys.path.insert(0, 'analysis/community_belief_v10')
import pandas as pd, paths
df = pd.read_parquet(paths.PANELS / 'evidence_panel.parquet')
print(df[df.arm=='RN_COMM_OFF'].kind.value_counts())
assert (df[(df.arm=='RN_COMM_OFF') & (df.kind=='community_claim')].shape[0]) == 0
print('OFF arm 커뮤니티 근거 0건 확인')
"
```

- [ ] **Step 7: 커밋 (사용자 승인 시에만 실행)**

```bash
git add analysis/community_belief_v10/s0_evidence.py analysis/community_belief_v10/tests/test_s0_evidence.py
git commit -m "analysis: evidence_panel + kind 판정 (화이트리스트 조인, LTB 계보 해결)"
```

---

## Task 5: 커뮤니티 패널 (exposure / post / claim)

**Files:**
- Create: `analysis/community_belief_v10/s0_community.py`
- Test: `analysis/community_belief_v10/tests/test_s0_community.py`

**Interfaces:**
- Consumes: `dbio`, `paths`
- Produces: `s0_community.parse_exposure_id(exposure_id: str) -> dict`
  반환 키: `source_date, source_turn, post_id, relation, reader_agent_id, delivery_turn`
- Produces: `s0_community.build_post_panel(conn, arm) -> pd.DataFrame`
- Produces: `s0_community.build_exposure_panel(arm) -> pd.DataFrame` (CSV에서 읽음 — `exposure_level`이 DB에 없기 때문)
- Produces: `s0_community.build_claim_panel(conn, arm) -> pd.DataFrame`
  컬럼: `arm, reader_agent_id, delivery_turn, claim_index, claim_id, claim_text, claim_stance, supporting_quote, supporting_quote_ref, source_post_ids, cited`
- Produces: `s0_community.main() -> None` → `panels/{post,exposure,claim}_panel.parquet`

- [ ] **Step 1: 실패하는 테스트 작성**

`analysis/community_belief_v10/tests/test_s0_community.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dbio
import s0_community
from fixtures import make_run_db

EXPOSURE = "community:2026-03-03:t4:post:73:best_full_body:A002:delivered_t5"


def test_parse_exposure_id():
    parsed = s0_community.parse_exposure_id(EXPOSURE)
    assert parsed["source_date"] == "2026-03-03"
    assert parsed["source_turn"] == 4
    assert parsed["post_id"] == "73"
    assert parsed["relation"] == "best_full_body"
    assert parsed["reader_agent_id"] == "A002"
    assert parsed["delivery_turn"] == 5


def test_parse_exposure_id_rejects_garbage():
    import pytest
    with pytest.raises(ValueError):
        s0_community.parse_exposure_id("not-an-exposure-id")


def test_claim_panel_uses_delivery_turn_not_log_turn(tmp_path):
    """claim_id의 turn은 community_logs.turn이 아니라 '배달 turn'이다.

    실측(2026-08-03): community_logs.turn은 글이 올라온 PM turn이고,
    claim은 다음 AM에 소비되므로 claim_id의 turn은 그보다 1 크다.
    노출 ID 끝의 delivered_t<N>이 그 배달 turn이다.
    fixture의 community_logs 행은 turn=4이고 노출은 delivered_t5이므로
    claim_id는 t005여야 한다. log turn을 그대로 쓰면 t004가 되어 틀린다.
    """
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_community.build_claim_panel(dbio.connect(db), "RN_COMM_ON")
    assert df["claim_id"].iloc[0] == "community_claim:A001:t005:01"
    assert df["delivery_turn"].iloc[0] == 5
    assert df["log_turn"].iloc[0] == 4
    assert df["source_post_ids"].iloc[0] == ["73"]
    assert df["claim_stance"].iloc[0] == "bullish"


def test_delivery_turn_falls_back_to_log_turn_plus_one(tmp_path):
    """노출 ID가 없는 claim은 log turn + 1로 배달 turn을 정한다."""
    assert s0_community.resolve_delivery_turn(4, []) == 5
    assert s0_community.resolve_delivery_turn(
        4, [{"delivery_turn": 5}, {"delivery_turn": 5}]) == 5


def test_post_panel_has_body_and_best_flag(tmp_path):
    db = make_run_db(tmp_path, "RN_COMM_ON", with_community=True)
    df = s0_community.build_post_panel(dbio.connect(db), "RN_COMM_ON")
    assert df["is_best"].iloc[0] == 1
    assert "본문 첫 문장" in df["content"].iloc[0]
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s0_community.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 's0_community'`

- [ ] **Step 3: 구현**

```python
"""커뮤니티 패널 3종.

exposure_level(title_only / full_body)은 DB의 community_interactions에는
없고 CSV에만 있다(2026-08-03 실측). 노출 수준을 다루는 모든 분석은 CSV를 쓴다.
두 수준은 절대 합산하지 않는다 — ANALYSIS_FIELD_GUIDE.md §2-E.
"""

import json
import re

import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api()

import dbio
import paths

EXPOSURE_RE = re.compile(
    r"^community:(?P<source_date>\d{4}-\d{2}-\d{2}):t(?P<source_turn>\d+):"
    r"post:(?P<post_id>[^:]+):(?P<relation>[a-z_]+):"
    r"(?P<reader_agent_id>[^:]+):delivered_t(?P<delivery_turn>\d+)$")


def parse_exposure_id(exposure_id: str) -> dict:
    match = EXPOSURE_RE.match(exposure_id)
    if not match:
        raise ValueError(f"노출 ID 형식이 예상과 다르다: {exposure_id}")
    parsed = match.groupdict()
    parsed["source_turn"] = int(parsed["source_turn"])
    parsed["delivery_turn"] = int(parsed["delivery_turn"])
    return parsed


def build_post_panel(conn, arm: str) -> pd.DataFrame:
    df = dbio.read_table(conn, "community_posts")
    df.insert(0, "arm", arm)
    for col in ("turn", "like_count", "unlike_count", "score", "is_best"):
        df[col] = pd.to_numeric(df[col])
    df["is_burnin"] = df["turn"].isin(paths.BURNIN_TURNS)
    return df


def build_exposure_panel(arm: str) -> pd.DataFrame:
    path = paths.csv_path(arm, "community_interactions.csv")
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    df.insert(0, "arm", arm)
    if not set(df["exposure_level"].dropna()) <= {"title_only", "full_body"}:
        raise AssertionError(
            f"예상치 못한 exposure_level: {set(df['exposure_level'])}")
    df["is_burnin"] = df["turn"].isin(paths.BURNIN_TURNS)
    return df


def resolve_delivery_turn(log_turn: int, exposures: list) -> int:
    """claim이 소비된 turn을 정한다.

    community_logs.turn은 글이 올라온 PM turn이고, claim은 다음 AM에 소비된다.
    claim_id에 박히는 turn은 후자다. 노출 ID 끝의 delivered_t<N>이 정본이고,
    노출이 하나도 없는 claim만 log_turn + 1로 되돌린다.
    실측(2026-08-03): 두 방식 모두 15,536건 전부 일치했다.
    """
    turns = {e["delivery_turn"] for e in exposures}
    if len(turns) > 1:
        raise AssertionError(f"한 claim의 배달 turn이 갈린다: {sorted(turns)}")
    return turns.pop() if turns else int(log_turn) + 1


def build_claim_panel(conn, arm: str) -> pd.DataFrame:
    rows = []
    for agent_id, turn, thinking in conn.execute(
            "select agent_id, turn, community_thinking from community_logs"):
        if not thinking:
            continue
        payload = json.loads(thinking)
        for index, claim in enumerate(payload.get("claims") or [], start=1):
            exposures = [parse_exposure_id(e)
                         for e in claim.get("source_exposure_ids") or []]
            delivery_turn = resolve_delivery_turn(int(turn), exposures)
            rows.append({
                "arm": arm,
                "reader_agent_id": agent_id,
                "log_turn": int(turn),
                "delivery_turn": delivery_turn,
                "claim_index": index,
                "claim_id": (f"community_claim:{agent_id}"
                             f":t{delivery_turn:03d}:{index:02d}"),
                "claim_text": claim.get("claim_text"),
                "claim_stance": claim.get("claim_stance"),
                "supporting_quote": claim.get("supporting_quote"),
                "supporting_quote_ref": claim.get("supporting_quote_ref"),
                "source_post_ids": [e["post_id"] for e in exposures],
                "source_relations": sorted({e["relation"] for e in exposures}),
            })
    return pd.DataFrame(rows)


def main() -> None:
    paths.PANELS.mkdir(parents=True, exist_ok=True)
    posts, exposures, claims = [], [], []
    for arm in paths.ARMS:
        conn = dbio.connect(paths.db_path(arm))
        posts.append(build_post_panel(conn, arm))
        exposures.append(build_exposure_panel(arm))
        claims.append(build_claim_panel(conn, arm))
    post_panel = pd.concat(posts, ignore_index=True)
    exposure_panel = pd.concat([e for e in exposures if len(e)], ignore_index=True)
    claim_panel = pd.concat([c for c in claims if len(c)], ignore_index=True)

    # 인용 여부 표시: evidence_panel의 community_claim ID와 대조
    ev = pd.read_parquet(paths.PANELS / "evidence_panel.parquet")
    cited = set(ev.loc[ev["kind"] == "community_claim", "evidence_id"])
    claim_panel["cited"] = claim_panel["claim_id"].isin(cited)

    post_panel.to_parquet(paths.PANELS / "post_panel.parquet", index=False)
    exposure_panel.to_parquet(paths.PANELS / "exposure_panel.parquet", index=False)
    claim_panel.to_parquet(paths.PANELS / "claim_panel.parquet", index=False)
    print(f"post {len(post_panel):,} / exposure {len(exposure_panel):,} "
          f"/ claim {len(claim_panel):,} (인용됨 {claim_panel['cited'].sum():,})")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s0_community.py -v`
Expected: 4 passed

- [ ] **Step 5: 실 데이터로 실행하고 claim_id 규칙을 실증 검증**

Run: `analysis/community_belief_v10/.venv/bin/python analysis/community_belief_v10/s0_community.py`

그다음 **claim_id 재구성 규칙이 실제로 맞는지** 확인한다:

```bash
analysis/community_belief_v10/.venv/bin/python -c "
import sys; sys.path.insert(0, 'analysis/community_belief_v10')
import pandas as pd, paths
claims = pd.read_parquet(paths.PANELS / 'claim_panel.parquet')
ev = pd.read_parquet(paths.PANELS / 'evidence_panel.parquet')
cited_ids = set(ev.loc[ev.kind=='community_claim','evidence_id'])
built_ids = set(claims.claim_id)
missing = cited_ids - built_ids
print('인용됐지만 재구성 실패:', len(missing), list(missing)[:5])
print('재구성 claim 수:', len(built_ids), '| 인용률:', claims.cited.mean().round(3))
assert not missing, 'claim_id 재구성 규칙이 틀렸다 — 규칙을 고칠 것'
"
```

Expected: `인용됐지만 재구성 실패: 0`
**실패하면** `community_logs.turn`이 배달 turn이 아닐 수 있다. 그 경우 `delivery_date`/`source_exposure_ids`의 `delivered_t<N>`에서 turn을 취하도록 규칙을 고친다.

- [ ] **Step 6: 커밋 (사용자 승인 시에만 실행)**

```bash
git add analysis/community_belief_v10/s0_community.py analysis/community_belief_v10/tests/test_s0_community.py
git commit -m "analysis: 커뮤니티 패널 (post/exposure/claim) + 노출 ID 파서"
```

---

## Task 6: news_panel과 action_panel

**Files:**
- Create: `analysis/community_belief_v10/s0_news.py`
- Create: `analysis/community_belief_v10/s0_actions.py`
- Test: `analysis/community_belief_v10/tests/test_s0_news.py`

**Interfaces:**
- Produces: `s0_news.parse_category(article_id: str) -> str` → `종목` / `섹터` / `경제`
- Produces: `s0_news.build_news_panel() -> pd.DataFrame`
  컬럼: `article_id, event_id, slot_ordinal, category, title, summary, published_at, observed_at, source, date, subturn`
- Produces: `s0_actions.build_action_panel(conn, arm) -> pd.DataFrame`
  컬럼: `arm, agent_id, turn, date, action, requested_quantity, filled_quantity, executed_price, signed_value, is_burnin`

- [ ] **Step 1: 실패하는 테스트 작성**

`analysis/community_belief_v10/tests/test_s0_news.py`:

```python
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s0_news


def test_parse_category():
    assert s0_news.parse_category("news_20260227_종목_4aa6a00f") == "종목"
    assert s0_news.parse_category("news_20260306_경제_79081fc2") == "경제"
    assert s0_news.parse_category("news_20260306_섹터_abc12345") == "섹터"


def test_parse_category_rejects_unknown():
    with pytest.raises(ValueError):
        s0_news.parse_category("news_20260306_이상한_abc")


def test_news_panel_from_sealed_bundle():
    """봉인 뉴스는 articles 760 / slots 760 / event 90이다."""
    df = s0_news.build_news_panel()
    assert len(df) == 760
    assert df["event_id"].nunique() == 90
    assert set(df["category"]) == {"종목", "섹터", "경제"}
    assert df["title"].notna().all()
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s0_news.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 's0_news'`

- [ ] **Step 3: s0_news.py 구현**

```python
"""news_panel: 봉인 뉴스 정본에서 event slot 단위 표를 만든다.

카테고리는 article_id에 인코딩되어 있다 (news_<yyyymmdd>_<카테고리>_<hash>).
event별 목표는 종목 5·섹터 3·경제 2이며, 부족분은 backfill하지 않는다
(EXPERIMENT_DESIGN.md §4). 따라서 event별 실제 수가 10 미만인 경우가 정상이다.
"""

import json

import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api()

import paths

VALID_CATEGORIES = {"종목", "섹터", "경제"}


def parse_category(article_id: str) -> str:
    parts = article_id.split("_")
    if len(parts) < 4 or parts[2] not in VALID_CATEGORIES:
        raise ValueError(f"카테고리를 읽을 수 없다: {article_id}")
    return parts[2]


def build_news_panel() -> pd.DataFrame:
    bundle = json.loads(paths.NEWS_JSON.read_text(encoding="utf-8"))
    articles = pd.DataFrame(bundle["articles"])
    slots = pd.DataFrame(bundle["slots"])
    df = slots.merge(articles, on="article_id", how="left",
                     suffixes=("", "_article"))
    if df["title"].isna().any():
        raise AssertionError("slot에 대응하는 article이 없다")
    df["category"] = df["article_id"].map(parse_category)
    df[["date", "subturn"]] = df["event_id"].str.split("/", expand=True)
    cols = ["article_id", "event_id", "date", "subturn", "slot_ordinal",
            "category", "title", "summary", "published_at", "observed_at", "source"]
    return df[cols].sort_values(["event_id", "slot_ordinal"]).reset_index(drop=True)


def main() -> None:
    paths.PANELS.mkdir(parents=True, exist_ok=True)
    df = build_news_panel()
    df.to_parquet(paths.PANELS / "news_panel.parquet", index=False)
    print(f"news_panel {len(df):,}행 / event {df['event_id'].nunique()}개")
    print(df.groupby("category").size())


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: s0_actions.py 구현**

```python
"""action_panel: decision과 fill을 turn 단위로 합친다.

decision(의도)과 fill(실제 체결)은 다른 것이다. 절대 합치지 않고 두 열로 둔다
(README.md '결과와 재현성' 절). signed_value는 매수 +, 매도 −로
일별 순매수 방향 집계에 쓴다.
"""

import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api()

import dbio
import paths


def build_action_panel(conn, arm: str) -> pd.DataFrame:
    decisions = dbio.read_table(conn, "simulation_decisions")
    fills = dbio.read_table(conn, "simulation_fills")
    df = decisions.merge(
        fills[["decision_id", "fill_id", "filled_quantity", "executed_price", "fee"]],
        on="decision_id", how="left")
    df.insert(0, "arm", arm)
    df["turn"] = pd.to_numeric(df["turn"])
    df["filled_quantity"] = pd.to_numeric(df["filled_quantity"]).fillna(0)
    df["executed_price"] = pd.to_numeric(df["executed_price"]).fillna(0)
    sign = df["action"].map({"buy": 1, "sell": -1})
    if sign.isna().any():
        raise AssertionError(f"예상치 못한 action: {set(df['action'])}")
    df["signed_value"] = sign * df["filled_quantity"] * df["executed_price"]
    df["is_burnin"] = df["turn"].isin(paths.BURNIN_TURNS)
    cols = ["arm", "agent_id", "turn", "date", "action", "requested_quantity",
            "fill_id", "filled_quantity", "executed_price", "signed_value", "is_burnin"]
    return df[cols]


def main() -> None:
    paths.PANELS.mkdir(parents=True, exist_ok=True)
    panel = pd.concat(
        [build_action_panel(dbio.connect(paths.db_path(arm)), arm)
         for arm in paths.ARMS], ignore_index=True)
    if len(panel) != 9000 * len(paths.ARMS):
        raise AssertionError(f"action_panel 행 수 {len(panel)} != 18000")
    panel.to_parquet(paths.PANELS / "action_panel.parquet", index=False)
    print(f"action_panel {len(panel):,}행")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s0_news.py -v`
Expected: 3 passed

- [ ] **Step 6: 실행**

```bash
analysis/community_belief_v10/.venv/bin/python analysis/community_belief_v10/s0_news.py
analysis/community_belief_v10/.venv/bin/python analysis/community_belief_v10/s0_actions.py
```

Expected: `news_panel 760행 / event 90개`, `action_panel 18,000행`

- [ ] **Step 7: 커밋 (사용자 승인 시에만 실행)**

```bash
git add analysis/community_belief_v10/s0_news.py analysis/community_belief_v10/s0_actions.py \
        analysis/community_belief_v10/tests/test_s0_news.py
git commit -m "analysis: news_panel + action_panel"
```

---

## Task 7: S0 검증 게이트

**Files:**
- Create: `analysis/community_belief_v10/s0_gate.py`
- Test: `analysis/community_belief_v10/tests/test_s0_gate.py`

**Interfaces:**
- Produces: `s0_gate.run_gates() -> dict[str, str]` — 게이트명 → "PASS"/실패 사유. 실패가 하나라도 있으면 `AssertionError`
- 게이트 6종은 스펙 §4.4와 동일

- [ ] **Step 1: 실패하는 테스트 작성**

`analysis/community_belief_v10/tests/test_s0_gate.py`:

```python
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s0_gate


def test_gate_detects_off_arm_community_leak():
    """OFF arm에 커뮤니티 근거가 있으면 설계 위반이다. 반드시 잡아야 한다."""
    evidence = pd.DataFrame([
        {"arm": "RN_COMM_OFF", "kind": "community_claim", "evidence_id": "c1"},
    ])
    with pytest.raises(AssertionError, match="OFF arm"):
        s0_gate.check_off_arm_is_clean(evidence)


def test_gate_detects_unknown_kind():
    evidence = pd.DataFrame([{"arm": "RN_COMM_ON", "kind": "unknown",
                              "evidence_id": "mystery"}])
    with pytest.raises(AssertionError, match="미분류"):
        s0_gate.check_no_unknown_kind(evidence)


def test_gate_detects_unpaired_keys():
    """ON/OFF의 (agent, turn) 집합이 다르면 paired 대조가 불가능하다."""
    belief = pd.DataFrame([
        {"arm": "RN_COMM_ON", "agent_id": "A001", "turn": 5, "layer": "STB", "dim": "dim_1"},
        {"arm": "RN_COMM_OFF", "agent_id": "A001", "turn": 6, "layer": "STB", "dim": "dim_1"},
    ])
    with pytest.raises(AssertionError, match="짝"):
        s0_gate.check_arms_are_paired(belief)


def test_gate_passes_on_clean_input():
    belief = pd.DataFrame([
        {"arm": a, "agent_id": "A001", "turn": 5, "layer": "STB", "dim": "dim_1"}
        for a in ("RN_COMM_ON", "RN_COMM_OFF")])
    s0_gate.check_arms_are_paired(belief)  # 예외 없이 통과
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s0_gate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 's0_gate'`

- [ ] **Step 3: 구현**

```python
"""S0 검증 게이트. 조용한 오류를 여기서 전부 죽인다.

D2 검색이 60/60회 0건이면서 에러 없이 조용히 죽어 있던 사고(2026-07-31)가
있었다. 그래서 이 분석은 "결과가 비어 있음"을 성공으로 취급하지 않는다.
"""

import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api()

import dbio
import paths

EXPECTED_COUNTS = {
    "RN_COMM_ON": {"simulation_stb_states": 9000, "simulation_ltb_states": 9100,
                   "simulation_decisions": 9000, "simulation_fills": 9000,
                   "simulation_trade_outcomes": 27000,
                   "community_posts": 3150, "community_interactions": 15745,
                   "community_logs": 4500},
    "RN_COMM_OFF": {"simulation_stb_states": 9000, "simulation_ltb_states": 9100,
                    "simulation_decisions": 9000, "simulation_fills": 9000,
                    "simulation_trade_outcomes": 27000,
                    "community_posts": 0, "community_interactions": 0,
                    "community_logs": 0},
}


def check_row_counts() -> None:
    for arm, expected in EXPECTED_COUNTS.items():
        conn = dbio.connect(paths.db_path(arm))
        for table, want in expected.items():
            got = dbio.table_count(conn, table)
            if got != want:
                raise AssertionError(f"{arm}.{table} 행 수 {got} != 기대 {want}")


def check_off_arm_is_clean(evidence: pd.DataFrame) -> None:
    leak = evidence[(evidence["arm"] == "RN_COMM_OFF")
                    & (evidence["kind"] == "community_claim")]
    if len(leak):
        raise AssertionError(
            f"OFF arm에 커뮤니티 근거 {len(leak)}건 — 조건 격리 위반")


def check_no_unknown_kind(evidence: pd.DataFrame) -> None:
    unknown = evidence[evidence["kind"] == "unknown"]
    if len(unknown):
        raise AssertionError(
            f"kind 미분류 {len(unknown)}건: {unknown['evidence_id'].head(5).tolist()}")


def check_arms_are_paired(belief: pd.DataFrame) -> None:
    keys = {arm: set(map(tuple, belief.loc[belief["arm"] == arm,
                                           ["agent_id", "turn", "layer", "dim"]].values))
            for arm in belief["arm"].unique()}
    if len(keys) != 2:
        raise AssertionError(f"arm이 2개가 아니다: {list(keys)}")
    on, off = keys["RN_COMM_ON"], keys["RN_COMM_OFF"]
    if on != off:
        raise AssertionError(
            f"ON/OFF 짝이 맞지 않는다. ON에만 {len(on - off)}개, OFF에만 {len(off - on)}개")


def check_exposure_levels(exposure: pd.DataFrame) -> None:
    levels = set(exposure["exposure_level"].dropna())
    if not levels <= {"title_only", "full_body"}:
        raise AssertionError(f"예상치 못한 exposure_level: {levels}")


def check_posting_rate(post: pd.DataFrame) -> str:
    """게시율 100%면 '게시 여부 판단' 설계 요소의 변별력이 없다 → 논문 한계."""
    on = post[post["arm"] == "RN_COMM_ON"]
    eligible_per_day, days = 70, 45
    rate = len(on) / (eligible_per_day * days)
    return f"게시율 {rate:.1%} ({len(on)}/{eligible_per_day * days})"


def run_gates() -> dict:
    belief = pd.read_parquet(paths.PANELS / "belief_panel.parquet")
    evidence = pd.read_parquet(paths.PANELS / "evidence_panel.parquet")
    exposure = pd.read_parquet(paths.PANELS / "exposure_panel.parquet")
    post = pd.read_parquet(paths.PANELS / "post_panel.parquet")

    results = {}
    check_row_counts(); results["행 수"] = "PASS"
    check_off_arm_is_clean(evidence); results["OFF arm 격리"] = "PASS"
    check_no_unknown_kind(evidence); results["kind 분류"] = "PASS"
    check_arms_are_paired(belief); results["ON/OFF 짝"] = "PASS"
    check_exposure_levels(exposure); results["exposure_level"] = "PASS"
    results["게시율"] = check_posting_rate(post)
    return results


if __name__ == "__main__":
    for name, status in run_gates().items():
        print(f"[{name}] {status}")
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s0_gate.py -v`
Expected: 4 passed

- [ ] **Step 5: 실 데이터로 게이트 실행**

Run: `analysis/community_belief_v10/.venv/bin/python analysis/community_belief_v10/s0_gate.py`

Expected: 6줄 모두 PASS(게시율은 수치 출력).
**하나라도 실패하면 그 원인을 해결하기 전까지 S1로 넘어가지 않는다.**

- [ ] **Step 6: 커밋 (사용자 승인 시에만 실행)**

```bash
git add analysis/community_belief_v10/s0_gate.py analysis/community_belief_v10/tests/test_s0_gate.py
git commit -m "analysis: S0 검증 게이트 6종"
```

---

## Task 8: 로컬 임베딩 모듈

**Files:**
- Create: `analysis/community_belief_v10/embed.py`
- Test: `analysis/community_belief_v10/tests/test_embed.py`

**Interfaces:**
- Produces: `embed.text_key(text: str) -> str` — sha256 16자리
- Produces: `embed.encode(texts: list[str], cache_name: str) -> np.ndarray` — (N, 384), L2 정규화, 중복 텍스트는 1회만 계산
- Produces: `embed.MODEL_NAME = "intfloat/multilingual-e5-small"`, `embed.PREFIX = "query: "`

- [ ] **Step 1: 실패하는 테스트 작성**

`analysis/community_belief_v10/tests/test_embed.py`:

```python
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import embed


def test_text_key_is_stable_and_short():
    assert embed.text_key("가나다") == embed.text_key("가나다")
    assert embed.text_key("가나다") != embed.text_key("가나라")
    assert len(embed.text_key("가나다")) == 16


def test_encode_returns_normalized_vectors(tmp_path, monkeypatch):
    monkeypatch.setattr(embed, "CACHE_DIR", tmp_path)
    vectors = embed.encode(["삼성전자 주가가 올랐다", "메모리 업황이 좋다"], "t1")
    assert vectors.shape == (2, 384)
    norms = np.linalg.norm(vectors, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)


def test_duplicate_texts_get_identical_vectors(tmp_path, monkeypatch):
    monkeypatch.setattr(embed, "CACHE_DIR", tmp_path)
    vectors = embed.encode(["같은 문장", "다른 문장", "같은 문장"], "t2")
    assert np.allclose(vectors[0], vectors[2])


def test_cache_is_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(embed, "CACHE_DIR", tmp_path)
    first = embed.encode(["캐시 테스트"], "t3")
    assert (tmp_path / "t3.npz").exists()
    second = embed.encode(["캐시 테스트"], "t3")
    assert np.allclose(first, second)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_embed.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'embed'`

- [ ] **Step 3: 구현**

```python
"""로컬 임베딩. 유료 API를 쓰지 않는다.

intfloat/multilingual-e5-small을 CPU에서 돌린다. 모델 가중치는 Task 1에서
1회 내려받았고, 이후에는 HF_HUB_OFFLINE=1로 네트워크를 쓰지 않는다.

belief 텍스트는 차원별로 변하지 않으면 이전 문장이 그대로 유지되므로
(2026-07-31 규칙 변경) 중복이 매우 많다. 텍스트 해시로 중복을 제거해
같은 문장을 두 번 계산하지 않는다.
"""

import hashlib
from pathlib import Path

import numpy as np

import guard

guard.enforce_no_paid_api(offline=True)
guard.assert_no_openai_package()

import paths

MODEL_NAME = "intfloat/multilingual-e5-small"
PREFIX = "query: "
CACHE_DIR = paths.OUT / "embed_cache"
_MODEL = None


def _model():
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer(MODEL_NAME, device="cpu")
    return _MODEL


def text_key(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def encode(texts: list, cache_name: str) -> np.ndarray:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = Path(CACHE_DIR) / f"{cache_name}.npz"

    cache = {}
    if cache_file.exists():
        # allow_pickle은 쓰지 않는다. keys는 문자열 배열, vectors는 float 배열이라
        # 순수 npz로 충분하며, pickle 역직렬화 경로를 열어둘 이유가 없다.
        stored = np.load(cache_file)
        cache = {str(k): v for k, v in zip(stored["keys"], stored["vectors"])}

    keys = [text_key(t) for t in texts]
    missing = sorted({k for k in keys if k not in cache})
    if missing:
        key_to_text = {}
        for key, text in zip(keys, texts):
            key_to_text.setdefault(key, text)
        batch = [PREFIX + key_to_text[k] for k in missing]
        vectors = _model().encode(
            batch, batch_size=64, convert_to_numpy=True,
            normalize_embeddings=True, show_progress_bar=True)
        for key, vector in zip(missing, vectors):
            cache[key] = vector
        np.savez_compressed(
            cache_file,
            keys=np.array(list(cache.keys())),
            vectors=np.vstack(list(cache.values())))

    return np.vstack([cache[k] for k in keys])
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_embed.py -v`
Expected: 4 passed (첫 실행 시 모델 로드로 수십 초 걸릴 수 있다)

- [ ] **Step 5: 커밋 (사용자 승인 시에만 실행)**

```bash
git add analysis/community_belief_v10/embed.py analysis/community_belief_v10/tests/test_embed.py
git commit -m "analysis: 로컬 e5-small 임베딩 모듈 + 해시 캐시"
```

---

## Task 9: S1 — 뉴스·게시글 공동 클러스터링

**Files:**
- Create: `analysis/community_belief_v10/s1_cluster.py`
- Test: `analysis/community_belief_v10/tests/test_s1_cluster.py`

**Interfaces:**
- Consumes: `embed.encode`, `panels/news_panel.parquet`, `panels/post_panel.parquet`
- Produces: `s1_cluster.build_corpus() -> pd.DataFrame` — 컬럼 `doc_id, doc_type('news'|'post'), date, turn, text, category, post_type, post_id, article_id`
- Produces: `s1_cluster.choose_k(vectors, k_range, seeds) -> pd.DataFrame` — 컬럼 `k, silhouette, ari_stability`
- Produces: `s1_cluster.fit(vectors, k, seed) -> tuple[np.ndarray, np.ndarray]` — (labels, centroids)
- Produces: `s1_cluster.echo_ratio(corpus) -> pd.DataFrame` — 클러스터별 뉴스/게시글 수와 에코 비율
- Produces: `s1_cluster.transfer_lag(corpus) -> pd.DataFrame` — 클러스터별 뉴스 첫 등장일 → 게시글 등장일 지연
- Produces: `panels/corpus_clusters.parquet`, `panels/cluster_centroids.npy`, `cluster_labels.csv`, `figures/s1_*.png`

- [ ] **Step 1: 실패하는 테스트 작성**

`analysis/community_belief_v10/tests/test_s1_cluster.py`:

```python
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s1_cluster


def _toy_corpus():
    return pd.DataFrame([
        {"doc_id": "n1", "doc_type": "news", "date": "2026-03-02", "cluster": 0},
        {"doc_id": "n2", "doc_type": "news", "date": "2026-03-02", "cluster": 0},
        {"doc_id": "p1", "doc_type": "post", "date": "2026-03-03", "cluster": 0},
        {"doc_id": "p2", "doc_type": "post", "date": "2026-03-04", "cluster": 0},
        {"doc_id": "p3", "doc_type": "post", "date": "2026-03-05", "cluster": 1},
    ])


def test_echo_ratio_counts_news_and_posts():
    out = s1_cluster.echo_ratio(_toy_corpus()).set_index("cluster")
    assert out.loc[0, "n_news"] == 2
    assert out.loc[0, "n_post"] == 2
    assert out.loc[0, "echo_ratio"] == 1.0
    # 뉴스가 없는 클러스터는 커뮤니티 고유 화제(novel)다
    assert out.loc[1, "n_news"] == 0
    assert bool(out.loc[1, "is_novel"])


def test_transfer_lag_measures_days_from_first_news():
    out = s1_cluster.transfer_lag(_toy_corpus()).set_index("cluster")
    assert out.loc[0, "median_lag_days"] == 2.0  # 03-03, 03-05 → median 1,3 = 2


def test_fit_returns_labels_and_centroids():
    rng = np.random.default_rng(0)
    vectors = np.vstack([rng.normal(1, 0.01, (20, 8)), rng.normal(-1, 0.01, (20, 8))])
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    labels, centroids = s1_cluster.fit(vectors, k=2, seed=0)
    assert labels.shape == (40,)
    assert centroids.shape == (2, 8)
    assert len(set(labels)) == 2


def test_remove_style_axis_collapses_the_group_difference():
    """두 집단이 한 방향으로 떨어져 있으면, 그 방향을 제거한 뒤
    두 집단의 평균이 그 방향 위에서 같아져야 한다."""
    rng = np.random.default_rng(7)
    topic = rng.normal(size=(40, 6))                 # 두 집단이 공유하는 화제 변동
    style = np.zeros(6)
    style[0] = 3.0                                   # 0번 축이 문체 축
    vectors = np.vstack([topic[:20] + style, topic[20:] - style])
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    doc_type = np.array(["news"] * 20 + ["post"] * 20)

    corrected = s1_cluster.remove_style_axis(vectors, doc_type)

    axis = vectors[doc_type == "news"].mean(0) - vectors[doc_type == "post"].mean(0)
    axis /= np.linalg.norm(axis)
    news_projection = (corrected[doc_type == "news"] @ axis).mean()
    post_projection = (corrected[doc_type == "post"] @ axis).mean()
    assert abs(news_projection - post_projection) < 1e-9


def test_remove_style_axis_keeps_unit_norm():
    rng = np.random.default_rng(8)
    vectors = rng.normal(size=(10, 5))
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    doc_type = np.array(["news"] * 5 + ["post"] * 5)
    corrected = s1_cluster.remove_style_axis(vectors, doc_type)
    assert np.allclose(np.linalg.norm(corrected, axis=1), 1.0, atol=1e-9)


def test_mixed_cluster_share_counts_clusters_holding_both_types():
    corpus = pd.DataFrame([
        {"cluster": 0, "doc_type": "news"},
        {"cluster": 0, "doc_type": "post"},   # 혼합
        {"cluster": 1, "doc_type": "post"},   # 게시글만
        {"cluster": 2, "doc_type": "news"},   # 뉴스만
    ])
    assert s1_cluster.mixed_cluster_share(corpus) == 1 / 3


def test_plot_diagnostics_writes_four_figures(tmp_path, monkeypatch):
    """진단 그림 4종이 실제로 파일로 떨어지는지 확인한다.

    그림의 내용은 자동 검증하지 않는다(사람이 보는 물건이다). 다만 '그렸다고
    보고했는데 파일이 없다'는 조용한 실패는 막는다.
    """
    import s1_cluster as module

    monkeypatch.setattr(module.paths, "FIGURES", tmp_path)
    rng = np.random.default_rng(2)
    vectors = rng.normal(size=(12, 5))
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    corpus = pd.DataFrame({
        "cluster": [0] * 6 + [1] * 6,
        "doc_type": ["news"] * 6 + ["post"] * 6,
        "date": ["2026-03-02"] * 6 + ["2026-03-05"] * 6,
    })
    k_table = pd.DataFrame({"k": [2, 3], "silhouette": [0.4, 0.3],
                            "ari_stability": [0.9, 0.7]})
    written = module.plot_diagnostics(vectors, corpus, k_table)
    assert len(written) == 4
    for target in written:
        assert target.exists() and target.stat().st_size > 0
    assert {t.name for t in written} == {
        "s1_pca_by_cluster.png", "s1_pca_by_doctype.png",
        "s1_pca_by_date.png", "s1_k_selection.png"}


def test_original_space_diagnostics_writes_three_figures_and_real_numbers(
        tmp_path, monkeypatch):
    """원공간 진단 3종이 파일로 떨어지고, 수치가 실제 기하를 반영하는지 확인한다.

    잘 갈린 두 덩어리를 넣으면 분리도가 뚜렷하게 양수여야 하고,
    centroid 비대각 유사도는 1보다 확실히 낮아야 한다. 이 검사는
    함수가 상수를 뱉는 껍데기가 아님을 보장한다.
    """
    import s1_cluster as module

    monkeypatch.setattr(module.paths, "FIGURES", tmp_path)
    rng = np.random.default_rng(11)
    group_a = rng.normal(size=(30, 8)) * 0.05 + np.array([1, 0, 0, 0, 0, 0, 0, 0])
    group_b = rng.normal(size=(30, 8)) * 0.05 + np.array([0, 1, 0, 0, 0, 0, 0, 0])
    vectors = np.vstack([group_a, group_b])
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    labels = np.array([0] * 30 + [1] * 30)

    written, stats = module.original_space_diagnostics(vectors, labels)

    assert {t.name for t in written} == {
        "s1_orig_similarity_distribution.png",
        "s1_orig_silhouette_plot.png",
        "s1_orig_centroid_heatmap.png"}
    for target in written:
        assert target.exists() and target.stat().st_size > 0

    assert stats["separation"] > 0.5           # 뚜렷이 갈린 입력이므로
    assert stats["centroid_offdiag_mean"] < 0.5
    assert stats["negative_silhouette_share"] == 0.0


def test_choose_k_reports_stability_across_seeds():
    rng = np.random.default_rng(1)
    vectors = np.vstack([rng.normal(1, 0.01, (20, 8)), rng.normal(-1, 0.01, (20, 8))])
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    table = s1_cluster.choose_k(vectors, k_range=[2, 3], seeds=[0, 1, 2])
    assert set(table["k"]) == {2, 3}
    assert (table["ari_stability"] <= 1.0).all()
    assert table.loc[table["k"] == 2, "ari_stability"].iloc[0] > 0.9
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s1_cluster.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 's1_cluster'`

- [ ] **Step 3: 구현**

```python
"""S1: 봉인 뉴스 760 + 커뮤니티 글 3,150을 하나의 임베딩 공간에서 군집화한다.

비지도 군집은 가설 생성용이다(embedding_analysis_plan.md). 그래서 토픽 라벨뿐
아니라 날짜·doc_type·카테고리 라벨에 대한 silhouette도 함께 낸다. 클러스터가
화제가 아니라 시장 국면이나 날짜로 갈렸을 가능성을 독자가 판단할 수 있어야 한다.

라벨 이름은 사람이 붙인다. LLM 보조는 유료이므로 쓰지 않는다.
"""

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import adjusted_rand_score, silhouette_score

import guard  # noqa: F401

guard.enforce_no_paid_api(offline=True)

import embed
import paths

K_RANGE = list(range(6, 25, 2))
SEEDS = [0, 1, 2, 3, 4]


def build_corpus() -> pd.DataFrame:
    news = pd.read_parquet(paths.PANELS / "news_panel.parquet")
    posts = pd.read_parquet(paths.PANELS / "post_panel.parquet")
    posts = posts[posts["arm"] == "RN_COMM_ON"]

    news_docs = pd.DataFrame({
        "doc_id": "news:" + news["article_id"],
        "doc_type": "news",
        "date": news["date"],
        "turn": np.nan,
        "text": news["title"].fillna("") + "\n" + news["summary"].fillna(""),
        "category": news["category"],
        "post_type": pd.NA,
        "post_id": pd.NA,
        "article_id": news["article_id"],
    }).drop_duplicates("doc_id")

    post_docs = pd.DataFrame({
        "doc_id": "post:" + posts["post_id"].astype(str),
        "doc_type": "post",
        "date": posts["date"],
        "turn": posts["turn"],
        "text": posts["title"].fillna("") + "\n" + posts["content"].fillna(""),
        "category": pd.NA,
        "post_type": posts["post_type"],
        "post_id": posts["post_id"].astype(str),
        "article_id": pd.NA,
    })
    return pd.concat([news_docs, post_docs], ignore_index=True)


def fit(vectors: np.ndarray, k: int, seed: int):
    model = KMeans(n_clusters=k, random_state=seed, n_init=10)
    labels = model.fit_predict(vectors)
    return labels, model.cluster_centers_


def choose_k(vectors: np.ndarray, k_range=None, seeds=None) -> pd.DataFrame:
    k_range = list(k_range if k_range is not None else K_RANGE)
    seeds = list(seeds if seeds is not None else SEEDS)
    rows = []
    for k in k_range:
        runs = [fit(vectors, k, seed)[0] for seed in seeds]
        pairs = [adjusted_rand_score(runs[i], runs[j])
                 for i in range(len(runs)) for j in range(i + 1, len(runs))]
        rows.append({
            "k": k,
            "silhouette": float(silhouette_score(vectors, runs[0], metric="cosine")),
            "ari_stability": float(np.mean(pairs)) if pairs else 1.0,
        })
    return pd.DataFrame(rows)


def label_silhouettes(vectors: np.ndarray, corpus: pd.DataFrame) -> dict:
    """토픽이 아니라 날짜·문서종류로 갈렸을 가능성을 정량화한다."""
    out = {}
    for column in ("cluster", "date", "doc_type"):
        values = corpus[column].astype(str).values
        if len(set(values)) < 2:
            continue
        out[column] = float(silhouette_score(vectors, values, metric="cosine"))
    return out


def remove_style_axis(vectors: np.ndarray, doc_type: np.ndarray) -> np.ndarray:
    """뉴스와 게시글을 가르는 단일 방향 하나를 직교 제거한다.

    왜 필요한가 (2026-08-03 1차 실행 실측, k=18):
      doc_type silhouette 0.374  ≫  cluster 0.040  >  date 0.014
    임베딩 공간의 지배축이 화제가 아니라 문체였다. 뉴스는 헤드라인+기사체 요약,
    게시글은 1인칭 개인투자자 문장이다. 그 결과 18개 클러스터가 **전부** 순수
    뉴스이거나 순수 게시글이 되었고, echo_ratio와 transfer_lag이 전부 NaN이 됐다.
    "같은 화제를 기자가 말할 때와 개미가 말할 때"를 비교한다는 S1의 목적이
    성립하지 않는 상태였다.

    왜 그룹 평균을 통째로 빼지 않는가:
      각 집단의 평균 벡터를 각각 빼면 두 centroid가 원점에서 만나 문체 차이는
      확실히 사라진다. 그러나 그 방법은 "뉴스와 게시글이 원래 다른 화제를 다룬다"는
      **진짜 차이까지** 함께 지운다. 그래서 여기서는 두 평균의 차이 방향 하나만
      제거하는 보수적인 방법을 쓴다.

    남는 한계 (REPORT에 반드시 명시):
      단일 방향 제거도 '문체'와 '평균 화제차'를 완전히 분리하지 못한다. 이 축에
      실려 있던 진짜 화제 차이도 일부 함께 제거된다. 따라서 제거 전후 지표를
      모두 보고하고, 결론이 이 보정에 의존하는지 밝힌다.
    """
    news_mean = vectors[doc_type == "news"].mean(axis=0)
    post_mean = vectors[doc_type == "post"].mean(axis=0)
    axis = news_mean - post_mean
    norm = float(np.linalg.norm(axis))
    if norm == 0.0:
        raise AssertionError("문체 축이 영벡터다 — 두 집단의 평균이 동일하다")
    axis = axis / norm
    corrected = vectors - np.outer(vectors @ axis, axis)
    norms = np.linalg.norm(corrected, axis=1, keepdims=True)
    if float(norms.min()) == 0.0:
        raise AssertionError("문체 축 제거 후 영벡터가 생겼다")
    return corrected / norms


def mixed_cluster_share(corpus: pd.DataFrame) -> float:
    """뉴스와 게시글이 함께 들어 있는 클러스터의 비율.

    문체 축 제거가 성공했는지 판정하는 기준이다. 1차 실행에서는 0.0이었다
    (18개 클러스터 전부 한쪽 종류만). 이 값이 0에 가까우면 echo_ratio와
    transfer_lag은 계산해봐야 NaN이므로, 그 사실을 먼저 보고한다.
    """
    counts = (corpus.groupby(["cluster", "doc_type"]).size()
              .unstack(fill_value=0)
              .reindex(columns=["news", "post"], fill_value=0))
    mixed = int(((counts["news"] > 0) & (counts["post"] > 0)).sum())
    return mixed / len(counts)


def plot_diagnostics(vectors: np.ndarray, corpus: pd.DataFrame,
                     k_table: pd.DataFrame) -> list:
    """진단용 그림 4종. 주장의 근거가 아니라 진단 도구다.

    embedding_analysis_plan.md는 PCA/UMAP 지도를 '탐색 전용, 우선순위 4~5'로
    못박았다. 그래서 이 그림들은 결과 제시가 아니라 다음 질문에 답하기 위해 있다:
      - by_cluster: 토픽 지도 (군집이 실제로 분리되는가)
      - by_doctype: 뉴스와 게시글이 같은 자리에 겹치는가 (echo) 아니면 갈라지는가 (novel)
      - by_date:    ⚠️ 군집이 화제가 아니라 '시점'으로 갈렸는지 눈으로 판별.
                    label_silhouettes()의 날짜 점수와 반드시 함께 볼 것.
      - k_selection: silhouette과 시드 안정성이 어디서 만나는가

    그림 안의 글자는 영어로 쓴다. matplotlib 기본 폰트에 한글 글리프가 없어
    한글 라벨은 두부(□)로 깨진다. 해석은 REPORT.md의 한국어 본문이 담당한다.
    """
    import matplotlib
    matplotlib.use("Agg")          # 화면 없는 환경에서도 저장되도록
    import matplotlib.pyplot as plt
    from sklearn.decomposition import PCA

    paths.FIGURES.mkdir(parents=True, exist_ok=True)
    coords = PCA(n_components=2, random_state=0).fit_transform(vectors)
    written = []

    def scatter(values, title, filename, discrete):
        figure, axis = plt.subplots(figsize=(8, 7))
        if discrete:
            for value in sorted(pd.unique(values)):
                mask = values == value
                axis.scatter(coords[mask, 0], coords[mask, 1], s=6, alpha=0.5,
                             label=str(value))
            axis.legend(markerscale=3, fontsize=8, loc="best")
        else:
            mappable = axis.scatter(coords[:, 0], coords[:, 1], c=values, s=6,
                                    alpha=0.5, cmap="viridis")
            figure.colorbar(mappable, ax=axis, label="days since first article")
        axis.set_title(title)
        axis.set_xlabel("PC1")
        axis.set_ylabel("PC2")
        figure.tight_layout()
        target = paths.FIGURES / filename
        figure.savefig(target, dpi=150)
        plt.close(figure)
        written.append(target)

    scatter(corpus["cluster"].to_numpy(),
            "News + posts by cluster (topic map)",
            "s1_pca_by_cluster.png", True)
    scatter(corpus["doc_type"].to_numpy(),
            "News vs community posts (echo or novel)",
            "s1_pca_by_doctype.png", True)
    dates = pd.to_datetime(corpus["date"])
    scatter((dates - dates.min()).dt.days.to_numpy(),
            "By date - are clusters topics or just time periods?",
            "s1_pca_by_date.png", False)

    figure, axis = plt.subplots(figsize=(7, 5))
    axis.plot(k_table["k"], k_table["silhouette"], marker="o", label="silhouette")
    axis.plot(k_table["k"], k_table["ari_stability"], marker="s",
              label="seed stability (ARI)")
    axis.set_xlabel("k")
    axis.set_ylabel("score")
    axis.set_title("Cluster count selection")
    axis.legend()
    axis.grid(alpha=0.3)
    figure.tight_layout()
    target = paths.FIGURES / "s1_k_selection.png"
    figure.savefig(target, dpi=150)
    plt.close(figure)
    written.append(target)
    return written


def original_space_diagnostics(vectors: np.ndarray, labels: np.ndarray) -> tuple:
    """투영 없이 원공간(384차원) 기하를 보여주는 그림 3종과 그 수치.

    왜 산점도가 아닌가 (2026-08-03 실측):
      384차원을 종이에 그리려면 어떤 방법을 쓰든 투영해야 한다. PCA-3D는 분산의
      8.5%만 담는데 그 안에서 silhouette 0.248이 나온다 — 같은 라벨을 원공간에서
      다시 채점하면 **-0.015(음수)** 다. 차원을 올릴수록 이 착시분은 줄어든다
      (2D 0.341 → 50D 0.026). t-SNE·UMAP은 순수 노이즈에서도 덩어리를 만들어
      내므로 더 나쁘다. 그래서 여기서는 '그리지' 않고 '재서' 보여준다.

    세 그림:
      1) 유사도 분포 — 같은 클러스터 쌍과 다른 클러스터 쌍의 cosine 분포를 겹쳐
         그린다. 두 곡선이 포개지면 경계가 없다는 뜻이다.
      2) silhouette plot — 점별 silhouette을 클러스터별로 정렬. 음수 구간이 보인다.
      3) centroid 유사도 히트맵 — 중심끼리 얼마나 비슷한가. 1에 가까우면 같은 점이다.

    이 셋은 '군집이 없다'를 증명하는 그림이므로 음성 결과 절에 그대로 쓸 수 있다.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from sklearn.metrics import silhouette_samples

    paths.FIGURES.mkdir(parents=True, exist_ok=True)
    written = []
    ordered = sorted(set(labels))

    # --- 1) within/between 유사도 분포 ---
    compact = vectors.astype(np.float32)
    similarity = compact @ compact.T
    upper = np.triu_indices(len(compact), 1)
    same = labels[upper[0]] == labels[upper[1]]
    pair_similarity = similarity[upper]
    within, between = pair_similarity[same], pair_similarity[~same]

    figure, axis = plt.subplots(figsize=(8, 5))
    bins = np.linspace(float(pair_similarity.min()), float(pair_similarity.max()), 120)
    axis.hist(between, bins=bins, density=True, alpha=0.55, label="different clusters")
    axis.hist(within, bins=bins, density=True, alpha=0.55, label="same cluster")
    axis.set_xlabel("cosine similarity (original 384-dim space, no projection)")
    axis.set_ylabel("density")
    axis.set_title(f"Within vs between cluster similarity "
                   f"(gap = {within.mean() - between.mean():+.3f})")
    axis.legend()
    figure.tight_layout()
    target = paths.FIGURES / "s1_orig_similarity_distribution.png"
    figure.savefig(target, dpi=150); plt.close(figure); written.append(target)

    # --- 2) silhouette plot ---
    values = silhouette_samples(vectors, labels, metric="cosine")
    figure, axis = plt.subplots(figsize=(8, 6))
    offset = 0
    for cluster in ordered:
        group = np.sort(values[labels == cluster])
        axis.fill_betweenx(np.arange(offset, offset + len(group)), 0, group)
        axis.text(-0.02, offset + len(group) / 2, str(cluster),
                  va="center", ha="right", fontsize=8)
        offset += len(group) + 20
    axis.axvline(0.0, color="black", linewidth=0.8)
    axis.axvline(float(values.mean()), linestyle="--", linewidth=1,
                 label=f"mean = {values.mean():.3f}")
    axis.set_xlabel("silhouette (cosine, original space)")
    axis.set_ylabel("documents, grouped by cluster")
    axis.set_title(f"Per-document silhouette — {np.mean(values < 0):.1%} are negative")
    axis.legend()
    figure.tight_layout()
    target = paths.FIGURES / "s1_orig_silhouette_plot.png"
    figure.savefig(target, dpi=150); plt.close(figure); written.append(target)

    # --- 3) centroid 유사도 히트맵 ---
    centroids = np.vstack([vectors[labels == c].mean(axis=0) for c in ordered])
    centroids /= np.linalg.norm(centroids, axis=1, keepdims=True)
    matrix = centroids @ centroids.T
    figure, axis = plt.subplots(figsize=(6.5, 5.5))
    image = axis.imshow(matrix, vmin=float(matrix.min()), vmax=1.0, cmap="magma")
    figure.colorbar(image, ax=axis)
    for i in range(len(matrix)):
        for j in range(len(matrix)):
            axis.text(j, i, f"{matrix[i, j]:.2f}", ha="center", va="center",
                      fontsize=7, color="white")
    off_diagonal = matrix[~np.eye(len(matrix), dtype=bool)]
    axis.set_title(f"Cluster centroid similarity "
                   f"(off-diagonal mean = {off_diagonal.mean():.3f})")
    axis.set_xticks(range(len(ordered))); axis.set_yticks(range(len(ordered)))
    figure.tight_layout()
    target = paths.FIGURES / "s1_orig_centroid_heatmap.png"
    figure.savefig(target, dpi=150); plt.close(figure); written.append(target)

    stats = {
        "within_mean": float(within.mean()),
        "between_mean": float(between.mean()),
        "separation": float(within.mean() - between.mean()),
        "negative_silhouette_share": float(np.mean(values < 0)),
        "centroid_offdiag_mean": float(off_diagonal.mean()),
    }
    return written, stats


def echo_ratio(corpus: pd.DataFrame) -> pd.DataFrame:
    counts = (corpus.groupby(["cluster", "doc_type"]).size()
              .unstack(fill_value=0).reindex(columns=["news", "post"], fill_value=0))
    out = counts.rename(columns={"news": "n_news", "post": "n_post"}).reset_index()
    out["echo_ratio"] = out["n_post"] / out["n_news"].replace(0, np.nan)
    out["is_novel"] = out["n_news"] == 0
    return out


def transfer_lag(corpus: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for cluster, group in corpus.groupby("cluster"):
        news_dates = pd.to_datetime(group.loc[group["doc_type"] == "news", "date"])
        post_dates = pd.to_datetime(group.loc[group["doc_type"] == "post", "date"])
        if news_dates.empty or post_dates.empty:
            rows.append({"cluster": cluster, "median_lag_days": np.nan,
                         "n_post": len(post_dates)})
            continue
        first = news_dates.min()
        lags = (post_dates - first).dt.days
        rows.append({"cluster": cluster, "median_lag_days": float(lags.median()),
                     "n_post": len(post_dates)})
    return pd.DataFrame(rows)


def main() -> None:
    paths.FIGURES.mkdir(parents=True, exist_ok=True)
    corpus = build_corpus()
    raw_vectors = embed.encode(corpus["text"].tolist(), "s1_corpus")

    # --- 보정 전 상태를 먼저 기록한다 (결론이 보정에 의존하는지 밝히기 위해) ---
    raw_labels, _ = fit(raw_vectors, k=18, seed=0)
    before = corpus.assign(cluster=raw_labels)
    print("=== 문체 축 제거 전 (k=18 기준) ===")
    for name, score in label_silhouettes(raw_vectors, before).items():
        print(f"  {name}: {score:.3f}")
    print(f"  뉴스·게시글 혼합 클러스터 비율: {mixed_cluster_share(before):.1%}")

    # --- 문체 축 제거 ---
    vectors = remove_style_axis(raw_vectors, corpus["doc_type"].to_numpy())
    np.save(paths.PANELS / "corpus_vectors_style_removed.npy", vectors)

    table = choose_k(vectors)
    table.to_csv(paths.OUT / "s1_k_selection.csv", index=False)
    print("\n=== 문체 축 제거 후 k 선택 ===")
    print(table.to_string(index=False))

    # silhouette와 안정성을 동시에 만족하는 k를 고른다.
    # 자동 선택값은 제안일 뿐이며 사용자가 확정한다.
    table["score"] = table["silhouette"] * table["ari_stability"]
    k = int(table.loc[table["score"].idxmax(), "k"])
    print(f"\n제안 k = {k} (사용자 확정 필요)")

    labels, centroids = fit(vectors, k, seed=0)
    corpus["cluster"] = labels
    corpus.to_parquet(paths.PANELS / "corpus_clusters.parquet", index=False)
    np.save(paths.PANELS / "cluster_centroids.npy", centroids)

    diagnostics = label_silhouettes(vectors, corpus)
    print("\n=== 문체 축 제거 후 라벨별 silhouette ===")
    print("(날짜가 높으면 토픽이 아니라 시점으로 갈린 것,"
          " doc_type이 높으면 문체 제거가 부족한 것)")
    for name, score in diagnostics.items():
        print(f"  {name}: {score:.3f}")

    share = mixed_cluster_share(corpus)
    print(f"\n뉴스·게시글 혼합 클러스터 비율: {share:.1%}")
    if share == 0.0:
        print("  ⚠️ 혼합 클러스터가 하나도 없다. 아래 echo_ratio·transfer_lag은"
              " 구조적으로 전부 NaN이며 해석할 수 없다.")

    figures = plot_diagnostics(vectors, corpus, table)
    print(f"\n투영 기반 진단 그림 {len(figures)}장 저장:")
    for target in figures:
        print(f"  {target}")

    # 투영 없이 원공간 기하를 직접 재는 그림 3종. 산점도가 못 하는 일을 한다.
    original_figures, original_stats = original_space_diagnostics(
        vectors, corpus["cluster"].to_numpy())
    print(f"\n원공간 진단 그림 {len(original_figures)}장 저장:")
    for target in original_figures:
        print(f"  {target}")
    print("\n=== 원공간 기하 (투영 없음) ===")
    print(f"  같은 클러스터 평균 cosine : {original_stats['within_mean']:.3f}")
    print(f"  다른 클러스터 평균 cosine : {original_stats['between_mean']:.3f}")
    print(f"  분리도                    : {original_stats['separation']:+.3f}")
    print(f"  silhouette 음수인 점 비율 : {original_stats['negative_silhouette_share']:.1%}")
    print(f"  centroid 비대각 평균      : {original_stats['centroid_offdiag_mean']:.3f}")
    if original_stats["centroid_offdiag_mean"] > 0.9:
        print("  ⚠️ 클러스터 중심들이 서로 cosine 0.9 초과 — 사실상 한 덩어리다.")

    print("\n에코 비율:")
    print(echo_ratio(corpus).to_string(index=False))
    print("\n전이 지연:")
    print(transfer_lag(corpus).to_string(index=False))

    # 사람이 라벨을 채울 파일
    rows = []
    for cluster in sorted(corpus["cluster"].unique()):
        member_idx = np.where(labels == cluster)[0]
        distances = 1 - vectors[member_idx] @ centroids[cluster]
        for rank, position in enumerate(member_idx[np.argsort(distances)][:10], 1):
            rows.append({"cluster": cluster, "rank": rank,
                         "doc_type": corpus.iloc[position]["doc_type"],
                         "date": corpus.iloc[position]["date"],
                         "text": corpus.iloc[position]["text"][:200],
                         "label": ""})
    pd.DataFrame(rows).to_csv(paths.OUT / "cluster_labels.csv", index=False)
    print(f"\ncluster_labels.csv 생성 — 클러스터당 대표 10건. label 열을 채울 것")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s1_cluster.py -v`
Expected: 4 passed

- [ ] **Step 5: 실행**

Run: `analysis/community_belief_v10/.venv/bin/python analysis/community_belief_v10/s1_cluster.py`
Expected: k 선택표, 제안 k, 라벨별 silhouette, 에코 비율, 전이 지연 출력 + `cluster_labels.csv` 생성

- [ ] **Step 6: 사용자에게 k 확정과 라벨 작성 요청**

`s1_k_selection.csv`와 `cluster_labels.csv`를 사용자에게 보여주고 k를 확정받는다.
**날짜 silhouette이 cluster silhouette보다 높으면** 군집이 화제가 아니라 시점으로 갈린 것이므로, 그 사실을 REPORT에 명시하고 토픽 해석을 보류한다.

- [ ] **Step 7: 커밋 (사용자 승인 시에만 실행)**

```bash
git add analysis/community_belief_v10/s1_cluster.py analysis/community_belief_v10/tests/test_s1_cluster.py
git commit -m "analysis: S1 뉴스·게시글 공동 클러스터링 + 에코 비율 + 전이 지연"
```

---

## Task 10: S2 1층 — 인용 집계 (토픽 × belief 차원)

**Files:**
- Create: `analysis/community_belief_v10/s2_citation.py`
- Test: `analysis/community_belief_v10/tests/test_s2_citation.py`

**Interfaces:**
- Consumes: `panels/evidence_panel.parquet`, `panels/claim_panel.parquet`, `panels/corpus_clusters.parquet`, `panels/news_panel.parquet`
- Produces: `s2_citation.attach_labels(evidence, claims, corpus, news) -> pd.DataFrame`
  evidence 각 행에 **`topic_label`(주축)** 과 `cluster`(exploratory)를 부여.
  `community_claim` → `claim_stance`, `news`/`depth2_recent_search` → 뉴스 카테고리, `outcome` → `"outcome"`
- Produces: `s2_citation.label_by_dimension(linked) -> pd.DataFrame` — `topic_label × dim × relation` 인용 수
- Produces: `s2_citation.cluster_by_dimension(linked) -> pd.DataFrame` — 같은 표의 exploratory 판(주장에 사용 금지)
- Produces: `s2_citation.community_share(linked) -> pd.DataFrame` — `arm × layer × dim`별 커뮤니티 인용 비중
- Produces: `panels/evidence_with_labels.parquet`, `s2_label_by_dimension.csv`, `s2_cluster_by_dimension_exploratory.csv`, `s2_community_share.csv`

> **설계 변경 근거 (2026-08-03):** k-means 토픽 군집은 이 코퍼스에 존재하지 않는다는 것이
> 확인됐다(모든 시도 silhouette 0.03~0.07). 게시글 92.2%가 `trade_share` 한 종류이고
> 45거래일간 단일 종목 텍스트다. 따라서 주축을 봉인 데이터의 내장 라벨로 교체하고,
> 클러스터링 실패는 REPORT에 음성 결과로 보고한다. Task 11 이후의 `cluster_centroids.npy`
> 기반 토픽축 투영도 exploratory로 강등한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`analysis/community_belief_v10/tests/test_s2_citation.py`:

```python
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s2_citation


def _fixtures():
    evidence = pd.DataFrame([
        {"arm": "RN_COMM_ON", "agent_id": "A001", "turn": 5, "layer": "STB",
         "dim": "dim_4", "relation": "support",
         "evidence_id": "community_claim:A001:t005:01", "kind": "community_claim"},
        {"arm": "RN_COMM_ON", "agent_id": "A001", "turn": 5, "layer": "STB",
         "dim": "dim_1", "relation": "support",
         "evidence_id": "news_20260306_종목_aaa", "kind": "news"},
        {"arm": "RN_COMM_OFF", "agent_id": "A001", "turn": 5, "layer": "STB",
         "dim": "dim_1", "relation": "support",
         "evidence_id": "news_20260306_종목_aaa", "kind": "news"},
        {"arm": "RN_COMM_ON", "agent_id": "A001", "turn": 12, "layer": "LTB",
         "dim": "dim_6", "relation": "support",
         "evidence_id": "outcome:fill_A001_t010:h1", "kind": "outcome"},
    ])
    claims = pd.DataFrame([
        {"arm": "RN_COMM_ON", "claim_id": "community_claim:A001:t005:01",
         "source_post_ids": ["73"], "claim_stance": "bearish"},
    ])
    corpus = pd.DataFrame([
        {"doc_id": "post:73", "doc_type": "post", "post_id": "73",
         "article_id": None, "cluster": 2},
        {"doc_id": "news:news_20260306_종목_aaa", "doc_type": "news",
         "post_id": None, "article_id": "news_20260306_종목_aaa", "cluster": 5},
    ])
    news = pd.DataFrame([
        {"article_id": "news_20260306_종목_aaa", "category": "종목"},
    ])
    return evidence, claims, corpus, news


def test_community_claim_is_labelled_by_its_stance():
    """주축은 발견된 클러스터가 아니라 데이터에 내장된 stance다."""
    linked = s2_citation.attach_labels(*_fixtures())
    row = linked[linked["kind"] == "community_claim"]
    assert row["topic_label"].iloc[0] == "bearish"
    assert row["cluster"].iloc[0] == 2          # exploratory 열은 그대로 유지


def test_news_is_labelled_by_sealed_category():
    linked = s2_citation.attach_labels(*_fixtures())
    row = linked[(linked["kind"] == "news") & (linked["arm"] == "RN_COMM_ON")]
    assert row["topic_label"].iloc[0] == "종목"
    assert row["cluster"].iloc[0] == 5


def test_outcome_gets_its_own_label_not_na():
    """성과 피드백에는 토픽 축이 없다. NA로 두면 미할당과 구분되지 않는다."""
    linked = s2_citation.attach_labels(*_fixtures())
    row = linked[linked["kind"] == "outcome"]
    assert row["topic_label"].iloc[0] == "outcome"


def test_community_share_is_zero_for_off_arm():
    linked = s2_citation.attach_labels(*_fixtures())
    share = s2_citation.community_share(linked)
    off = share[share["arm"] == "RN_COMM_OFF"]
    assert (off["community_share"] == 0).all()


def test_label_by_dimension_crosstab():
    linked = s2_citation.attach_labels(*_fixtures())
    table = s2_citation.label_by_dimension(linked)
    hit = table[(table["topic_label"] == "bearish") & (table["dim"] == "dim_4")]
    assert hit["n"].iloc[0] == 1
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s2_citation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 's2_citation'`

- [ ] **Step 3: 구현**

```python
"""S2 1층: 인용 원장 집계. 임베딩 없이도 확정적으로 나오는 경로다.

커뮤니티 claim은 번호 기반 인용이라 supporting_quote가 구조적으로 verbatim이고
(EXPERIMENT_DESIGN.md §8.5), dimension_evidence_json에 claim ID가 남는다.
따라서 "어느 토픽의 글이 어느 belief 차원에 들어갔는가"는 추정이 아니라 사실이다.

title_only 노출은 규정상 claim·STB 근거로 쓸 수 없으므로(§8.4) 애초에
evidence_panel에 나타나지 않는다.
"""

import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api(offline=True)

import paths


def attach_labels(evidence: pd.DataFrame, claims: pd.DataFrame,
                  corpus: pd.DataFrame, news: pd.DataFrame) -> pd.DataFrame:
    """근거 하나하나에 라벨을 붙인다.

    ⚠️ 2026-08-03 설계 변경 — 발견된 클러스터를 주축에서 내렸다.

    S1의 k-means 토픽 군집은 이 코퍼스에 **존재하지 않는다**. 모든 시도에서
    silhouette이 0.03~0.07 구간(=구조 없음)에 머물렀다:
      공동 군집 보정 전 0.040 / 보정 후 0.027 / 뉴스만 0.045~0.074 /
      게시글만 0.045~0.051 / 게시글 post_type 라벨 -0.021(음수)
    원인은 데이터의 성질이다. 게시글 3,150건 중 2,906건(92.2%)이 `trade_share`
    한 종류로 "오늘 샀다/팔았다 + 이유"이고, 45거래일간 단일 종목 텍스트라
    화제가 갈릴 재료가 없다. 게시글의 실제 변이는 화제가 아니라 방향과 근거다.

    그래서 주축을 **봉인 데이터에 이미 있는 라벨**로 바꾼다. 연구자가 구조를
    발명하지 않는다는 점이 이 선택의 핵심이다.

      community_claim          → claim_stance (bullish / bearish / neutral)
      news, depth2_recent_search → 뉴스 카테고리 (종목 / 섹터 / 경제)
      outcome                  → "outcome" (성과 피드백에는 토픽 축이 없다)

    k-means `cluster`는 exploratory 열로 함께 남기되 주분석에 쓰지 않는다.
    클러스터링 실패 자체는 REPORT에 음성 결과로 보고한다.
    """
    post_cluster = dict(zip(corpus.loc[corpus["doc_type"] == "post", "post_id"],
                            corpus.loc[corpus["doc_type"] == "post", "cluster"]))
    news_cluster = dict(zip(corpus.loc[corpus["doc_type"] == "news", "article_id"],
                            corpus.loc[corpus["doc_type"] == "news", "cluster"]))
    claim_posts = dict(zip(claims["claim_id"], claims["source_post_ids"]))
    claim_stance = dict(zip(claims["claim_id"], claims["claim_stance"]))
    news_category = dict(zip(news["article_id"], news["category"]))

    def resolve(row):
        kind, evidence_id = row["kind"], row["evidence_id"]
        if kind == "community_claim":
            posts = claim_posts.get(evidence_id) or []
            clusters = [post_cluster[p] for p in posts if p in post_cluster]
            # 한 claim이 여러 글을 인용할 수 있다. 첫 출처를 대표로 쓰고
            # 다중 출처 여부는 별도 열로 남긴다.
            return (claim_stance.get(evidence_id, pd.NA),
                    clusters[0] if clusters else pd.NA, len(set(clusters)))
        if kind in ("news", "depth2_recent_search"):
            return (news_category.get(evidence_id, pd.NA),
                    news_cluster.get(evidence_id, pd.NA), 1)
        if kind == "outcome":
            return ("outcome", pd.NA, 0)
        return (pd.NA, pd.NA, 0)

    resolved = evidence.apply(resolve, axis=1, result_type="expand")
    out = evidence.copy()
    out["topic_label"] = resolved[0]     # 주축
    out["cluster"] = resolved[1]         # exploratory only
    out["n_source_clusters"] = resolved[2]
    return out


def label_by_dimension(linked: pd.DataFrame) -> pd.DataFrame:
    """주 산출물: 라벨 × belief 차원 × 관계 교차표.

    커뮤니티 쪽은 stance이므로 "bullish 주장이 어느 차원의 support로 들어갔나",
    뉴스 쪽은 카테고리이므로 "경제 기사가 dim_3에 몰리는가"를 바로 읽을 수 있다.
    """
    subset = linked[linked["topic_label"].notna()]
    return (subset.groupby(["arm", "layer", "topic_label", "dim", "relation", "kind"])
            .size().reset_index(name="n"))


def cluster_by_dimension(linked: pd.DataFrame) -> pd.DataFrame:
    """exploratory 전용. 군집 구조가 없다는 것이 확인됐으므로 주장에 쓰지 않는다."""
    subset = linked[linked["cluster"].notna()]
    return (subset.groupby(["arm", "layer", "cluster", "dim", "relation", "kind"])
            .size().reset_index(name="n"))


def community_share(linked: pd.DataFrame) -> pd.DataFrame:
    total = linked.groupby(["arm", "layer", "dim"]).size().rename("n_total")
    community = (linked[linked["kind"] == "community_claim"]
                 .groupby(["arm", "layer", "dim"]).size().rename("n_community"))
    out = pd.concat([total, community], axis=1).fillna(0).reset_index()
    out["community_share"] = out["n_community"] / out["n_total"]
    return out


def main() -> None:
    evidence = pd.read_parquet(paths.PANELS / "evidence_panel.parquet")
    claims = pd.read_parquet(paths.PANELS / "claim_panel.parquet")
    corpus = pd.read_parquet(paths.PANELS / "corpus_clusters.parquet")
    news = pd.read_parquet(paths.PANELS / "news_panel.parquet")

    linked = attach_labels(evidence, claims, corpus, news)

    # 라벨이 반드시 붙어야 하는 kind에서 하나라도 비면 조인이 깨진 것이다.
    # depth2_recent_search도 포함한다 — 실측(2026-08-03) 결과 D2 검색이 반환한
    # article_id는 ON 377개·OFF 394개 모두 봉인 뉴스 760개 안에 있다.
    labelled_kinds = ["news", "community_claim", "depth2_recent_search"]
    unlabelled = linked[linked["kind"].isin(labelled_kinds)
                        & linked["topic_label"].isna()]
    if len(unlabelled):
        raise AssertionError(
            f"라벨 미할당 근거 {len(unlabelled)}건: "
            f"{unlabelled['evidence_id'].head(5).tolist()}")

    linked.to_parquet(paths.PANELS / "evidence_with_labels.parquet", index=False)
    label_by_dimension(linked).to_csv(paths.OUT / "s2_label_by_dimension.csv",
                                      index=False)
    cluster_by_dimension(linked).to_csv(
        paths.OUT / "s2_cluster_by_dimension_exploratory.csv", index=False)
    share = community_share(linked)
    share.to_csv(paths.OUT / "s2_community_share.csv", index=False)

    print("=== 커뮤니티 인용 비중 (ON arm, burn-in 포함) ===")
    print(share[share["arm"] == "RN_COMM_ON"].to_string(index=False))
    print("\n※ dim_4가 커뮤니티의 주 수신 차원이라는 가설의 직접 검증 지점")

    on_claims = linked[(linked["arm"] == "RN_COMM_ON")
                       & (linked["kind"] == "community_claim")]
    print("\n=== stance × 차원 × 관계 (커뮤니티 주장이 어디로 갔는가) ===")
    print(pd.crosstab([on_claims["topic_label"], on_claims["relation"]],
                      [on_claims["layer"], on_claims["dim"]]).to_string())

    news_rows = linked[linked["kind"].isin(["news", "depth2_recent_search"])]
    print("\n=== 뉴스 카테고리 × 차원 (arm별) ===")
    print(pd.crosstab([news_rows["arm"], news_rows["topic_label"]],
                      [news_rows["layer"], news_rows["dim"]]).to_string())


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s2_citation.py -v`
Expected: 4 passed

- [ ] **Step 5: 실행**

Run: `analysis/community_belief_v10/.venv/bin/python analysis/community_belief_v10/s2_citation.py`
Expected: 차원별 커뮤니티 인용 비중 표 출력. **여기서 dim_4 가설의 첫 답이 나온다.**

- [ ] **Step 6: 커밋 (사용자 승인 시에만 실행)**

```bash
git add analysis/community_belief_v10/s2_citation.py analysis/community_belief_v10/tests/test_s2_citation.py
git commit -m "analysis: S2 1층 인용 집계 (토픽 × belief 차원)"
```

---

## Task 11: S2 2층 — belief 이동량

**Files:**
- Create: `analysis/community_belief_v10/s2_movement.py`
- Create: `analysis/community_belief_v10/s2_belief_figures.py`
- Test: `analysis/community_belief_v10/tests/test_s2_movement.py`
- Test: `analysis/community_belief_v10/tests/test_s2_belief_figures.py`

**Interfaces:**
- Consumes: `panels/belief_panel.parquet`, `panels/cluster_centroids.npy`, `embed.encode`
- Produces: `s2_movement.semantic_delta(panel, vectors) -> pd.DataFrame`
  컬럼: `arm, agent_id, turn, layer, dim, delta_semantic, prev_turn`
- Produces: `s2_movement.topic_delta(panel, vectors, centroids) -> pd.DataFrame`
  컬럼: `arm, agent_id, turn, layer, dim, cluster, delta_topic`
- Produces: `panels/movement_panel.parquet`, `panels/topic_movement_panel.parquet`

- [ ] **Step 1: 실패하는 테스트 작성**

`analysis/community_belief_v10/tests/test_s2_movement.py`:

```python
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s2_movement


def _panel():
    return pd.DataFrame([
        {"arm": "A", "agent_id": "A001", "turn": 1, "layer": "STB", "dim": "dim_1"},
        {"arm": "A", "agent_id": "A001", "turn": 2, "layer": "STB", "dim": "dim_1"},
        {"arm": "A", "agent_id": "A001", "turn": 3, "layer": "STB", "dim": "dim_1"},
    ])


def test_unchanged_text_gives_zero_delta():
    """패러프레이즈 강제가 없으므로 Δ≈0은 진짜 무변화다."""
    vectors = np.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    out = s2_movement.semantic_delta(_panel(), vectors)
    row2 = out[out["turn"] == 2].iloc[0]
    assert np.isclose(row2["delta_semantic"], 0.0, atol=1e-9)
    row3 = out[out["turn"] == 3].iloc[0]
    assert np.isclose(row3["delta_semantic"], 1.0, atol=1e-9)


def test_first_turn_has_no_delta():
    vectors = np.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    out = s2_movement.semantic_delta(_panel(), vectors)
    assert out[out["turn"] == 1]["delta_semantic"].isna().all()


def test_delta_is_computed_within_agent_layer_dim():
    """다른 agent/layer/dim의 값이 섞이면 안 된다."""
    panel = pd.DataFrame([
        {"arm": "A", "agent_id": "A001", "turn": 1, "layer": "STB", "dim": "dim_1"},
        {"arm": "A", "agent_id": "A002", "turn": 1, "layer": "STB", "dim": "dim_1"},
        {"arm": "A", "agent_id": "A002", "turn": 2, "layer": "STB", "dim": "dim_1"},
    ])
    vectors = np.array([[1.0, 0.0], [0.0, 1.0], [0.0, 1.0]])
    out = s2_movement.semantic_delta(panel, vectors)
    assert out[out["agent_id"] == "A001"]["delta_semantic"].isna().all()
    a002 = out[(out["agent_id"] == "A002") & (out["turn"] == 2)]
    assert np.isclose(a002["delta_semantic"].iloc[0], 0.0, atol=1e-9)


def test_topic_delta_sign():
    """belief가 centroid 쪽으로 가면 양수여야 한다."""
    panel = pd.DataFrame([
        {"arm": "A", "agent_id": "A001", "turn": 1, "layer": "STB", "dim": "dim_1"},
        {"arm": "A", "agent_id": "A001", "turn": 2, "layer": "STB", "dim": "dim_1"},
    ])
    vectors = np.array([[0.0, 1.0], [1.0, 0.0]])
    centroids = np.array([[1.0, 0.0]])
    out = s2_movement.topic_delta(panel, vectors, centroids)
    row = out[(out["turn"] == 2) & (out["cluster"] == 0)].iloc[0]
    assert row["delta_topic"] > 0
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s2_movement.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 's2_movement'`

- [ ] **Step 3: 구현**

```python
"""S2 2층: belief 텍스트의 이동량.

2026-07-31에 "여섯 차원을 매번 새 문장으로 재서술" 규칙이 제거되었으므로,
관점이 안 변한 차원은 이전 문장이 그대로 유지된다. 따라서 Δ≈0은 측정 노이즈가
아니라 진짜 무변화다 (ANALYSIS_FIELD_GUIDE.md §2-C). 이 성질이 이 분석의 근거다.

STB와 LTB는 층이 다르고, dim_6은 층 간 의미가 다르므로 절대 합치지 않는다.
"""

import numpy as np
import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api(offline=True)

import embed
import paths

GROUP_KEYS = ["arm", "agent_id", "layer", "dim"]


def _sorted_with_vectors(panel: pd.DataFrame, vectors: np.ndarray):
    """그룹키+turn으로 정렬하고, 같은 순서로 재배열한 벡터와
    '직전 행이 같은 그룹인가' 마스크를 함께 돌려준다."""
    work = panel.reset_index(drop=True).copy()
    work["_row"] = np.arange(len(work))
    work = work.sort_values(GROUP_KEYS + ["turn"]).reset_index(drop=True)
    ordered = vectors[work["_row"].to_numpy()]
    same_group = (work[GROUP_KEYS].shift().eq(work[GROUP_KEYS])
                  .all(axis=1).to_numpy())
    return work.drop(columns=["_row"]), ordered, same_group


def semantic_delta(panel: pd.DataFrame, vectors: np.ndarray) -> pd.DataFrame:
    work, ordered, same_group = _sorted_with_vectors(panel, vectors)
    cosine = np.full(len(work), np.nan)
    if len(work) > 1:
        cosine[1:] = np.einsum("ij,ij->i", ordered[1:], ordered[:-1])
    delta = 1.0 - cosine
    delta[~same_group] = np.nan          # 그룹의 첫 turn은 직전이 없다
    work["delta_semantic"] = delta
    work["prev_turn"] = work["turn"].shift().where(same_group)
    return work


def topic_delta(panel: pd.DataFrame, vectors: np.ndarray,
                centroids: np.ndarray) -> pd.DataFrame:
    work, ordered, same_group = _sorted_with_vectors(panel, vectors)
    similarity = ordered @ centroids.T          # (N, K)
    diff = np.full_like(similarity, np.nan)
    if len(work) > 1:
        diff[1:] = similarity[1:] - similarity[:-1]
    diff[~same_group] = np.nan
    wide = pd.DataFrame(diff, columns=list(range(centroids.shape[0])))
    keys = ["arm", "agent_id", "turn", "layer", "dim"]
    out = pd.concat([work[keys].reset_index(drop=True), wide], axis=1)
    out = out.melt(id_vars=keys, var_name="cluster", value_name="delta_topic")
    out["cluster"] = out["cluster"].astype(int)
    return out.dropna(subset=["delta_topic"]).reset_index(drop=True)


def main() -> None:
    panel = pd.read_parquet(paths.PANELS / "belief_panel.parquet")
    vectors = embed.encode(panel["text"].fillna("").tolist(), "s2_beliefs")
    centroids = np.load(paths.PANELS / "cluster_centroids.npy")

    movement = semantic_delta(panel, vectors)
    movement.to_parquet(paths.PANELS / "movement_panel.parquet", index=False)

    zero_rate = (movement["delta_semantic"].dropna() < 1e-9).mean()
    print(f"movement_panel {len(movement):,}행 | Δ=0(진짜 무변화) 비율 {zero_rate:.1%}")

    topic = topic_delta(panel, vectors, centroids)
    topic.to_parquet(paths.PANELS / "topic_movement_panel.parquet", index=False)
    print(f"topic_movement_panel {len(topic):,}행")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s2_movement.py -v`
Expected: 4 passed

- [ ] **Step 5: 실행 (20~40분 소요)**

Run: `analysis/community_belief_v10/.venv/bin/python analysis/community_belief_v10/s2_movement.py`
Expected: `movement_panel 217,200행` + Δ=0 비율 출력

`topic_movement_panel`은 217,200 × K행이 되므로 K=12면 약 260만 행이다.
메모리가 부족하면 arm별로 나눠 저장하도록 `main()`을 수정한다.

- [ ] **Step 6: belief 변화 시각화 3종 — 실패하는 테스트 작성**

`analysis/community_belief_v10/tests/test_s2_belief_figures.py`:

```python
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s2_belief_figures


def _paired_panel():
    rows = []
    for arm in ("RN_COMM_ON", "RN_COMM_OFF"):
        for turn in (7, 8):
            rows.append({"arm": arm, "agent_id": "A001", "turn": turn,
                         "layer": "STB", "dim": "dim_4", "date": "2026-03-05"})
    return pd.DataFrame(rows)


def test_arm_divergence_pairs_identical_keys():
    """같은 (agent, turn, layer, dim)의 ON/OFF 벡터 거리를 잰다."""
    panel = _paired_panel()
    # ON turn7 == OFF turn7 (거리 0), ON turn8 ⟂ OFF turn8 (거리 1)
    vectors = np.array([[1.0, 0.0], [1.0, 0.0],      # ON  t7, t8
                        [1.0, 0.0], [0.0, 1.0]])     # OFF t7, t8
    out = s2_belief_figures.arm_divergence(panel, vectors)
    assert len(out) == 2
    assert np.isclose(out.loc[out["turn"] == 7, "divergence"].iloc[0], 0.0)
    assert np.isclose(out.loc[out["turn"] == 8, "divergence"].iloc[0], 1.0)


def test_arm_divergence_fails_when_keys_do_not_pair():
    import pytest
    panel = _paired_panel().drop(index=3).reset_index(drop=True)
    vectors = np.eye(2)[[0, 0, 0]]
    with pytest.raises(AssertionError, match="짝"):
        s2_belief_figures.arm_divergence(panel, vectors)


def test_stance_axis_points_from_bearish_to_bullish():
    claims = pd.DataFrame({"claim_stance": ["bullish", "bullish",
                                            "bearish", "bearish"]})
    vectors = np.array([[1.0, 0.0], [1.0, 0.0], [-1.0, 0.0], [-1.0, 0.0]])
    axis = s2_belief_figures.stance_axis(claims, vectors)
    assert np.isclose(np.linalg.norm(axis), 1.0)
    assert axis[0] > 0.99          # bullish 쪽이 +


def test_stance_axis_requires_both_stances():
    import pytest
    claims = pd.DataFrame({"claim_stance": ["bullish", "neutral"]})
    vectors = np.array([[1.0, 0.0], [0.0, 1.0]])
    with pytest.raises(AssertionError, match="stance"):
        s2_belief_figures.stance_axis(claims, vectors)


def test_project_on_stance_returns_scalar_per_row():
    panel = _paired_panel()
    vectors = np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 0.0], [0.0, 1.0]])
    axis = np.array([1.0, 0.0])
    out = s2_belief_figures.project_on_stance(panel, vectors, axis)
    assert len(out) == len(panel)
    assert np.isclose(out["stance_score"].iloc[0], 1.0)
    assert np.isclose(out["stance_score"].iloc[1], 0.0)


def test_all_three_figures_are_written(tmp_path, monkeypatch):
    monkeypatch.setattr(s2_belief_figures.paths, "FIGURES", tmp_path)
    divergence = pd.DataFrame({"turn": [7, 8, 7, 8],
                               "layer": ["STB"] * 4,
                               "dim": ["dim_1", "dim_1", "dim_4", "dim_4"],
                               "divergence": [0.1, 0.2, 0.3, 0.5]})
    stance = pd.DataFrame({"arm": ["RN_COMM_ON"] * 2 + ["RN_COMM_OFF"] * 2,
                           "date": ["2026-03-05", "2026-03-06"] * 2,
                           "layer": ["STB"] * 4, "dim": ["dim_1"] * 4,
                           "stance_score": [0.2, 0.3, 0.1, 0.1]})
    movement = pd.DataFrame({"arm": ["RN_COMM_ON"] * 4,
                             "agent_id": ["A001", "A001", "A002", "A002"],
                             "turn": [7, 8, 7, 8], "layer": ["STB"] * 4,
                             "dim": ["dim_4"] * 4,
                             "delta_semantic": [0.1, 0.4, 0.2, 0.0]})
    written = s2_belief_figures.plot_all(divergence, stance, movement)
    assert {t.name for t in written} == {
        "s2_arm_divergence.png", "s2_stance_axis_timeline.png",
        "s2_change_heatmap.png"}
    for target in written:
        assert target.exists() and target.stat().st_size > 0
```

- [ ] **Step 7: 테스트 실패 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s2_belief_figures.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 's2_belief_figures'`

- [ ] **Step 8: 구현**

```python
"""belief 변화 시각화 3종. 전부 임의 차원축소 없이 그린다.

왜 산점도가 아닌가:
  raw belief '위치'를 2~3차원에 투영하면 S1 코퍼스와 같은 함정에 빠진다
  (PCA-3D는 분산 8.5%만 담고, 그 안에서 좋아 보이는 군집이 원공간에서는
  silhouette 음수였다). 그러나 이 연구가 보고 싶은 것은 위치가 아니라 **변화**이고,
  변화는 투영 없이 스칼라로 잴 수 있다.

세 그림:
  1) arm_divergence  — 같은 (agent, turn, layer, dim)에서 ON belief와 OFF belief
     사이의 cosine 거리. 두 arm은 seed·뉴스·cohort·prompt가 동일하고
     community_mode만 다르므로, 이 거리가 곧 커뮤니티가 만든 차이다. 투영 없음.
  2) stance_axis     — bullish claim 평균 − bearish claim 평균으로 만든 단일 축.
     연구자가 고른 축이 아니라 데이터가 스스로 라벨한 축이다(claim_stance).
     belief를 여기 투영하면 "얼마나 강세 쪽인가"라는 해석 가능한 스칼라가 된다.
  3) change heatmap  — agent × turn 격자에 Δ_semantic. 세로 줄무늬면 뉴스 충격에
     동조한 것이고, 흩어져 있으면 개인차다. 투영 없음.

그림 안 글자는 영어로 쓴다(matplotlib 기본 폰트에 한글 글리프 없음).
"""

import numpy as np
import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api(offline=True)

import paths

PAIR_KEYS = ["agent_id", "turn", "layer", "dim"]


def arm_divergence(panel: pd.DataFrame, vectors: np.ndarray) -> pd.DataFrame:
    work = panel.reset_index(drop=True).copy()
    work["_row"] = np.arange(len(work))
    on = work[work["arm"] == "RN_COMM_ON"].set_index(PAIR_KEYS)
    off = work[work["arm"] == "RN_COMM_OFF"].set_index(PAIR_KEYS)
    if set(on.index) != set(off.index):
        raise AssertionError(
            f"ON/OFF 짝이 맞지 않는다 — ON만 {len(set(on.index) - set(off.index))}, "
            f"OFF만 {len(set(off.index) - set(on.index))}")
    off = off.loc[on.index]
    similarity = np.einsum("ij,ij->i",
                           vectors[on["_row"].to_numpy()],
                           vectors[off["_row"].to_numpy()])
    out = on.reset_index()[PAIR_KEYS].copy()
    out["divergence"] = 1.0 - similarity
    return out


def stance_axis(claims: pd.DataFrame, vectors: np.ndarray) -> np.ndarray:
    bullish = claims["claim_stance"].to_numpy() == "bullish"
    bearish = claims["claim_stance"].to_numpy() == "bearish"
    if not bullish.any() or not bearish.any():
        raise AssertionError(
            "stance 축을 만들 수 없다 — bullish 또는 bearish claim이 없다")
    axis = vectors[bullish].mean(axis=0) - vectors[bearish].mean(axis=0)
    norm = float(np.linalg.norm(axis))
    if norm == 0.0:
        raise AssertionError("stance 축이 영벡터다")
    return axis / norm


def project_on_stance(panel: pd.DataFrame, vectors: np.ndarray,
                      axis: np.ndarray) -> pd.DataFrame:
    out = panel.reset_index(drop=True).copy()
    out["stance_score"] = vectors @ axis
    return out


def plot_all(divergence: pd.DataFrame, stance: pd.DataFrame,
             movement: pd.DataFrame) -> list:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    paths.FIGURES.mkdir(parents=True, exist_ok=True)
    written = []

    # 1) ON/OFF 짝 발산 곡선 — 차원별
    figure, axis_obj = plt.subplots(figsize=(9, 5.5))
    for (layer, dim), group in divergence.groupby(["layer", "dim"]):
        series = group.groupby("turn")["divergence"].mean().sort_index()
        axis_obj.plot(series.index, series.values, marker="", linewidth=1.4,
                      label=f"{layer}/{dim}")
    axis_obj.set_xlabel("turn (AM/PM events, 1-90)")
    axis_obj.set_ylabel("cosine distance between ON and OFF belief")
    axis_obj.set_title("Paired ON/OFF belief divergence "
                       "(same agent, same turn, same news)")
    axis_obj.legend(fontsize=8, ncol=2)
    axis_obj.grid(alpha=0.3)
    figure.tight_layout()
    target = paths.FIGURES / "s2_arm_divergence.png"
    figure.savefig(target, dpi=150); plt.close(figure); written.append(target)

    # 2) 강세-약세 축 타임라인 — arm별
    figure, axis_obj = plt.subplots(figsize=(9, 5.5))
    for arm, group in stance.groupby("arm"):
        series = group.groupby("date")["stance_score"].mean().sort_index()
        axis_obj.plot(range(len(series)), series.values, linewidth=1.6, label=arm)
    axis_obj.axhline(0.0, color="black", linewidth=0.8)
    axis_obj.set_xlabel("trading day")
    axis_obj.set_ylabel("projection on bullish(+) / bearish(-) axis")
    axis_obj.set_title("Belief position on the community stance axis")
    axis_obj.legend()
    axis_obj.grid(alpha=0.3)
    figure.tight_layout()
    target = paths.FIGURES / "s2_stance_axis_timeline.png"
    figure.savefig(target, dpi=150); plt.close(figure); written.append(target)

    # 3) agent × turn 변화량 히트맵 — arm 나란히
    arms = sorted(movement["arm"].unique())
    figure, axes = plt.subplots(1, len(arms), figsize=(7 * len(arms), 6),
                                squeeze=False)
    for index, arm in enumerate(arms):
        grid = (movement[movement["arm"] == arm]
                .pivot_table(index="agent_id", columns="turn",
                             values="delta_semantic", aggfunc="mean"))
        image = axes[0][index].imshow(grid.values, aspect="auto", cmap="magma")
        axes[0][index].set_title(f"{arm} — belief change per agent-turn")
        axes[0][index].set_xlabel("turn")
        axes[0][index].set_ylabel("agent")
        figure.colorbar(image, ax=axes[0][index])
    figure.tight_layout()
    target = paths.FIGURES / "s2_change_heatmap.png"
    figure.savefig(target, dpi=150); plt.close(figure); written.append(target)
    return written


def main() -> None:
    import embed

    panel = pd.read_parquet(paths.PANELS / "belief_panel.parquet")
    vectors = embed.encode(panel["text"].fillna("").tolist(), "s2_beliefs")

    divergence = arm_divergence(panel, vectors)
    divergence.to_parquet(paths.PANELS / "arm_divergence.parquet", index=False)

    claims = pd.read_parquet(paths.PANELS / "claim_panel.parquet")
    claim_vectors = embed.encode(claims["claim_text"].fillna("").tolist(),
                                 "s2_claims")
    axis = stance_axis(claims, claim_vectors)
    stance = project_on_stance(panel, vectors, axis)
    stance.to_parquet(paths.PANELS / "stance_projection.parquet", index=False)

    movement = pd.read_parquet(paths.PANELS / "movement_panel.parquet")
    written = plot_all(divergence, stance,
                       movement[movement["delta_semantic"].notna()])

    print(f"belief 변화 그림 {len(written)}장 저장:")
    for target in written:
        print(f"  {target}")
    print("\n=== 차원별 평균 ON/OFF 발산 (burn-in 제외) ===")
    main_window = divergence[~divergence["turn"].isin(paths.BURNIN_TURNS)]
    print(main_window.groupby(["layer", "dim"])["divergence"]
          .agg(["mean", "std"]).to_string())


if __name__ == "__main__":
    main()
```

- [ ] **Step 9: 테스트 통과 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s2_belief_figures.py -v`
Expected: 6 passed

- [ ] **Step 10: 실행**

Run: `analysis/community_belief_v10/.venv/bin/python analysis/community_belief_v10/s2_belief_figures.py`
Expected: 그림 3장 + 차원별 평균 ON/OFF 발산 표. `s2_beliefs` 캐시가 이미 있으면 빠르게 끝난다.

- [ ] **Step 11: 커밋 (사용자 승인 시에만 실행)**

```bash
git add analysis/community_belief_v10/s2_movement.py analysis/community_belief_v10/s2_belief_figures.py analysis/community_belief_v10/tests/test_s2_movement.py analysis/community_belief_v10/tests/test_s2_belief_figures.py
git commit -m "analysis: S2 2층 belief 이동량 + 변화 시각화 3종"
```

---

## Task 12: S3 — ON/OFF paired 대조

**Files:**
- Create: `analysis/community_belief_v10/s3_contrast.py`
- Test: `analysis/community_belief_v10/tests/test_s3_contrast.py`

**Interfaces:**
- Consumes: `panels/movement_panel.parquet`, `panels/evidence_with_cluster.parquet`
- Produces: `s3_contrast.pair_arms(movement) -> pd.DataFrame` — 컬럼 `agent_id, turn, layer, dim, delta_on, delta_off, paired_diff`
- Produces: `s3_contrast.bootstrap_by_agent(df, value_col, n=2000, seed=0) -> dict` — `{"mean", "ci_low", "ci_high"}`
- Produces: `s3_contrast.by_turn_block(paired) -> pd.DataFrame` — 초/중/후반 구간별 요약
- Produces: `s3_contrast.first_exposure_effect(paired, first_turn) -> dict`
- Produces: `s3_contrast.csv`, `figures/s3_*.png`

- [ ] **Step 1: 실패하는 테스트 작성**

`analysis/community_belief_v10/tests/test_s3_contrast.py`:

```python
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s3_contrast


def _movement():
    rows = []
    for arm, base in (("RN_COMM_ON", 0.5), ("RN_COMM_OFF", 0.2)):
        for agent in ("A001", "A002"):
            for turn in (7, 8):
                rows.append({"arm": arm, "agent_id": agent, "turn": turn,
                             "layer": "STB", "dim": "dim_4",
                             "delta_semantic": base, "is_burnin": False})
    return pd.DataFrame(rows)


def test_pair_arms_produces_one_row_per_key():
    paired = s3_contrast.pair_arms(_movement())
    assert len(paired) == 4
    assert np.allclose(paired["paired_diff"], 0.3)


def test_pair_arms_fails_on_unmatched_keys():
    import pytest
    movement = _movement()
    movement = movement[~((movement["arm"] == "RN_COMM_OFF")
                          & (movement["turn"] == 8))]
    with pytest.raises(AssertionError, match="짝"):
        s3_contrast.pair_arms(movement)


def test_bootstrap_resamples_agents_not_rows():
    """agent-turn 행을 독립 표본으로 취급하면 안 된다."""
    df = pd.DataFrame({"agent_id": ["A001"] * 50 + ["A002"] * 50,
                       "value": [1.0] * 50 + [3.0] * 50})
    result = s3_contrast.bootstrap_by_agent(df, "value", n=500, seed=0)
    assert np.isclose(result["mean"], 2.0)
    # agent 2개뿐이므로 CI가 넓어야 한다. 행 단위 재표본이면 거의 0폭이 된다.
    assert result["ci_high"] - result["ci_low"] > 0.5


def test_by_turn_block_splits_into_three():
    paired = s3_contrast.pair_arms(_movement())
    paired = pd.concat([paired.assign(turn=t) for t in (10, 45, 85)],
                       ignore_index=True)
    blocks = s3_contrast.by_turn_block(paired)
    assert set(blocks["block"]) == {"초반", "중반", "후반"}
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s3_contrast.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 's3_contrast'`

- [ ] **Step 3: 구현**

```python
"""S3: ON/OFF paired 대조.

seed·뉴스·cohort·prompt·모델이 같고 community_mode만 다르므로
(agent_id, turn, layer, dim)이 1:1로 대응한다.

⚠️ 경로 발산: t=2 이후 두 arm의 belief 경로가 갈라지므로 t가 클수록
추정치는 "커뮤니티 처치효과 + 누적 경로 발산"의 혼합이다. 그래서 turn 구간별로
나눠 보고하고, 첫 Best 수신 turn의 즉시 효과를 가장 깨끗한 추정치로 따로 낸다.
후반 차이를 순수 처치효과로 주장하지 않는다.

⚠️ 통계 단위: agent 수준 클러스터 부트스트랩. agent-turn 행은 독립 실험
반복이 아니다 (total_deviation_spec.md §4).
"""

import numpy as np
import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api(offline=True)

import paths

PAIR_KEYS = ["agent_id", "turn", "layer", "dim"]
# 사전지정 주지표. 결과를 보고 고르지 않는다.
PRIMARY = {"layer": "STB", "dim": "dim_4"}


def pair_arms(movement: pd.DataFrame) -> pd.DataFrame:
    on = movement[movement["arm"] == "RN_COMM_ON"].set_index(PAIR_KEYS)
    off = movement[movement["arm"] == "RN_COMM_OFF"].set_index(PAIR_KEYS)
    if set(on.index) != set(off.index):
        only_on, only_off = len(set(on.index) - set(off.index)), len(
            set(off.index) - set(on.index))
        raise AssertionError(f"ON/OFF 짝 불일치 — ON만 {only_on}, OFF만 {only_off}")
    paired = pd.DataFrame({
        "delta_on": on["delta_semantic"],
        "delta_off": off.loc[on.index, "delta_semantic"],
    }).reset_index()
    paired["paired_diff"] = paired["delta_on"] - paired["delta_off"]
    return paired


def bootstrap_by_agent(df: pd.DataFrame, value_col: str, n: int = 2000,
                       seed: int = 0) -> dict:
    agents = df["agent_id"].unique()
    by_agent = {a: df.loc[df["agent_id"] == a, value_col].to_numpy()
                for a in agents}
    rng = np.random.default_rng(seed)
    means = np.empty(n)
    for i in range(n):
        picked = rng.choice(agents, size=len(agents), replace=True)
        means[i] = np.concatenate([by_agent[a] for a in picked]).mean()
    observed = df[value_col].mean()
    return {"mean": float(observed),
            "ci_low": float(np.percentile(means, 2.5)),
            "ci_high": float(np.percentile(means, 97.5)),
            "n_agents": int(len(agents)), "n_rows": int(len(df))}


def by_turn_block(paired: pd.DataFrame) -> pd.DataFrame:
    def block(turn):
        if turn <= 30:
            return "초반"
        if turn <= 60:
            return "중반"
        return "후반"

    work = paired.copy()
    work["block"] = work["turn"].map(block)
    rows = []
    for (blk, layer, dim), group in work.groupby(["block", "layer", "dim"]):
        stats = bootstrap_by_agent(group.dropna(subset=["paired_diff"]),
                                   "paired_diff")
        rows.append({"block": blk, "layer": layer, "dim": dim, **stats})
    return pd.DataFrame(rows)


def first_exposure_effect(paired: pd.DataFrame, first_turn: int) -> dict:
    """가장 깨끗한 추정치: 첫 Best 수신 turn의 즉시 효과."""
    subset = paired[(paired["turn"] == first_turn)
                    & paired["paired_diff"].notna()]
    if subset.empty:
        raise AssertionError(f"turn {first_turn}에 짝 데이터가 없다")
    out = {}
    for (layer, dim), group in subset.groupby(["layer", "dim"]):
        out[f"{layer}/{dim}"] = bootstrap_by_agent(group, "paired_diff")
    return out


def main() -> None:
    movement = pd.read_parquet(paths.PANELS / "movement_panel.parquet")
    movement = movement[movement["delta_semantic"].notna()]
    paired = pair_arms(movement)
    paired.to_parquet(paths.PANELS / "paired_movement.parquet", index=False)

    main_analysis = paired[~paired["turn"].isin(paths.BURNIN_TURNS)]

    print("=== 사전지정 주지표: STB dim_4 ===")
    primary = main_analysis[(main_analysis["layer"] == PRIMARY["layer"])
                            & (main_analysis["dim"] == PRIMARY["dim"])]
    print(bootstrap_by_agent(primary.dropna(subset=["paired_diff"]), "paired_diff"))

    print("\n=== 차원별 (burn-in 제외) ===")
    rows = []
    for (layer, dim), group in main_analysis.groupby(["layer", "dim"]):
        rows.append({"layer": layer, "dim": dim,
                     **bootstrap_by_agent(group.dropna(subset=["paired_diff"]),
                                          "paired_diff")})
    summary = pd.DataFrame(rows)
    print(summary.to_string(index=False))
    summary.to_csv(paths.OUT / "s3_contrast.csv", index=False)

    print("\n=== turn 구간별 (경로 발산 확인) ===")
    blocks = by_turn_block(main_analysis)
    print(blocks.to_string(index=False))
    blocks.to_csv(paths.OUT / "s3_turn_blocks.csv", index=False)

    # 첫 Best 수신 turn을 데이터에서 확정한다 (day1은 전날 Best가 없다)
    evidence = pd.read_parquet(paths.PANELS / "evidence_with_cluster.parquet")
    community = evidence[(evidence["arm"] == "RN_COMM_ON")
                         & (evidence["kind"] == "community_claim")]
    first_turn = int(community["turn"].min())
    print(f"\n=== 첫 커뮤니티 인용 turn = {first_turn} (가장 깨끗한 추정치) ===")
    for key, stats in first_exposure_effect(paired, first_turn).items():
        print(f"  {key}: {stats}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s3_contrast.py -v`
Expected: 4 passed

- [ ] **Step 5: 실행**

Run: `analysis/community_belief_v10/.venv/bin/python analysis/community_belief_v10/s3_contrast.py`
Expected: 주지표 / 차원별 / 구간별 / 첫 노출 효과 4개 블록 출력

- [ ] **Step 6: 커밋 (사용자 승인 시에만 실행)**

```bash
git add analysis/community_belief_v10/s3_contrast.py analysis/community_belief_v10/tests/test_s3_contrast.py
git commit -m "analysis: S3 ON/OFF paired 대조 + agent 클러스터 부트스트랩"
```

---

## Task 13: S3 — 거래 방향과 외부 검증

**Files:**
- Create: `analysis/community_belief_v10/s3_behavior.py`
- Test: `analysis/community_belief_v10/tests/test_s3_behavior.py`

**Interfaces:**
- Consumes: `panels/action_panel.parquet`, 사용자 제공 실제 개인 순매수 CSV
- Produces: `s3_behavior.daily_direction(actions) -> pd.DataFrame` — 컬럼 `arm, date, signed_value_sum, direction`
- Produces: `s3_behavior.load_actual(path) -> pd.DataFrame` — 컬럼 `date, actual_net_value, actual_direction`
- Produces: `s3_behavior.agreement(simulated, actual) -> pd.DataFrame` — arm별 방향 일치율, always-buy/always-sell baseline
- Produces: `s3_behavior.csv`

**⚠️ 선행 조건:** 실제 삼성전자 개인 순매수 일별 데이터가 필요하다. 사용자에게 파일 경로를 받는다.
기대 형식: CSV, 열 `date`(YYYY-MM-DD)와 `net_value`(개인 순매수 대금, 매수 우위 시 양수).
**파일이 없으면 Task 13은 건너뛰고 REPORT에 "외부 검증 미실시"로 명시한다. 대충 만든 대체 데이터를 쓰지 않는다.**

- [ ] **Step 1: 실패하는 테스트 작성**

`analysis/community_belief_v10/tests/test_s3_behavior.py`:

```python
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s3_behavior


def _actions():
    return pd.DataFrame([
        {"arm": "RN_COMM_ON", "date": "2026-03-10", "signed_value": 100.0, "is_burnin": False},
        {"arm": "RN_COMM_ON", "date": "2026-03-10", "signed_value": -30.0, "is_burnin": False},
        {"arm": "RN_COMM_ON", "date": "2026-03-11", "signed_value": -50.0, "is_burnin": False},
        {"arm": "RN_COMM_OFF", "date": "2026-03-10", "signed_value": -10.0, "is_burnin": False},
        {"arm": "RN_COMM_OFF", "date": "2026-03-11", "signed_value": -50.0, "is_burnin": False},
    ])


def test_daily_direction_aggregates_am_and_pm():
    out = s3_behavior.daily_direction(_actions())
    row = out[(out["arm"] == "RN_COMM_ON") & (out["date"] == "2026-03-10")].iloc[0]
    assert row["signed_value_sum"] == 70.0
    assert row["direction"] == 1


def test_agreement_computes_per_arm_and_baselines():
    simulated = s3_behavior.daily_direction(_actions())
    actual = pd.DataFrame([
        {"date": "2026-03-10", "actual_net_value": 5.0, "actual_direction": 1},
        {"date": "2026-03-11", "actual_net_value": -5.0, "actual_direction": -1},
    ])
    out = s3_behavior.agreement(simulated, actual).set_index("arm")
    assert out.loc["RN_COMM_ON", "agreement_rate"] == 1.0
    assert out.loc["RN_COMM_OFF", "agreement_rate"] == 0.5
    assert out.loc["RN_COMM_ON", "always_buy_baseline"] == 0.5


def test_load_actual_rejects_missing_columns(tmp_path):
    import pytest
    bad = tmp_path / "bad.csv"
    bad.write_text("date,value\n2026-03-10,1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="net_value"):
        s3_behavior.load_actual(bad)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s3_behavior.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 's3_behavior'`

- [ ] **Step 3: 구현**

```python
"""S3 행동 축: 일별 거래 방향과 실제 개인 순매수 방향의 비교.

primary behavioral metric은 일별 AM+PM gross signed fill value의 방향과
실제 삼성전자 Individuals 일별 순거래대금 방향의 비교다
(EXPERIMENT_DESIGN.md §10). AM-only/PM-only는 실제 intraday target이 없어
탐색 분석으로만 쓴다.

⚠️ hold가 비활성이라 방향 비교만 가능하고 거래 빈도 비교는 불가하다.
⚠️ burn-in 3거래일은 t1 강제 전원매수를 주분석에서 제외한다.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api(offline=True)

import paths

REQUIRED_ACTUAL_COLUMNS = {"date", "net_value"}


def daily_direction(actions: pd.DataFrame) -> pd.DataFrame:
    out = (actions.groupby(["arm", "date"])["signed_value"].sum()
           .reset_index(name="signed_value_sum"))
    out["direction"] = np.sign(out["signed_value_sum"]).astype(int)
    return out


def load_actual(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = REQUIRED_ACTUAL_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(
            f"실제 개인 순매수 파일에 필요한 열이 없다: {sorted(missing)}. "
            f"필요 열: date, net_value")
    df = df.rename(columns={"net_value": "actual_net_value"})
    df["actual_direction"] = np.sign(df["actual_net_value"]).astype(int)
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df[["date", "actual_net_value", "actual_direction"]]


def agreement(simulated: pd.DataFrame, actual: pd.DataFrame) -> pd.DataFrame:
    merged = simulated.merge(actual, on="date", how="inner")
    if merged.empty:
        raise AssertionError("시뮬레이션 날짜와 실제 데이터 날짜가 겹치지 않는다")
    rows = []
    for arm, group in merged.groupby("arm"):
        rows.append({
            "arm": arm,
            "n_days": len(group),
            "agreement_rate": float((group["direction"] == group["actual_direction"]).mean()),
            "always_buy_baseline": float((group["actual_direction"] == 1).mean()),
            "always_sell_baseline": float((group["actual_direction"] == -1).mean()),
            "buy_day_recall": float(
                (group.loc[group["actual_direction"] == 1, "direction"] == 1).mean()
                if (group["actual_direction"] == 1).any() else np.nan),
            "sell_day_recall": float(
                (group.loc[group["actual_direction"] == -1, "direction"] == -1).mean()
                if (group["actual_direction"] == -1).any() else np.nan),
        })
    return pd.DataFrame(rows)


def main(actual_path: str) -> None:
    actions = pd.read_parquet(paths.PANELS / "action_panel.parquet")
    actions = actions[~actions["is_burnin"]]   # burn-in 3거래일 제외
    simulated = daily_direction(actions)
    actual = load_actual(Path(actual_path))
    result = agreement(simulated, actual)
    result.to_csv(paths.OUT / "s3_behavior.csv", index=False)
    print(result.to_string(index=False))
    print("\n※ hold 비활성 → 방향 비교만 유효. 거래 빈도 비교는 불가(논문 한계)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(
            "사용법: python s3_behavior.py <실제_개인순매수_CSV>\n"
            "필요 열: date(YYYY-MM-DD), net_value(매수 우위 시 양수)\n"
            "파일이 없으면 이 단계를 건너뛰고 REPORT에 '외부 검증 미실시'로 적을 것.")
    main(sys.argv[1])
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s3_behavior.py -v`
Expected: 4 passed

- [ ] **Step 5: 사용자에게 실제 데이터 요청 후 실행**

Run: `analysis/community_belief_v10/.venv/bin/python analysis/community_belief_v10/s3_behavior.py <경로>`
Expected: arm별 일치율 + always-buy/always-sell baseline 표

**파일을 못 받으면 이 Step은 건너뛰고 REPORT에 미실시로 기록한다.**

- [ ] **Step 6: 커밋 (사용자 승인 시에만 실행)**

```bash
git add analysis/community_belief_v10/s3_behavior.py analysis/community_belief_v10/tests/test_s3_behavior.py
git commit -m "analysis: S3 일별 거래 방향 + 실제 개인 순매수 외부 검증"
```

---

## Task 14: S4 — 메커니즘 (도달 대비 채택률)

**Files:**
- Create: `analysis/community_belief_v10/s4_mechanism.py`
- Test: `analysis/community_belief_v10/tests/test_s4_mechanism.py`

**Interfaces:**
- Consumes: `panels/exposure_panel.parquet`, `panels/claim_panel.parquet`, `panels/evidence_with_cluster.parquet`, `panels/post_panel.parquet`, `panels/corpus_clusters.parquet`
- Produces: `s4_mechanism.reach_by_post(exposure) -> pd.DataFrame` — `post_id, n_full_body, n_title_only`
- Produces: `s4_mechanism.adopt_by_post(claims, evidence) -> pd.DataFrame` — `post_id, n_cited`
- Produces: `s4_mechanism.adoption_rate(reach, adopt, posts, corpus) -> pd.DataFrame` — `post_id, cluster, is_best, n_full_body, n_cited, adoption_rate`
- Produces: `s4_mechanism.best_self_exclusion_contrast(exposure) -> pd.DataFrame`
- Produces: `s4_mechanism.reaction_vs_citation(exposure, claims) -> pd.DataFrame`
- Produces: `s4_mechanism.csv`

- [ ] **Step 1: 실패하는 테스트 작성**

`analysis/community_belief_v10/tests/test_s4_mechanism.py`:

```python
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s4_mechanism


def _exposure():
    return pd.DataFrame([
        {"post_id": 73, "agent_id": "A001", "exposure_level": "full_body",
         "reaction": "like", "is_best": True, "turn": 5},
        {"post_id": 73, "agent_id": "A002", "exposure_level": "full_body",
         "reaction": "unlike", "is_best": True, "turn": 5},
        {"post_id": 73, "agent_id": "A003", "exposure_level": "title_only",
         "reaction": None, "is_best": False, "turn": 4},
        {"post_id": 99, "agent_id": "A001", "exposure_level": "full_body",
         "reaction": "none", "is_best": False, "turn": 5},
    ])


def test_reach_separates_title_only_from_full_body():
    """두 노출 수준은 절대 합산하지 않는다."""
    out = s4_mechanism.reach_by_post(_exposure()).set_index("post_id")
    assert out.loc[73, "n_full_body"] == 2
    assert out.loc[73, "n_title_only"] == 1
    assert out.loc[99, "n_full_body"] == 1
    assert out.loc[99, "n_title_only"] == 0


def test_adopt_counts_only_cited_claims():
    claims = pd.DataFrame([
        {"claim_id": "c1", "source_post_ids": ["73"], "cited": True},
        {"claim_id": "c2", "source_post_ids": ["99"], "cited": False},
    ])
    evidence = pd.DataFrame([{"evidence_id": "c1", "kind": "community_claim"}])
    out = s4_mechanism.adopt_by_post(claims, evidence).set_index("post_id")
    assert out.loc["73", "n_cited"] == 1
    assert "99" not in out.index or out.loc["99", "n_cited"] == 0


def test_adoption_rate_divides_by_full_body_reach():
    reach = pd.DataFrame([{"post_id": "73", "n_full_body": 4, "n_title_only": 10}])
    adopt = pd.DataFrame([{"post_id": "73", "n_cited": 2}])
    posts = pd.DataFrame([{"post_id": "73", "is_best": 1}])
    corpus = pd.DataFrame([{"post_id": "73", "doc_type": "post", "cluster": 3}])
    out = s4_mechanism.adoption_rate(reach, adopt, posts, corpus)
    assert out["adoption_rate"].iloc[0] == 0.5
    assert out["cluster"].iloc[0] == 3


def test_unlike_posts_can_still_be_cited():
    """반대한 글도 contradict 근거로 인용될 수 있다. 이를 잡아내야 한다."""
    claims = pd.DataFrame([{"claim_id": "c1", "source_post_ids": ["73"],
                            "cited": True, "reader_agent_id": "A002"}])
    out = s4_mechanism.reaction_vs_citation(_exposure(), claims)
    unlike_row = out[(out["reaction"] == "unlike")]
    assert unlike_row["n_cited"].iloc[0] == 1
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s4_mechanism.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 's4_mechanism'`

- [ ] **Step 3: 구현**

```python
"""S4: ON arm 내부 메커니즘 — 어떤 글이 실제로 belief에 채택되었나.

도달(reach) = 본문까지 읽은 독자 수. title_only는 규정상 근거로 쓸 수 없으므로
분모에 넣지 않는다 (EXPERIMENT_DESIGN.md §8.4, ANALYSIS_FIELD_GUIDE.md §2-E).

⚠️ selected(직접 선택 읽기)는 처치 이후의 선택이므로 인과적 조절변수로 쓰지
않는다. mechanism 기술통계로만 쓴다 (belief_deviation_rubric.md 분석원칙 5).
⚠️ 게시글 수나 like 수만으로 커뮤니티가 거래를 바꿨다고 단정하지 않는다.
"""

import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api(offline=True)

import paths


def reach_by_post(exposure: pd.DataFrame) -> pd.DataFrame:
    counts = (exposure.groupby(["post_id", "exposure_level"]).size()
              .unstack(fill_value=0)
              .reindex(columns=["full_body", "title_only"], fill_value=0))
    return counts.rename(columns={"full_body": "n_full_body",
                                  "title_only": "n_title_only"}).reset_index()


def adopt_by_post(claims: pd.DataFrame, evidence: pd.DataFrame) -> pd.DataFrame:
    cited_ids = set(evidence.loc[evidence["kind"] == "community_claim",
                                 "evidence_id"])
    cited = claims[claims["claim_id"].isin(cited_ids) | claims.get(
        "cited", pd.Series(False, index=claims.index))]
    rows = []
    for _, claim in cited.iterrows():
        for post_id in claim["source_post_ids"]:
            rows.append({"post_id": post_id})
    if not rows:
        return pd.DataFrame(columns=["post_id", "n_cited"])
    return (pd.DataFrame(rows).groupby("post_id").size()
            .reset_index(name="n_cited"))


def adoption_rate(reach: pd.DataFrame, adopt: pd.DataFrame,
                  posts: pd.DataFrame, corpus: pd.DataFrame) -> pd.DataFrame:
    cluster_map = corpus[corpus["doc_type"] == "post"][["post_id", "cluster"]]
    out = (reach.astype({"post_id": str})
           .merge(adopt.astype({"post_id": str}), on="post_id", how="left")
           .merge(posts.astype({"post_id": str})[["post_id", "is_best"]],
                  on="post_id", how="left")
           .merge(cluster_map.astype({"post_id": str}), on="post_id", how="left"))
    out["n_cited"] = out["n_cited"].fillna(0)
    out["adoption_rate"] = out["n_cited"] / out["n_full_body"].replace(0, pd.NA)
    return out


def best_self_exclusion_contrast(exposure: pd.DataFrame) -> pd.DataFrame:
    """Best 작성자는 자기 글을 못 받고 6위로 backfill하지도 않는다.
    같은 날 같은 자격인데 Best 수신량이 4 vs 5로 갈리는 준외생 변이다.
    표본이 작으므로 검정력 한계를 함께 보고한다."""
    best = exposure[(exposure["exposure_level"] == "full_body")
                    & (exposure["is_best"].astype(str).str.lower() == "true")]
    return (best.groupby(["turn", "agent_id"]).size()
            .reset_index(name="n_best_received"))


def reaction_vs_citation(exposure: pd.DataFrame,
                         claims: pd.DataFrame) -> pd.DataFrame:
    """반대(unlike)한 글도 인용되는가 — 무비판적 동조가 아님을 확인하는 축."""
    pairs = []
    for _, claim in claims[claims.get("cited", True) == True].iterrows():  # noqa: E712
        for post_id in claim["source_post_ids"]:
            pairs.append({"agent_id": claim.get("reader_agent_id"),
                          "post_id": post_id})
    cited_pairs = pd.DataFrame(pairs)
    full = exposure[exposure["exposure_level"] == "full_body"].copy()
    full["post_id"] = full["post_id"].astype(str)
    if len(cited_pairs):
        cited_pairs["post_id"] = cited_pairs["post_id"].astype(str)
        cited_pairs["cited"] = True
        full = full.merge(cited_pairs, on=["agent_id", "post_id"], how="left")
    else:
        full["cited"] = False
    full["cited"] = full["cited"].fillna(False)
    return (full.groupby("reaction")["cited"].agg(["size", "sum"])
            .rename(columns={"size": "n_exposure", "sum": "n_cited"})
            .assign(citation_rate=lambda d: d["n_cited"] / d["n_exposure"])
            .reset_index())


def main() -> None:
    exposure = pd.read_parquet(paths.PANELS / "exposure_panel.parquet")
    exposure = exposure[exposure["arm"] == "RN_COMM_ON"]
    claims = pd.read_parquet(paths.PANELS / "claim_panel.parquet")
    evidence = pd.read_parquet(paths.PANELS / "evidence_with_cluster.parquet")
    posts = pd.read_parquet(paths.PANELS / "post_panel.parquet")
    posts = posts[posts["arm"] == "RN_COMM_ON"]
    corpus = pd.read_parquet(paths.PANELS / "corpus_clusters.parquet")

    reach = reach_by_post(exposure)
    adopt = adopt_by_post(claims, evidence)
    rates = adoption_rate(reach, adopt, posts, corpus)
    rates.to_csv(paths.OUT / "s4_mechanism.csv", index=False)

    print("=== Best vs 비Best ===")
    print(rates.groupby("is_best")[["n_full_body", "n_cited", "adoption_rate"]]
          .agg(["mean", "sum"]).to_string())

    print("\n=== 클러스터별 채택률 ===")
    print(rates.groupby("cluster")[["n_full_body", "n_cited", "adoption_rate"]]
          .agg({"n_full_body": "sum", "n_cited": "sum",
                "adoption_rate": "mean"}).to_string())

    print("\n=== 반응별 인용률 (unlike도 인용되는가) ===")
    print(reaction_vs_citation(exposure, claims).to_string(index=False))

    print("\n=== Best 수신량 준외생 변이 ===")
    contrast = best_self_exclusion_contrast(exposure)
    print(contrast["n_best_received"].value_counts().to_string())
    print("※ 표본이 작다. 검정력 한계를 REPORT에 명시할 것")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s4_mechanism.py -v`
Expected: 4 passed

- [ ] **Step 5: 실행**

Run: `analysis/community_belief_v10/.venv/bin/python analysis/community_belief_v10/s4_mechanism.py`
Expected: Best vs 비Best, 클러스터별 채택률, 반응별 인용률, Best 수신량 분포 출력

- [ ] **Step 6: 커밋 (사용자 승인 시에만 실행)**

```bash
git add analysis/community_belief_v10/s4_mechanism.py analysis/community_belief_v10/tests/test_s4_mechanism.py
git commit -m "analysis: S4 메커니즘 (도달 대비 채택률, Best 대조, 반응별 인용률)"
```

---

## Task 15: S5 강건성 + REPORT 생성

**Files:**
- Create: `analysis/community_belief_v10/s5_robustness.py`
- Create: `analysis/community_belief_v10/report.py`
- Test: `analysis/community_belief_v10/tests/test_s5_robustness.py`

**Interfaces:**
- Produces: `s5_robustness.alternative_model_check(corpus, k) -> pd.DataFrame` — 대체 로컬 모델로 재군집 후 ARI
- Produces: `s5_robustness.k_sensitivity(vectors, corpus, k_values) -> pd.DataFrame`
- Produces: `s5_robustness.burnin_sensitivity(paired) -> pd.DataFrame`
- Produces: `s5_robustness.shortage_sensitivity(paired, news) -> pd.DataFrame` — shortage event 제외 시 결과 변화
- Produces: `report.build() -> str` — REPORT.md 본문

- [ ] **Step 1: 실패하는 테스트 작성**

`analysis/community_belief_v10/tests/test_s5_robustness.py`:

```python
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import s5_robustness


def test_burnin_sensitivity_reports_both_windows():
    paired = pd.DataFrame({
        "agent_id": ["A001"] * 10,
        "turn": list(range(1, 11)),
        "layer": ["STB"] * 10,
        "dim": ["dim_4"] * 10,
        "paired_diff": [1.0] * 6 + [0.0] * 4,
    })
    out = s5_robustness.burnin_sensitivity(paired)
    included = out[out["window"] == "전체 45일"]["mean"].iloc[0]
    excluded = out[out["window"] == "burn-in 제외 42일"]["mean"].iloc[0]
    assert included == 0.6
    assert excluded == 0.0


def test_shortage_sensitivity_drops_shortage_events():
    paired = pd.DataFrame({
        "agent_id": ["A001", "A001"],
        "turn": [7, 9],
        "layer": ["STB", "STB"],
        "dim": ["dim_4", "dim_4"],
        "paired_diff": [1.0, 0.0],
    })
    # turn 9의 event에 뉴스가 9개뿐 → shortage
    news = pd.DataFrame({"event_id": ["e7"] * 10 + ["e9"] * 9,
                         "turn": [7] * 10 + [9] * 9})
    out = s5_robustness.shortage_sensitivity(paired, news)
    complete_only = out[out["window"] == "complete-news-only"]["mean"].iloc[0]
    assert complete_only == 1.0


def test_k_sensitivity_returns_row_per_k():
    rng = np.random.default_rng(0)
    vectors = rng.normal(size=(30, 6))
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    corpus = pd.DataFrame({"doc_type": ["news"] * 15 + ["post"] * 15})
    out = s5_robustness.k_sensitivity(vectors, corpus, [2, 3])
    assert set(out["k"]) == {2, 3}
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/test_s5_robustness.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 's5_robustness'`

- [ ] **Step 3: s5_robustness.py 구현**

```python
"""S5: 강건성. 전부 로컬·무료 계산이다.

대체 임베딩 모델도 로컬 모델을 쓴다. 유료 임베딩 API는 쓰지 않는다.
"""

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import adjusted_rand_score

import guard  # noqa: F401

guard.enforce_no_paid_api(offline=True)

import paths

ALT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def burnin_sensitivity(paired: pd.DataFrame) -> pd.DataFrame:
    rows = [{"window": "전체 45일", "mean": float(paired["paired_diff"].mean()),
             "n": len(paired)}]
    excluded = paired[~paired["turn"].isin(paths.BURNIN_TURNS)]
    rows.append({"window": "burn-in 제외 42일",
                 "mean": float(excluded["paired_diff"].mean()),
                 "n": len(excluded)})
    return pd.DataFrame(rows)


def shortage_sensitivity(paired: pd.DataFrame, news: pd.DataFrame) -> pd.DataFrame:
    """event별 뉴스가 10개 미만인 shortage event를 제외한 결과를 함께 낸다.
    부족분을 backfill하지 않는 것이 설계이므로(EXPERIMENT_DESIGN.md §4)
    shortage event는 정상이지만 민감도는 보고해야 한다."""
    counts = news.groupby("turn").size()
    complete_turns = set(counts[counts >= 10].index)
    rows = [{"window": "전체", "mean": float(paired["paired_diff"].mean()),
             "n": len(paired)}]
    subset = paired[paired["turn"].isin(complete_turns)]
    rows.append({"window": "complete-news-only",
                 "mean": float(subset["paired_diff"].mean()) if len(subset) else np.nan,
                 "n": len(subset)})
    return pd.DataFrame(rows)


def k_sensitivity(vectors: np.ndarray, corpus: pd.DataFrame,
                  k_values: list) -> pd.DataFrame:
    rows = []
    for k in k_values:
        labels = KMeans(n_clusters=k, random_state=0, n_init=10).fit_predict(vectors)
        frame = corpus.copy()
        frame["cluster"] = labels
        counts = frame.groupby(["cluster", "doc_type"]).size().unstack(fill_value=0)
        novel = int((counts.get("news", pd.Series(0, index=counts.index)) == 0).sum())
        rows.append({"k": k, "n_novel_clusters": novel,
                     "largest_cluster_share": float(
                         pd.Series(labels).value_counts(normalize=True).max())})
    return pd.DataFrame(rows)


def metric_sensitivity(vectors: np.ndarray, k: int) -> pd.DataFrame:
    """cosine과 centered dot product의 군집 결과를 비교한다.

    벡터가 L2 정규화되어 있어 cosine == dot이다. 평균 벡터를 빼면(centering)
    "모든 한국어 금융 텍스트가 공유하는 방향"이 제거되므로 결과가 달라질 수
    있다. 결론이 이 선택에 좌우되는지 확인하는 것이 목적이다.
    """
    base = KMeans(n_clusters=k, random_state=0, n_init=10).fit_predict(vectors)
    centered = vectors - vectors.mean(axis=0, keepdims=True)
    centered /= np.linalg.norm(centered, axis=1, keepdims=True)
    other = KMeans(n_clusters=k, random_state=0, n_init=10).fit_predict(centered)
    return pd.DataFrame([{"comparison": "cosine vs centered-dot", "k": k,
                          "ari": float(adjusted_rand_score(base, other))}])


def alternative_model_check(corpus: pd.DataFrame, k: int) -> pd.DataFrame:
    """대체 로컬 모델로 다시 군집해 원래 군집과의 ARI를 낸다."""
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(ALT_MODEL, device="cpu")
    vectors = model.encode(["query: " + t for t in corpus["text"].tolist()],
                           batch_size=64, convert_to_numpy=True,
                           normalize_embeddings=True, show_progress_bar=True)
    labels = KMeans(n_clusters=k, random_state=0, n_init=10).fit_predict(vectors)
    ari = adjusted_rand_score(corpus["cluster"].to_numpy(), labels)
    return pd.DataFrame([{"alt_model": ALT_MODEL, "k": k, "ari_vs_primary": float(ari)}])


def main() -> None:
    paired = pd.read_parquet(paths.PANELS / "paired_movement.parquet")
    paired = paired[paired["paired_diff"].notna()]
    primary = paired[(paired["layer"] == "STB") & (paired["dim"] == "dim_4")]
    news = pd.read_parquet(paths.PANELS / "news_panel.parquet")
    corpus = pd.read_parquet(paths.PANELS / "corpus_clusters.parquet")

    print("=== burn-in 민감도 (주지표 STB dim_4) ===")
    print(burnin_sensitivity(primary).to_string(index=False))

    # news_panel의 event_id에서 turn을 복원해 shortage 판정에 쓴다
    news = news.copy()
    news["turn"] = news.groupby("event_id", sort=True).ngroup() + 1
    print("\n=== shortage 민감도 ===")
    print(shortage_sensitivity(primary, news).to_string(index=False))

    k = int(corpus["cluster"].nunique())
    import embed
    vectors = embed.encode(corpus["text"].tolist(), "s1_corpus")

    print("\n=== k 민감도 ===")
    print(k_sensitivity(vectors, corpus, [max(2, k - 4), k, k + 4]).to_string(index=False))

    print("\n=== metric 민감도 (cosine vs centered dot) ===")
    print(metric_sensitivity(vectors, k).to_string(index=False))

    print("\n=== 대체 로컬 모델 일치도 ===")
    print(alternative_model_check(corpus, k).to_string(index=False))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: report.py 구현**

```python
"""REPORT.md 생성. 결과 표와 함께 caveat을 항상 같이 싣는다.

인공물 목록을 빼고 결과만 싣지 않는다 — ANALYSIS_FIELD_GUIDE.md §2의
항목들은 해석을 바꾸는 내용이므로 보고서에 붙어 있어야 한다.
"""

from pathlib import Path

import pandas as pd

import guard  # noqa: F401

guard.enforce_no_paid_api(offline=True)

import paths

CAVEATS = """
## 해석 시 반드시 함께 읽을 것

| 인공물 | 영향 |
| --- | --- |
| 양면 근거의 한쪽 편입 | dim_4에 집중. 양면성 자체는 최종 evidence에 남지 않는다 |
| hold 비활성 | LTB dim_6 outcome은 추론 품질이 아니라 (강제 방향 × 시장 방향)이다 |
| 패러프레이즈 강제 제거 | Δ≈0은 측정 노이즈가 아니라 진짜 무변화다 |
| title_only ≠ full_body | 두 노출 수준을 합산하지 않았다 |
| day1 구조 | 전날 Best가 없어 community_thinking이 실행되지 않는다 |
| 게시율 | 100%면 "게시 여부 판단" 설계 요소의 변별력이 없다 |
| shortage event 59개 | complete-news-only 민감도를 함께 보라 |
| 경로 발산 | 후반 turn의 ON/OFF 차이는 처치효과와 누적 발산의 혼합이다 |
| selected 읽기 | 처치 이후 선택이므로 인과 조절변수로 쓰지 않았다 |

이 분석은 유료 API를 전혀 호출하지 않았다. 모든 임베딩은 로컬 CPU에서
`intfloat/multilingual-e5-small`로 계산했다.
"""


def _table(path: Path, title: str) -> str:
    if not path.exists():
        return f"### {title}\n\n(미실시)\n"
    df = pd.read_csv(path)
    return f"### {title}\n\n{df.to_markdown(index=False)}\n"


def build() -> str:
    parts = ["# v10 커뮤니티–belief 분석 결과\n"]
    parts.append(_table(paths.OUT / "s1_k_selection.csv", "S1 k 선택"))
    parts.append(_table(paths.OUT / "s2_community_share.csv",
                        "S2 차원별 커뮤니티 인용 비중"))
    parts.append(_table(paths.OUT / "s3_contrast.csv", "S3 ON/OFF 차원별 대조"))
    parts.append(_table(paths.OUT / "s3_turn_blocks.csv", "S3 turn 구간별"))
    parts.append(_table(paths.OUT / "s3_behavior.csv", "S3 실제 개인 순매수 비교"))
    parts.append(CAVEATS)
    return "\n".join(parts)


if __name__ == "__main__":
    (paths.OUT / "REPORT.md").write_text(build(), encoding="utf-8")
    print(f"REPORT.md 작성: {paths.OUT / 'REPORT.md'}")
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `analysis/community_belief_v10/.venv/bin/pytest analysis/community_belief_v10/tests/ -v`
Expected: 전체 테스트 통과 (약 40건)

- [ ] **Step 6: 실행**

```bash
analysis/community_belief_v10/.venv/bin/python analysis/community_belief_v10/s5_robustness.py
analysis/community_belief_v10/.venv/bin/python analysis/community_belief_v10/report.py
```

- [ ] **Step 7: 최종 무과금 확인**

```bash
grep -rnE "openai|openrouter|api_key|API_KEY|requests\.post|httpx" \
     analysis/community_belief_v10/*.py | grep -v guard.py
```

Expected: 출력 없음 (guard.py의 키 제거 목록만 예외)

```bash
analysis/community_belief_v10/.venv/bin/pip list | grep -iE "openai|anthropic"
```

Expected: 출력 없음

- [ ] **Step 8: 커밋 (사용자 승인 시에만 실행)**

```bash
git add analysis/community_belief_v10/s5_robustness.py analysis/community_belief_v10/report.py \
        analysis/community_belief_v10/tests/test_s5_robustness.py
git commit -m "analysis: S5 강건성 + REPORT 생성 + 무과금 최종 확인"
```

---

## 실행 순서 요약

```
Task 1  환경 + guard          (10~20분, 여기서만 네트워크 사용)
Task 2  DB 접근 + fixture     (10분)
Task 3  belief_panel          (10분)
Task 4  evidence_panel        (15분) ← kind 판정 함정 2개
Task 5  커뮤니티 패널          (15분) ← claim_id 규칙 실증 검증 필수
Task 6  news/action panel     (10분)
Task 7  S0 게이트             (5분)  ← 여기 통과 못 하면 진행 금지
─────────────────────────────── 오늘 여기까지가 현실적 목표
Task 8  임베딩 모듈           (10분)
Task 9  S1 클러스터링         (10분 + 사용자 라벨링)
Task 10 S2 1층 인용           (5분)  ← dim_4 가설의 첫 답
Task 11 S2 2층 이동량         (20~40분)
Task 12 S3 arm 대조           (20분)
Task 13 S3 외부 검증          (사용자 데이터 필요)
Task 14 S4 메커니즘           (10분)
Task 15 S5 강건성 + REPORT    (30~60분)
```

## 사용자에게 받아야 할 것

1. **실제 삼성전자 개인 순매수 일별 CSV** — Task 13. 열: `date`(YYYY-MM-DD), `net_value`(매수 우위 시 양수). 없으면 Task 13 생략
2. **클러스터 k 확정** — Task 9 Step 6에서 `s1_k_selection.csv`를 보고 결정
3. **클러스터 라벨** — Task 9가 만든 `cluster_labels.csv`의 `label` 열
4. **커밋 승인** — 각 Task 마지막 단계
