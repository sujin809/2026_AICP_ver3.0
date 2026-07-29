# RealNews Community A/B — 전체 파이프라인 (STB/LTB 반영판)

> 삼성전자(`005930`) 개인투자자 에이전트 100명이 **커뮤니티 토론 기능 유무(ON/OFF)** 아래
> 실제 뉴스로 매매하며 생성하는 데이터를 수집·비교하는 A/B 실험의 **데이터 준비 → 봉인 → 실행 → 검증** 전 과정.
>
> **작성**: 2026-07-29 · **검증 방식**: 코드 직접 대조 (경로·라인 명시)
> **메모리 로직**: 상세 검토는 [`RN_STB_LTB_REVIEW.md`](RN_STB_LTB_REVIEW.md) 참조
> **상태**: STB/LTB 코어·스키마·로컬 적대적 테스트 통과 / **유료 본 실행은 NO-GO** (§9)
> **폐기**: 옛 `RN_EXECUTION_FLOW.md`(부정확 서술) → `*.deprecated.md`

---

## 0. 실험 개요

| 항목 | 값 | 근거 |
|------|-----|------|
| 조건(arm) | `RN_COMM_OFF`, `RN_COMM_ON` (2개) | `spec.RN_CONDITIONS` |
| 두 arm의 유일한 차이 | **community 사용 가능성** | 설계 §0 |
| 에이전트 | 100명 (고정) | `persona_depth_manifest.json` |
| 뉴스 depth 분포 | D0=30 / D1=55 / D2=15 | manifest 실측 |
| 기간(현재 manifest 예시) | 45거래일 × AM·PM = **90 decision event** | 설계 §0 |
| 주 분석 | 첫 3거래일 burn-in 제외 **42거래일** | 설계 §0 |
| 메모리 | **STB + LTB 2계층** (FUSE 재귀) | `runner.py` |
| belief | dim_1~dim_6 **텍스트** (150/100자 상한) | `belief_contract.py` |
| 모델 | `qwen/qwen3.5-flash-02-23` | `rn_model_pin.py` |
| reasoning | **OFF** (`effort:none`+exclude, 모든 재시도 포함) | `call_policy.py` |
| 수수료 | **0원 강제** (fee=0) | `db/schema.py`, 설계 §0.0 |

> 45·90·42는 **코드 상수가 아니라 manifest에서 resolver가 재계산**한다. 기간 변경 시 함께 재계산됨.
> **RQ1** 반응 정합성(실제 개인투자자 순매수/순매도 방향) · **RQ2** community 총효과가 주 질문.

---

## 1. 파이프라인 전체 지도

```
[A. 데이터 준비]  뉴스 크롤 → 필터링/요약 → split 5폴더
                 persona 100명 → depth manifest
                 source candidate (뉴스/가격/타깃)
        │
        ▼
[B. 봉인/Preflight]  09: 입력 검증·봉인 (모델호출 없음, NO-GO 게이트)
                    10: persona candidate build/validate
                    11: input candidate build/validate
        │
        ▼
[C. 실행 (script 12)]  telemetry(P0) → run-p1(P1 canary) → run(본실행) → finalize
        │  (각 turn: STB→분석→결정→체결→LTB, community 전/후)
        ▼
[D. 검증/산출]  canonical CSV/JSONL + sidecar trace + RUN_RECORD
                validate-final / 99_validate (진단)
```

---

## 2. A단계 — 데이터 준비

### 2.1 뉴스 split 5폴더 (필터링·요약 완료)

크롤 원문 → 카테고리별 split → **필터링 여부(N=유지)** 판정 + 본문 기반 150~200자 요약.

| 폴더 | 파일 | 총 기사 | 유지(N) | 제외(Y) | 요약 |
|------|------|--------|---------|---------|------|
| `samsung_split` | 47 | 4,689 | 4,560 (97.2%) | 129 | ✅ |
| `semiconductor_split` | 84 | 1,675 | 1,571 (93.8%) | 104 | ✅ 97.9% |
| `macro_economic-policy_split` | 50 | 3,696 | 2,732 (73.9%) | 964 | ✅ 100% |
| `macro_business-index_split` | 36 | 704 | 553 (78.6%) | 151 | ✅ |
| `macro_trade_split` | 16 | 310 | 294 (94.8%) | 16 | ✅ |
| **합계** | **233** | **11,074** | **9,710 (87.7%)** | **1,364** | |

