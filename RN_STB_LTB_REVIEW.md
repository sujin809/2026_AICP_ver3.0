# RN A/B 실험 — STB/LTB 메모리 로직 검토 문서

> 목적: RealNews Community A/B 실험의 **Short-Term Belief(STB) / Long-Term Belief(LTB)** 구현을
> 코드에서 직접 검증하여, 실행 순서·입력 경계·belief 표현 방식을 정확히 기록한다.
> 이전 `RN_EXECUTION_FLOW.md`는 부정확한 서술(§7)이 있어 `*.deprecated.md`로 폐기했다.
>
> **작성**: 2026-07-29 · **검증 방식**: 코드 직접 대조 (경로·라인 명시)
> **권위 소스**: `twinmarket_kr/rn_ab/*.py` (구현) + `REALNEWS_COMMUNITY_AB_100AGENT_FUSE_MEMORY_DESIGN.md` (설계 계약)

---

## 0. 한눈에 보기

| 항목 | 값 | 검증 위치 |
|------|-----|----------|
| 조건(arm) | `RN_COMM_OFF`, `RN_COMM_ON` (2개) | `spec.RN_CONDITIONS` |
| 에이전트 | 100명 | `persona_depth_manifest.json` (100 rows) |
| depth 분포 | **D0=30, D1=55, D2=15** | manifest 실측 |
| 메모리 구조 | **STB + LTB 2계층** (FUSE 방식) | `runner.py`, `stage_adapter.py` |
| belief 차원 | dim_1~dim_6 (6개, **텍스트**) | `belief_contract.py` |
| belief 길이 제한 | dim_1=150자, dim_2~6=100자 | `belief_contract.py` |
| 모델 | `qwen/qwen3.5-flash-02-23` | `rn_model_pin.py` |
| reasoning | OFF (`effort:none` + `exclude`) | `call_policy.py` |
| 수수료 | 0원 강제 (`fee_amount=0`) | `db/schema.py`, 설계 §0.0 |
| 거래 성과 horizon | next_turn / H1 / H5 | 설계 §0.0 |

> ⚠️ **핵심 정정**: belief는 `-100 ~ +100` 같은 **숫자 강도 척도가 아니다.**
> dim_1~dim_6은 각각 **한국어 텍스트**이며, 위 숫자는 문자 수 상한(150/100)이다.

---

## 1. STB / LTB 개념

### STB (Short-Term Belief) — 이번 턴 단기 신념
- **현재 AM/PM에 새로 허용된 뉴스 + 실제 노출된 community만** 해석해 만든 `dim_1~dim_6`.
- prior를 **carry하지 않는다**: 이전 STB·이전 LTB·시장/포트폴리오 상태·과거 거래 성찰을 넣지 않는다.
- "짧은 상호작용의 기억" = 매 턴 새로 작성, 휘발적.
- 저장 테이블: `short_term_belief_history`

### LTB (Long-Term Belief) — 누적 장기 신념
- `LTB_(t-1) + STB_t + 이번 턴 체결 사실(fill_t) + 새로 관찰된 과거 가격 성찰`을
  **재귀적으로 다시 해석**해 거래 **뒤에** 작성하는 누적 `dim_1~dim_6`.
- FUSE의 `previous LTM + current STM → new LTM` 패턴에 대응.
- 저장 테이블: `paper_ltb_states` (`parent_ltb_id`로 계층 추적)
- **가시성 제약**: `visible_from_turn > turn` → `LTB_t`는 **다음 decision event부터** 보인다.

---

## 2. 실행 순서 (코드 검증) ✅

`runner.py:369` 의 확정된 barrier 순서:

```python
for stage in ("stb", "analysis", "decision", "fill", "post_fill_ltb"):
    await barrier(stage, phase_attempt_id, attempt_number)
```

한 event(AM 또는 PM turn)의 전체 흐름 (`runner.py:_event_phase`):

```
[event 시작]
 0. mature_outcomes_for_event()      # due한 H1/H5 가격 성과 성숙
 1. community.prepare_event()        # (ON만) 전날 PM 보드 → 이번 STB용 claim 준비
 ────────── 아래 5단계가 순차 barrier ──────────
 2. STB      run_stb()               # 뉴스 + community만 → STB_t 생성
 3. ANALYSIS run_analysis()          # LTB_(t-1) + STB_t → 시장분석
 4. DECISION run_decision()          # LTB_(t-1) + STB_t + analysis → BUY/SELL+수량
 5. FILL     run_fill()              # deterministic 체결 → fill_t (모델호출 없음)
 6. LTB      run_post_fill_ltb()     # LTB_(t-1) + STB_t + fill_t → LTB_t 생성
 ──────────────────────────────────────────────
 7. community.after_event()          # (ON만) PM 보드 생성 → 다음 AM에 노출
[100명 전원 검증 후 단일 트랜잭션 commit]
```