**통일 필터링 기준** — KEEP(N): 거시경제 지표·정부/산업 정책·기업/산업 뉴스·거래/통상·시장동향·투자.
DROP(Y): 부동산 생활정보·개인 생활물가·사건/범죄/미담·문화/스포츠/연예·지역행사/홍보성.

**요약 규칙**: 본문 누가/무엇/왜/어떻게 추출 후 재구성(앞 몇 글자 복사 금지), hallucination 금지, 150자 미만 본문은 스킵.

**턴당 버킷 쿼터 (확정)** — `CATEGORY_TARGETS = {종목:5, 섹터:3, 경제:2}` (`news_agent.py:27`), 목표 합 10.
- 폴더로 버킷 결정: `samsung_split`→종목, `semiconductor_split`→섹터, `macro_*`→경제.
- **부족 허용**: 버킷 후보가 모자라면 `min(target, pool)`로 **있는 만큼만** 제공 → 턴당 노출량이 10 미만일 수 있다.
- **교차 보충 금지**: 부족 버킷을 다른 버킷으로 채우지 않는다(구성비 왜곡 방지). 대신 **실제 노출 분포를 결과와 함께 보고**.
- 필터링 여부 `N`인 기사만 로드, `(제목,날짜)` dedup, 섹터 우선순위 종목>섹터>경제 (`news_agent.py:402,405`).

> ⚠️ 봉인 시엔 이 split이 아니라 **provenance·leakage 검증을 통과한 실기사 pool**에서
> event당 **목표 10개(5/3/2)**를 재선정해 `real_news_bundle_manifest`로 고정한다(설계 §0.0, §6).
> 목표를 못 채우면 **safe·unique 기사만으로 부족한 채 계속**하고 shortage를 exception에 기록.

### 2.2 Persona 100명 + depth

- `persona_depth_manifest.json`: `agent_id → news_depth` **불변 assignment** (D0=30/D1=55/D2=15).
- persona prompt는 그 구조화 행의 **결정론적 projection** (`render_persona_v1`).
- ✅ DB `news_depth` ↔ prompt depth 서술: **수리 완료, 100/100 에이전트별 일치**.
  수리 전 60명이 어긋났으나(합계 분포는 우연히 같고 대각선만 40명 일치),
  DB depth를 정답으로 고정하고 prompt만 재생성 → 불일치 60→0
  (`persona_repair_manifest.json`: `post_repair_prompt_depth_mismatch_count=0`,
  `depth_changed_agent_count=0`).

### 2.3 Source candidate (script 10/11)

- `10 build/validate`: 100-agent source persona candidate.
- `11 build/validate`: 뉴스 인벤토리·가격·타깃 candidate (기본 기간 `2026-02-27 ~ 2026-05-04`).
- 산출은 명시적 **NO-GO candidate** (`execution_authorized=false`) — 승인 단계에서만 봉인 승격.

---

## 3. B단계 — 봉인 / Preflight (`09`, 모델호출 없음)

`09_run_realnews_community_ab.py` 는 **local-only preflight 전용**. 입력을 검증·봉인하고
**어떤 모델 클라이언트/네트워크 코드에도 닿기 전에 종료**한다(실행 모드 없음).

필수 입력(모두 hash-pin):
```
--run-id  --input-root  --output-root  --study-spec  --cohort-registry
--persona-snapshot(dir)  --prompt(dir: STB/analysis/decision/LTB)
--calendar-event-registry  --stage-input-registry  --event-price-registry
--real-news-bundle  --known-injection-registry
--article-version-leakage-review-manifest
```

수행: 뉴스/타깃/가격/persona 봉인 검증 → 두 arm의 격리된 paper namespace 생성 → 종료.
`SealedStageInputRegistry`(`stage_inputs.py`)가 news cutoff·시장 스냅샷을 **파일 해시까지** 검증.

### 3.1 fake-news 배제 — 코드로 fail-closed 강제 (이번 run)

`--known-injection-registry`는 **"주입 목록"이 아니라 알려진 fake의 제외 교차검증용
closure 리스트**다. 깨끗한 실뉴스 번들이 이 리스트와 **하나라도 겹치면 preflight 실패** →
"이번 run에 fake 0건"을 **문서가 아니라 코드가 증명**한다. 비어 있을 필요는 없고,
실뉴스 번들과 **disjoint(겹침 0)**이면 된다.

| 강제 장치 | 위치 | 효과 |
|-----------|------|------|
| 번들 `fake_news_per_event=0` 강제 (bool 우회 차단) | `news.py:290` | 아니면 `NewsBundleError` |
| `fake/synthetic/injection` 필드·마커 거부 | `news.py:26,170` | 합성 기사 차단 |
| clean 번들 ↔ known-injection **disjoint** | `run_bundle.py:661` | 겹치면 실패 |
| `fake_news_mode: "off"` **하드코딩** | `resolver.py:821` | 플래그로 못 켬 |
| RUN_RECORD "fake count is 0" 기록 | `run_record.py:85` | 감사 명시 |

> ⚠️ 반대로 `08_run_six_conditions` 계열 launcher는 `fake_news_mode=on`이 기본이라
> 실뉴스-only에 fail-closed가 아니다(설계 §0.2). **이번 run은 그 진입점을 쓰지 않고
> `09`+`12` 경로만 사용** → fake를 켤 방법이 없다.

---

## 4. 메모리 아키텍처 — STB / LTB (핵심)

### 4.1 두 상태의 정의

- **STB_t (단기)**: **현재 AM/PM 뉴스 + 실제 노출된 community만** 해석한 `dim_1~dim_6`.
  이전 STB/LTB·시장/포트폴리오·과거 거래 성찰을 **carry 안 함**. 매 턴 새로 작성(휘발).
  → `short_term_belief_history`
- **LTB_t (장기)**: `LTB_(t-1) + STB_t + 이번 턴 체결 fill_t + 성숙 가격 성찰`을 **재귀 재해석**해
  거래 **뒤** 작성하는 누적 belief. `visible_from_turn > turn` → **다음 event부터** 사용.
  → `paper_ltb_states` (`parent_ltb_id` 계층)

### 4.2 dim_1~dim_6 의미 & 입력 경계

| 차원 | 의미 | 길이 | 주 입력 |
|------|------|------|--------|
| dim_1 | 방향성(1개월 방향) | 150 | 뉴스 |
| dim_2 | 가치 판단 | 100 | 뉴스 |
| dim_3 | 거시·업황 | 100 | 뉴스 |
| dim_4 | 시장심리 | 100 | **community** |
| dim_5 | 사건 해석 | 100 | 뉴스·community |
| dim_6 | 위험·규율 | 100 | **fill_t·가격 성과 전용** |

> **belief는 숫자 척도가 아니다.** dim_1~6은 텍스트이고 150/100은 문자 수 상한이다.
> `belief_summary`/`view_change`는 **사람용 로그 전용** — agent-visible 입력에 재투입 금지(`stages.py:268`).

---

## 5. C단계 — 실행 (`12`)

### 5.1 서브커맨드 (`operations.py`)

| 단계 | 명령 | 유료 | 설명 |
|------|------|:---:|------|
| P0 | `prepare-telemetry` | ✗ | 첫 event 결정론적 telemetry 계획 |
| — | `status` | ✗ | 로컬 게이트 점검 |
| P0 | `telemetry` | **✓** | 봉인된 첫-event reasoning-off 경계 실호출 |
| P1 | `run-p1` | **✓** | telemetry 통과 후 P1 canary 진행 |
| P1 | `resume-p1` | **✓** | 중단된 P1 복구 |
| 본실행 | `run` | **✓** | 검증된 P1 뒤 전체 run 시작 (`--p1-run-dir` 필수) |
| 본실행 | `resume` | **✓** | 전체 run 재개/복구 |
| 마감 | `finalize` | ✗ | lineage 검증 + 최종 CSV 발행 |
| 마감 | `validate-final` / `check-p1` | ✗ | 재검증 |

유료 명령 공통 게이트: `--authorize-paid-api-calls` + `--confirm-run-id`(RUN_RECORD와 정확히 일치) + `--run-dir`.

### 5.2 한 event(turn)의 stage 흐름 — 코드 확정 순서

`runner.py:369` — `("stb","analysis","decision","fill","post_fill_ltb")` barrier, 앞뒤 community:

```
[event 시작]
 0. mature_outcomes_for_event()   # 도래한 H1/H5 가격 성과 성숙
 1. community.prepare_event()     # (ON만) 전날 PM 보드 → 이번 STB용 claim
 ── 순차 barrier (100명×2arm 각 단계 전원 완료 후 다음) ──
 2. STB       run_stb()           # 뉴스+community → STB_t
 3. ANALYSIS  run_analysis()      # LTB_(t-1)+STB_t → 시장분석
 4. DECISION  run_decision()      # LTB_(t-1)+STB_t+analysis → BUY/SELL+수량
 5. FILL      run_fill()          # deterministic 체결 fill_t (모델호출 X)
 6. LTB       run_post_fill_ltb() # LTB_(t-1)+STB_t+fill_t → LTB_t
 ──────────────────────────────────────────────
 7. community.after_event()       # (ON만) PM 보드 생성 → 다음 AM 노출
[전원 검증 후 단일 트랜잭션 commit]
```

**부트스트랩**: `LTB_0`은 `deterministic_initial_ltb(persona)`로 **코드 생성**(모델호출 없음) → 재현성 100%.

### 5.3 결정 → 체결 → LTB (사용자 확정 셋팅)

```
LTB_(t-1), STB_t ─→ Decision ─→ Fill(fill_t) ─→ LTB_t = f(LTB_(t-1), STB_t, fill_t)
                                                        └─ 다음 턴의 LTB_(t-1)
```
- Decision(`stage_adapter.py:423`): `previous_ltb`+`current_stb` 두 블록, `order_history=[]`.
- Fill(`:493`): 원장 결정으로 deterministic 체결. 매수 현금제약·매도 보유제약, 미체결 폐기.
- LTB(`:526`): `fill_id` 포함, `transaction_episode` + `dim_6` 전용 가격성과. `fill_t`는 STB/analysis/decision에 **역유입 불가**.

### 5.4 커뮤니티 (ON 조건만)

- PM scientific commit 뒤 보드 생성 → **다음 거래일 AM STB**에 처음 진입.
- Best 상위 **K=5** 는 100명 전원에 broadcast 예약(글 0이면 노출 0).
- 추가 self-read 상한: **D1=5, D2=10** (D0=추가 0). depth는 DB `news_depth` 사용.
- OFF 조건은 community 경로 전면 미제공 → 두 arm의 유일 차이.

---

## 6. 거래 성과 관찰 (dim_6 성찰용)

- 각 `fill_t`는 **next_turn → H1(다음 거래일 동일 subturn) → H5(5거래일 뒤)** 순으로 markout 성숙.
- 관찰값은 도래 event 뒤 LTB **dim_6**에만 반영, STB·dim_1~5엔 넣지 않음.
- 마지막 구간 미도래는 `right_censored`. 수수료 0 기준 gross timing markout.
- H1/H5는 **성과 평가 horizon**이지 메모리 보존창이 아니다(설계 §5.5).

---

## 7. D단계 — 산출물 & 검증

### 7.1 canonical 로그 (보존·확장, 설계 §0.0)

`agent_turns`, `submitted_orders`, `exchange_fills`, `portfolio_updates`,
`community_posts`, `community_interactions`, `community_best_posts`, `community_logs`
+ sidecar: memory/outcome/API/community-exposure trace, `community_post_trace.jsonl`.
`RUN_RECORD`(+manifest)가 기존·신규 artifact 경로/hash를 색인.

### 7.2 무결성 (`db/schema.py` v9)

- 모든 belief/analysis/decision/fill 행에 `scientific_sha256` + `input_sha256`.
- `UNIQUE(run_id, condition_id, agent_id, event_id)` — event당 1회.
- 핵심 불변식: agent별 **committed STB 수 = LTB 수 = manifest의 decision-turn 수 U**.
- `community_post_trace`는 `condition_id='RN_COMM_ON'` CHECK로 arm 격리.

### 7.3 검증 명령

`finalize`(lineage+CSV) → `validate-final`(재계산) / `check-p1`(P1 재검증).
`99_validate.py`는 **archival 진단 전용**(승인 게이트 아님).

---

## 8. 비용 (설계 §, 과거 청구 기준)

| 단계 | 규모 | 비용 |
|------|------|------|
| P1 canary (단일 arm) | OFF~$0.75 / ON~$1.00 | ~$2 |
| 2-arm 전체(예시 90 event) | ~9,400 호출 | **~$33–38** (문서 헤더 기준) |

> 호출·비용은 응답 길이에 따라 ±변동. telemetry 단계가 실측 근거를 만든다.

---

## 9. 실행 전 NO-GO 게이트 (설계 §0.2)

메모리 4-stage 코어·스키마·로컬 적대적 테스트는 통과. 남은 봉인/검증:
- [x] ~~persona `news_depth` ↔ prompt 60/100 불일치~~ → **✅ 수리 완료 (60→0, 100/100 일치)**
      `persona_repair_manifest.json`이 증거 (DB depth 불변, prompt만 재생성).