부트스트랩(`runner.py:_bootstrap_phase`): `LTB_0`은 **결정론적 코드 생성** —
`deterministic_initial_ltb(persona)`, **모델 호출 없음**, persona만 사용 → 재현성 100%.

---

## 3. 사용자 지적의 정확성 — 거래→체결→LTB 순서 ✅

> 사용자 지적: "거래 결정을 하고 fill_t를 만든 뒤, 이전 턴꺼를 결합해서 LTB를 만드는 게 정확한 셋팅"

**코드가 정확히 그렇게 구현되어 있다.**

### (A) 거래 결정 — `run_decision()` (`stage_adapter.py:423`)
입력에 **이전 LTB + 현재 STB를 분리된 두 블록**으로 결합:
```python
payload = {
    "input_lineage": {
        "previous_ltb_id": str(parent["ltb_id"]),   # 이전 턴 LTB
        "current_stb_id":  str(stb["stb_id"]),       # 현재 턴 STB
    },
    "previous_ltb": dict(packet["previous_ltb"]),    # ← 결합
    "current_stb":  dict(packet["current_stb"]),     # ← 결합
    "order_history": [],   # 과거 주문 직접입력 없음 (이미 LTB에 통합됨)
    "analysis": {...},
}
```

### (B) 체결 — `run_fill()` (`stage_adapter.py:493`)
결정을 원장에서 읽어 **deterministic 체결** → `fill_t`(side/qty/price/pre·post portfolio). 모델호출 없음.

### (C) LTB 갱신 — `run_post_fill_ltb()` (`stage_adapter.py:526`)
`fill_t`를 **포함하여** 재귀 갱신:
```python
packet = build_post_fill_ltb_packet(
    parent_ltb_id=str(parent["ltb_id"]),  # 이전 LTB
    stb_id=str(stb["stb_id"]),            # 현재 STB
    fill_id=fill_id,                      # ← 이번 턴 체결 사실 포함
)
# packet 내용: previous_ltb + current_stb + transaction_episode(fill) +
#              eligible_price_outcomes_dim_6_only
```

### 요약 (턴 t)
```
LTB_(t-1), STB_t ─→ Decision ─→ Fill(fill_t) ─→ LTB_t = f(LTB_(t-1), STB_t, fill_t)
                                                          │
                                                          └─ 다음 턴의 LTB_(t-1)이 됨
```

---

## 4. 입력 경계 (누출 방지) — 채널별 규칙

설계 §0.1 표 + 코드(`stages.py`, `stage_adapter.py`)에서 강제되는 규칙:

| 채널 | STB 진입 시점 | LTB 통합 시점 | 영향 차원 |
|------|--------------|--------------|----------|
| 이번 턴 체결 `fill_t` | **STB에 절대 안 들어감** | 같은 턴 post-fill `LTB_t` | **LTB `dim_6`만** |
| 성숙 가격 markout | STB 안 들어감 | 도래 event의 LTB | **LTB `dim_6`만** |
| 실제 뉴스 | 현재 event STB | 같은 event 거래 뒤 LTB | STB `dim_1,2,3,5` 중심 |
| Community | 전날 PM분 → 다음 AM STB | 같은 event 거래 뒤 LTB | STB `dim_4,5` 중심 |

강제 장치:
- `build_decision_packet()` 는 `previous_ltb`/`current_stb`에서 `belief_summary`,`view_change` **금지**(`_reject_forbidden`, `stages.py:268`).
- `fill_t`는 같은 턴의 STB·analysis·decision에 **역유입 불가**, post-fill LTB에만 정확히 1회.
- `belief_summary`는 **사람용 로그 전용**, 어떤 agent-visible 입력에도 재투입 안 함.

---

## 5. dim_1 ~ dim_6 의미 (`initial_state.py`)

| 차원 | 의미 | 비고 |
|------|------|------|
| dim_1 | 방향성 기준 (삼성전자 1개월 방향) | 길이 150자 |
| dim_2 | 가치 판단 (실적·밸류) | 100자 |
| dim_3 | 거시·업황 판단 | 100자 |
| dim_4 | 시장심리 평가 | 100자 · community 주 영향 |
| dim_5 | 사건 해석 | 100자 |
| dim_6 | 위험·규율 (거래 성과 성찰) | 100자 · **fill/가격 성과 전용** |