- [ ] 실뉴스 bundle **봉인** — 데이터는 준비됨, 기계적 봉인만 남음:
      - **provenance 데이터는 이미 존재**: 재크롤 `outputs/crawl/*.jsonl`이 study window 기사의
        99%에 published/modified/effective/scraped/body/url을 갖춤. candidate의
        "immutable provenance missing" 판정은 **옛 pkl을 스캔한 stale 결과**.
      - **바인딩 검증됨**: `scripts/13_bind_news_provenance.py`가 study window 2,958개를
        effective_at=max(published,modified) 규칙으로 provenance 바인딩(해시 완비), 미매칭 109개뿐.
      - **slot 부족은 게이트 아님**: 목표 90×10(5/3/2)이나 부족하면 부족한 채 진행이 설계(부족 허용).
      - **남은 기계적 작업**: ① candidate 검증을 재크롤 기준으로 재실행, ② 90 거래일 event 슬롯
        매핑(거래 캘린더), ③ `real_news_bundle_manifest` 생성·승인. **재크롤/데이터 복구 불필요.**
- [ ] reasoning-off를 **모든 물리적 재시도까지** live canary로 확인(returned model/provider, `reasoning_tokens=0`).
- [ ] clean base DB 봉인(현재 `experiment_base_sim.db` 0 byte).
- [ ] `08_run_six_conditions` 계열의 fake-news 기본값·30명 하드코딩 진입점 미사용(실뉴스 2-arm만).

> 위 게이트를 모두 통과하기 전 유료 본 실행 금지.
> (턴당 뉴스 10개 미만은 정상 — 부족 허용 정책. 실제 노출 분포는 결과와 함께 보고.)

---

## 부록 A. 명령 요약

```bash
# B. 봉인 preflight (모델호출 없음)
python scripts/09_run_realnews_community_ab.py \
  --run-id <RUN_ID> --input-root <IN> --output-root <OUT> \
  --study-spec <...> --cohort-registry <...> \
  --persona-snapshot <dir> --prompt <dir> \
  --calendar-event-registry <...> --stage-input-registry <...> \
  --event-price-registry <...> --real-news-bundle <...> \
  --known-injection-registry <...> \
  --article-version-leakage-review-manifest <...>

# C. 실행 (script 12)
python scripts/12_operate_realnews_community_ab.py prepare-telemetry --run-dir <DIR>
python scripts/12_operate_realnews_community_ab.py telemetry \
  --run-dir <DIR> --authorize-paid-api-calls --confirm-run-id <RUN_ID>
python scripts/12_operate_realnews_community_ab.py run-p1 \
  --run-dir <DIR> --authorize-paid-api-calls --confirm-run-id <RUN_ID>
python scripts/12_operate_realnews_community_ab.py run \
  --run-dir <DIR> --p1-run-dir <P1_DIR> \
  --authorize-paid-api-calls --confirm-run-id <RUN_ID>

# D. 마감/검증 (모델호출 없음)
python scripts/12_operate_realnews_community_ab.py finalize --run-dir <DIR> --p1-run-dir <P1_DIR>
python scripts/12_operate_realnews_community_ab.py validate-final --run-dir <DIR>
```

## 부록 B. 검증 코드 경로

| 관심사 | 위치 |
|--------|------|
| turn stage 순서 | `rn_ab/runner.py:369` (`_event_phase`) |
| STB/분석/결정/체결/LTB | `rn_ab/stage_adapter.py:319 / :423 / :493 / :526` |
| decision/LTB packet | `rn_ab/stages.py:251 / :339` |
| 초기 LTB | `rn_ab/initial_state.py:7` |
| belief 한계 | `rn_ab/belief_contract.py` |
| 봉인 입력 registry | `rn_ab/stage_inputs.py` |
| 모델 pin / reasoning off | `rn_model_pin.py` / `rn_ab/call_policy.py` |
| 실행 서브커맨드 | `rn_ab/operations.py:60-113` |
| 스키마 v9 | `db/schema.py` (`PAPER_SIM_DDLS`) |
| depth manifest | `preparation/rn_ab_persona_snapshot_v1/persona_depth_manifest.json` |
| 설계 계약 | `REALNEWS_COMMUNITY_AB_100AGENT_FUSE_MEMORY_DESIGN.md` |