> 이름·의미·전망 horizon은 기존 0720 Samsung 계약을 유지하며 새 taxonomy를 만들지 않는다(설계 §0).

---

## 6. 데이터 무결성 / 재현성

- 모든 STB/LTB/analysis/decision/fill 행에 `scientific_sha256` + `input_sha256`.
- `UNIQUE(run_id, condition_id, agent_id, event_id)` — event당 1회.
- `paper_ltb_states.parent_ltb_id`, `current_stb_id` FK로 lineage 추적.
- `ltb_dimension_transitions` — LTB 전이(부모→자식) + 통합 근거 기록.
- 커밋 단위: **100명 × 양 arm 전원 검증 후 단일 트랜잭션** (부분 실패 시 phase 스냅샷 복구).

---

## 7. 폐기한 옛 문서(`RN_EXECUTION_FLOW.md`)의 오류

| 옛 서술 | 실제 (정정) |
|---------|-----------|
| "belief = 숫자 -100 ~ +100 (강한 약세~강한 강세)" | ❌ 그런 숫자 척도 없음. dim_1~6은 **텍스트**, 숫자는 문자수 상한(150/100) |
| STB만 기재, **LTB 누락** | STB/LTB **2계층**이 핵심. LTB 재귀 갱신이 빠지면 설계 왜곡 |
| "STB = 최근 10거래일 기억" | 설계상 LTB는 보존기간 저장소가 아니라 **매 턴 재귀 갱신**. 5/H1/H5는 **성과 평가 horizon**이지 메모리 보존창이 아님(설계 §0.1, §5.5) |
| 뉴스 버킷 쿼터 "삼성5·반도체3·거시2" | ⚠️ 권위 문서에 **없음**. event당 **목표 10개 provenance-safe 실기사**만 규정, 카테고리 고정쿼터 미검증 |

---

## 8. 미해결/실행 전 확인 필요 (설계 §0.2 P0 게이트)

이 메모리 로직 자체는 구현·검증되었으나, **유료 본 실행은 여전히 NO-GO** 요인이 있음:
- ✅ ~~persona `news_depth` ↔ prompt 60/100 불일치~~ → **수리 완료 (60→0, 100/100 일치)**.
  DB depth를 정답으로 고정하고 prompt만 재생성 (`persona_repair_manifest.json`:
  `post_repair_prompt_depth_mismatch_count=0`, `depth_changed_agent_count=0`).
- 실뉴스 bundle **900 slot(90 event×10) 재선정·봉인** 미완 (현재 899, 1 event가 9개).
- reasoning-off를 **모든 물리적 재시도까지** live canary로 확인해야 함.
- clean base DB 봉인 단계 필요.

> 즉, **STB/LTB 4-stage 코어·스키마·로컬 적대적 테스트는 통과**했고,
> 남은 것은 입력 봉인(persona/뉴스)·live canary·승인 artifact다.

---

## 부록 A. 검증에 사용한 코드 경로

| 관심사 | 파일:심볼 |
|--------|----------|
| 실행 순서 | `twinmarket_kr/rn_ab/runner.py:369` (`_event_phase`) |
| STB 생성 | `stage_adapter.py:319` (`run_stb`) |
| 분석 | `stage_adapter.py` (`run_analysis`) |
| 결정 | `stage_adapter.py:423` (`run_decision`) |
| 체결 | `stage_adapter.py:493` (`run_fill`) |
| LTB 갱신 | `stage_adapter.py:526` (`run_post_fill_ltb`) |
| decision packet | `stages.py:251` (`build_decision_packet`) |
| LTB packet | `stages.py:339` (`build_post_fill_ltb_packet`) |
| 초기 LTB | `initial_state.py:7` (`deterministic_initial_ltb`) |
| belief 한계 | `belief_contract.py` (`RN_BASELINE_BELIEF_LIMITS`) |
| 모델 pin | `rn_model_pin.py` (`RN_PAPER_MODEL`) |
| reasoning off | `call_policy.py` (`StrictCallPolicy`) |
| 스키마 | `db/schema.py` (`PAPER_SIM_DDLS`) |
| depth 분포 | `preparation/rn_ab_persona_snapshot_v1/persona_depth_manifest.json` |
