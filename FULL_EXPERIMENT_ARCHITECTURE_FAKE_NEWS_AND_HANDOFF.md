# Samsung 005930 Agent-Market Experiment Architecture

> 목적: 실제뉴스 baseline부터 향후 bullish/bearish fake-news 확장까지를 같은 연구·실행·분석 계약 안에 두기 위한 논문/인수인계용 기준 문서
>
> 상태: **설계 기준선**. 이 문서는 구현 완료를 뜻하지 않는다. 각 실행은 [실험 전 Go/No-Go 체크리스트](PRE_EXPERIMENT_GO_NO_GO_CHECKLIST.md)의 P0를 통과한 sealed run bundle만 논문용 결과로 사용할 수 있다.
>
> 세부 baseline memory·코드 감사: [REALNEWS_COMMUNITY_AB_100AGENT_FUSE_MEMORY_DESIGN.md](REALNEWS_COMMUNITY_AB_100AGENT_FUSE_MEMORY_DESIGN.md)  
> 팀 실행 요약: [REALNEWS_COMMUNITY_AB_100AGENT_TEAM_BRIEF.md](REALNEWS_COMMUNITY_AB_100AGENT_TEAM_BRIEF.md)

---

## 1. 연구 프로그램의 범위와 단계

이 연구는 삼성전자 `005930`을 대상으로 LLM agent 집단이 실제 뉴스와 community 상호작용에 반응해 보이는 거래 방향을 분석한다. 사람의 실제 주문을 재현하거나 투자 권고를 만드는 시스템이 아니다. 외부 타깃은 삼성전자 개인투자자 `Individuals`의 **일별 최종 순거래대금 방향**이며, AM/PM별 개인 수급 방향 데이터가 없으므로 AM/PM의 단독 비교는 탐색 분석으로만 둔다.

연구는 아래 순서로 진행한다. 후속 단계는 앞 단계의 품질 gate가 통과됐을 때만 시작한다.

| 단계 | 이름 | 실행 조건 | 핵심 산출물 |
|---|---|---|---|
| 0 | 입력·환경 고정 | 코드 구현 전/실행 전 | cohort/calendar/news/base/model/prompt registry와 resolved manifest |
| 1 | 실제뉴스 baseline | 지금 우선 실행 | `RN_COMM_OFF`, `RN_COMM_ON` pair와 방향성 검증 report |
| 2 | baseline 재현·진단 | Phase 1 `PASS` 또는 non-blocking `PASS_WITH_NEWS_SHORTAGE` 뒤 | 재실행 parity, checkpoint-resume parity, subgroup/robustness report |
| 3 | fake-news stimulus 검증 | Phase 1/2에 blocking failure가 없을 때 | fake registry, injection schedule, content/leakage audit, canary |
| 4 | fake-news factorial | Phase 3 `PASS` 또는 non-blocking `PASS_WITH_NEWS_SHORTAGE` 뒤 | bearish/bullish × community comparison, mediation traces |
| 5 | 논문·인수인계 봉인 | 각 phase 완료 시 | final report bundle, data dictionary, source hashes, decision log |

현재 연구의 유일한 유료 본실행 대상은 Phase 1이다. `RN_COMM_OFF`/`RN_COMM_ON`은 fake-news 결과를 주장하기 위한 대체물이 아니며, fake effect는 Phase 4가 끝나기 전에는 보고하거나 암시하지 않는다.

---

## 2. 질문, 분석 단위, 해석 범위

### 2.1 사전등록할 질문

| ID | 질문 | 대상 phase | 주 분석 단위 |
|---|---|---|---|
| RQ1 | 실제뉴스만 있을 때 agent 집단의 일별 거래 방향은 삼성전자 개인 순거래대금 방향과 어느 정도 일치하는가? | Phase 1 | arm × trading date |
| RQ2 | 실제뉴스 환경에서 community availability가 agent 집단의 belief·거래 방향·분산에 어떤 차이를 만드는가? | Phase 1 | paired simulated world/date |
| RQ3 | 동일한 frozen real-news bundle(목표 10개, event별 actual count 고정)에 bullish 또는 bearish fake 1개를 더했을 때 반응이 어떻게 달라지는가? | Phase 4 | injected event/date × simulated world |
| RQ4 | community가 fake-news 효과를 증폭·완화·지연시키는가? | Phase 4 | fake polarity × community mode interaction |
| RQ5 | news/community → STB + previous LTB → decision/fill → same-turn post-fill LTB의 경로가 trace로 재구성되는가? | 모든 phase | event × agent lineage |

### 2.2 해석상 금지할 주장

- agent의 방향 일치는 한국 개인투자자의 심리·행동을 인과적으로 대표한다는 뜻이 아니다.
- `RN_COMM_ON - RN_COMM_OFF`는 **시뮬레이션 내부에서 허용된 community mechanism의 총 효과**다. 실제 온라인 커뮤니티의 외부 효과 크기를 추정하지 않는다.
- fake-news arm의 fake label은 agent에게 보이지 않으므로, 이 실험은 허위정보 노출에 대한 agent 반응을 보는 것이다. 실제 사람에게 허위정보를 배포하거나 사람의 반응을 측정하는 연구가 아니다.
- AM/PM 자체와 실제 개인수급의 시간대별 방향을 직접 비교하거나 맞혔다고 말하지 않는다. primary outcome은 두 subturn의 당일 실현 거래를 합친 일별 값이다.

---

## 3. 조건 체계와 확장 순서

### 3.1 canonical condition ID

새 run, directory, DB, report, figure, manifest에는 아래 ID만 쓴다. 과거 `c00_*`, `c10_*` 및 six-condition launcher 이름은 **archival alias**이며 새 논문 artifact에서 사용하지 않는다.

| phase | condition ID | real-news | fake-news | community |
|---|---|---:|---:|---|
| 1 | `RN_COMM_OFF` | 목표 10; 불가 시 실제 안전 기사 수를 예외 기록 | 0 | 완전 OFF |
| 1 | `RN_COMM_ON` | `RN_COMM_OFF`와 동일한 frozen bundle | 0 | 원 설계 permission map ON |
| 4 | `FN_BEAR_COMM_OFF` | 목표 10; 부족 시 동일 예외 정책 | schedule상 0 또는 1 bearish | OFF |
| 4 | `FN_BEAR_COMM_ON` | OFF와 동일한 frozen bundle | schedule상 0 또는 1 bearish | ON |
| 4 | `FN_BULL_COMM_OFF` | 목표 10; 부족 시 동일 예외 정책 | schedule상 0 또는 1 bullish | OFF |
| 4 | `FN_BULL_COMM_ON` | OFF와 동일한 frozen bundle | schedule상 0 또는 1 bullish | ON |

`fake-news`가 있는 condition도 매 decision event에 fake를 억지로 넣지 않는다. 별도의 frozen `fake_injection_schedule`이 지정한 event에만 **추가 1개**를 넣는다. 비주입 event는 같은 frozen actual real bundle + fake 0이다. 이것이 실제 baseline과 fake 실험의 노출 빈도를 정확히 기술하는 방법이다.

### 3.2 같은 world를 비교하는 원칙

동일 `condition_pair_id`의 OFF/ON arm에는 community mode 외 다른 차이가 없어야 한다. 다음 input은 byte/hash 기준으로 동일하다.

```text
study specification / code revision / frozen cohort / persona prompt map
event calendar / prices / target labels (evaluator only)
real-news slot map / fake injection schedule and payload (해당 pair만)
base portfolio snapshot / seed namespace / model-provider policy
prompt schema / call policy / fee policy / memory policy
```

pair gate는 structural diff를 만들고 `community_mode` 한 field만 허용한다. fake polarity pair도 injection date·anchor·real bundle·모델·seed가 같고 fake payload의 polarity/내용 ID만 계획된 차이여야 한다. 조건 이름이 같다는 사실은 pair equality의 증거가 아니다.

### 3.3 effect 정의

실제 baseline에서는 community contrast를 아래처럼 본다.

```text
Δcommunity(real) = outcome(RN_COMM_ON) − outcome(RN_COMM_OFF)
```

fake phase에서는 polarity 및 community moderation을 분리한다.

```text
Δbear,off = outcome(FN_BEAR_COMM_OFF) − outcome(RN_COMM_OFF)
Δbear,on  = outcome(FN_BEAR_COMM_ON)  − outcome(RN_COMM_ON)
moderationbear = Δbear,on − Δbear,off

Δbull,off = outcome(FN_BULL_COMM_OFF) − outcome(RN_COMM_OFF)
Δbull,on  = outcome(FN_BULL_COMM_ON)  − outcome(RN_COMM_ON)
moderationbull = Δbull,on − Δbull,off
```

여기서 outcome은 사전에 정한 일별 direction accuracy, signed fill-value, buy/sell imbalance, belief-change 또는 이들의 event-level 변형이다. community 노출·게시·reaction은 primary treatment 자체가 아니라 mechanism trace 및 mediator 후보로 보고, post-treatment conditioning으로 total effect를 바꾸지 않는다.

---

## 4. 공통 실험 명세: authored input과 derived output

### 4.1 단일 진실원천

사람이 직접 승인하는 것은 `StudySpec`과 versioned registry다. 코드 곳곳의 default, shell argument, `outputs/logs/current`, 과거 runtime DB는 scientific input이 될 수 없다.

| authored/frozen input | resolver가 만드는 derived output |
|---|---|
| target ticker, study window, session definition | ordered trading dates, decision event IDs, `D`, `U` |
| exact cohort registry | `N`, depth/asset counts, community eligible sets |
| burn-in rule | exact excluded set `B`, evaluation set `E` |
| real-news pool and selection policy | event × slot 1..10 map, article-slot count |
| optional fake registry/schedule | event-specific fake slot map |
| model/provider/reasoning/call policy | immutable runtime request policy and expected logical call keys |
| community/memory/fee policy | event-level stage schedule, expected trace and ledger keys |

현재 승인 예시는 `N=100`, D0/D1/D2=`30/55/15`, 45 trading dates, AM/PM event, 첫 3 trading date burn-in이다. 즉 이 example에서는 `D=45`, `U=90`, `B=3`, `E=42`가 된다. 이 숫자는 기준 manifest의 **검증 결과**이며 Python의 `×2`, `90`, `42`, `70` 상수로 구현하면 안 된다.

### 4.2 RunBundle 구조

모든 paper run은 아래와 같은 isolated root에서 시작하고 끝난다.

```text
run_root/
  RUN_RECORD.json                      # machine-readable run/arm/path/hash index
  RUN_RECORD.md                        # 사람이 보는 artifact index
  RUN_FINALIZATION.json                # 두 arm local-integrity finalization
  resolved_study_manifest.json         # 전체 실행의 frozen resolved contract
  evaluator_contract.json              # evaluator binding; target 값은 별도
  source_hashes.json                   # code/prompt/dependency hash
  inputs/                              # sealed persona/prompt/runtime/generated inputs
  RN_COMM_OFF/
    RUN_RECORD.md
    paper_run.sqlite                   # OFF scientific state
    response_journal.sqlite            # OFF durable logical responses
    openrouter_attempts.jsonl           # OFF physical attempt/acceptance audit
  RN_COMM_ON/
    RUN_RECORD.md
    paper_run.sqlite                   # ON scientific state
    response_journal.sqlite            # ON durable logical responses
    openrouter_attempts.jsonl           # ON physical attempt/acceptance audit
  traces/
    rn_comm_off_final_fill_ledger.csv
    rn_comm_on_final_fill_ledger.csv
    final_fill_export_index.json
    community_post_trace.jsonl
    community_exposure_trace.jsonl
  community_interactions.csv
  community_best_posts.csv
```

위 구조는 현재 RN baseline의 실제 경로다. evaluator의 `daily_flow_comparison.csv`, `paired_condition_summary.json`, `direction_validation.json`, `evaluation_artifact_index.json`은 운영자가 명시한 별도 `--output-dir`에 생성한다. 후속 fake-news study도 단일 `runtime/run.sqlite`로 합치지 않고 condition별 디렉터리를 추가한다.

`resolved_study_manifest.json`의 hash는 checkpoint, arm별 API journal/audit, final ledger, validator, report가 모두 기록한다. resume 시 hash가 하나라도 달라지면 “새로운 run”으로 취급하고 기존 checkpoint에 이어 쓰지 않는다.

### 4.3 허용되는 연구 상수와 금지되는 operational hardcode

다음 값은 현재 연구의 승인된 설정으로 registry에 둘 수 있다: 100명, `30/55/15`, 자산 90/10, Best 최대 5개, 실뉴스 **목표** 10개와 shortage exception policy, 수수료 0원, first-3-trading-day burn-in, H1/H5 정의.

반대로 아래는 manifest resolver만 계산한다: active community agent 수, total event 수, expected STB/LTB/fill row 수, mature/censored reflection 수, report denominator, 날짜별 exposure upper bound. `N×U`, event list, eligible-agent set에서 산출하지 않고 코드에 `70`, `90`, `9000`, `42`를 박아 두면 기간·agent 수·session 구성 변경 시 조용히 다른 실험이 된다.

---

## 5. 실뉴스 data architecture: 목표 10개, shortage 예외, 시간 안전성

### 5.1 baseline input contract

baseline의 모든 decision event는 **10개의 provenance-safe real article을 우선 목표**로 한다.

```text
event_id e
  target real slots: (e, 1) ... (e, 10); actual safe slots may be fewer only with shortage metadata
  fake slots: none
```

“오늘 수집된 목록에 9개만 있다”면 수집해 둔 뉴스 데이터베이스의 candidate pool에서 해당 event의 cutoff, source/provenance, 원문 version, semantic leakage review를 모두 통과하는 **다른 실제 기사**를 먼저 재선정해 slot 10을 채운다. 재선정은 실행 전 한 번 수행하고 ordered ID/hash map을 frozen registry로 봉인한다.

다만 safe pool으로도 10개를 만들 수 없다는 이유만으로 run을 멈추지는 않는다. 그 event에는 가능한 모든 safe·unique 실제 기사만 넣어 실행하고, `news_coverage_status=shortage_accepted`, `target_real_count=10`, `selected_safe_count`, `serialized_count`, `delivered_real_count`, `actual_real_count(=delivered_real_count)`, `missing_real_count(=target_real_count-selected_safe_count)`, candidate pool size, selection/review reason, ordered ID/hash map을 manifest와 `article_delivery_trace`에 남긴다. duplicate나 synthetic article로 빈 slot을 채우지 않으며, unsafe article을 넣어 10개처럼 보이게 하지 않는다. `RN_COMM_OFF`/`RN_COMM_ON`은 shortage event까지 포함해 정확히 같은 frozen bundle을 사용한다.

각 shortage는 `registries/news_shortage_exception_manifest.jsonl`에 한 immutable row로 남긴다.

```text
event_id / exception_id / news_coverage_status=shortage_accepted
target_real_count / selected_safe_count / serialized_count / delivered_real_count
actual_real_count (= delivered_real_count) / missing_real_count (= target_real_count - selected_safe_count)
candidate_pool_digest + count / selection_algorithm + seed / reason_code + review ID
ordered (news_id, payload_hash) list / selected-bundle hash / pair-bundle hash / approval
```

`selected_safe_count` 부족은 non-blocking data-coverage exception이다. 반면 `serialized_count` 또는 `delivered_real_count`가 selected map과 다르거나 payload hash가 달라지는 것은 runtime delivery failure다. 후자는 shortage라는 이름으로 통과시키지 않고 retry 또는 integrity failure로 처리한다. 같은 원칙으로 OFF/ON의 delivered count/hash가 하나라도 다르면 pair comparison을 만들지 않는다.

현재 45일 × AM/PM 예시에서 목표는 90 event × 10 = **900 article slots**다. 기존 daily selection의 2026-03-23 PM 9개 row는 safe pool에서 보충을 먼저 시도한다. 보충이 불가능하면 9개라는 사실과 이유를 봉인한 899-slot exception bundle로 **계속 실행**한다. 예정 calendar에서 날짜나 event를 삭제하지 않는다.

### 5.2 재선정 알고리즘의 보호장치

재선정이 사후적으로 결과에 맞는 기사 picking이 되지 않게 다음을 기록한다.

| 항목 | 요구 |
|---|---|
| candidate universe | raw DB snapshot hash와 query/cutoff policy로 확정 |
| eligibility | target, source, published/observed/update time, raw body version, semantic review status를 모두 명시 |
| selection rule | deterministic seed, ranking/tie-break, category policy version을 기록 |
| duplicate rule | 동일 event 안 같은 `news_id` 또는 payload hash 중복 금지 |
| cross-event reuse | `allowed_if_independently_eligible_and_logged_v1`로 고정: 동일 event 안 중복은 금지하고, 다른 event에는 그 event cutoff를 독립적으로 통과했을 때만 재사용 가능. `reuse_of_event_ids`와 article ID/hash를 manifest/trace에 기록 |
| quota claim | 실제 resolved map이 5/3/2를 만족할 때만 그 quota를 논문에 주장; 기사 수 10을 맞추기 위해 미충족 quota를 숨기지 않음 |
| pair equality | `RN_COMM_OFF`와 `RN_COMM_ON`은 동일 read-only slot map을 참조 |

shortage는 누락을 감추는 면책이 아니다. report는 (a) planned full calendar를 유지한 전체 분석, (b) 하루의 모든 decision event(현재 AM·PM)가 `actual_real_count=10`이고 selected=serialized=delivered-real payload hash가 같은 날만 포함하는 `complete-news-only` sensitivity, (c) shortage event/date 목록과 각 count를 함께 제시한다. resolver는 이 sensitivity date set의 ordered date IDs·mask hash·분모를 미리 만들며, primary 분모는 shortage 때문에 교집합으로 조용히 줄이지 않는다.

runtime prompt는 “10개 뉴스가 제공된다”는 고정 문구를 쓰지 않는다. frozen manifest의 actual article list만 직렬화하고, payload 수와 prompt snapshot hash를 `article_delivery_trace`에 남긴다. shortage quality flag와 candidate/review metadata는 evaluator/report용이며, agent에 hidden study-quality label로 전달하지 않는다.

후보 pool의 기사 본문이 mutable live page에서 나중에 다시 받은 것이라면 “결정시점에 본 버전”임을 증명하지 못한다. URL만 보존하는 것은 충분하지 않다. `published_at`, `observed_at`/`scraped_at`, `last_modified_at` 가능 여부, raw body hash, cutoff-time payload hash, review decision을 저장해야 한다.

### 5.3 agent-visible news와 논문 표현

현재 코드는 실제 기사 **원문 전체**가 아니라 제목과 파생 summary를 agent에 주는 구조다. 원문이 아닌 summary를 쓰면 논문에는 “실제 언론기사에서 파생한 제목·요약 feed”라고 쓴다. 원문 전체로 바꾸는 것은 token budget, prompt injection 방어, prompt version, selection evidence가 동시에 변하는 새 amendment다.

### 5.4 leakage gate

아래는 **기사 수 shortage와 다른 blocking leakage/integrity failure**다. 하나라도 감지되면 event를 삭제해 날짜를 줄이는 대신 run을 실패시킨다.

- AM input에 당일 종가·마감 후 정보·실제 개인수급 target·미래 뉴스가 있음
- PM input에 cutoff 이후 정보가 있음
- raw article의 later version이 historical timestamp로 backdate됨
- target label이 STB/LTB/analysis/decision/community/API journal에 들어감
- stock, news, target calendar가 intersection으로 silently reduced 됨

leakage scan은 keyword filter만으로 끝나지 않는다. candidate를 reject/mask/allow로 분류하고 이유, reviewer, source version, resolution을 `article_version_leakage_review`에 남긴다.

---

## 6. 후속 fake-news architecture

### 6.1 baseline을 보존하는 방식

fake phase는 baseline real bundle을 덮어쓰지 않는다. 각각 별도 RunBundle과 condition namespace를 사용한다.

```text
baseline event:    target 10 real (actual count frozen) + 0 fake
injection event:  same frozen real bundle + 1 fake
non-injection event in fake study: same frozen real bundle + 0 fake
```

fake는 target real article 10개 중 하나를 대체하지 않는다. shortage event에서도 frozen actual real bundle 위에 추가되므로 fake effect를 “real news를 하나 빼서 생긴 효과”와 구분할 수 있다.

### 6.2 fake registry

agent에게 보이지 않는 private registry에는 최소한 아래를 보관한다.

| field | 의미 |
|---|---|
| `synthetic_id`, payload hash, version | immutable stimulus identity |
| polarity | `bullish` 또는 `bearish` |
| factual anchor | 어떤 실제 맥락을 변형했는지, event 전에 공개돼 있던 사실인지 |
| injection event | 노출이 허용된 event ID와 cutoff |
| generation/review record | 생성 방법, human approval, factual/temporal safety 검토 |
| matched pair ID | bullish/bearish 비교에서 공통 anchor·schedule을 묶는 키 |
| visibility metadata | private-only label; agent-visible payload에서 제거할 fields 목록 |

agent-visible payload에는 `is_fake`, `synthetic_id`, polarity, generation prompt, study condition, evaluation label이 절대로 포함되지 않는다. 반대로 final audit에는 모든 fake root와 그 exposure/claim/decision downstream lineage가 남아야 한다.

### 6.3 matched injection schedule

fake efficacy를 비교하려면 bullish/bearish injection은 임의의 서로 다른 날짜에 넣으면 안 된다. `matched_injection_id`별로 같은 real-news bundle, same event, same event order, same cohort, same model/provider/seed를 사용하고 polarity/content만 계획적으로 바꾼다.

scheduled event에서 fake 1개가 실제로 agent에 전달됐는지는 `article_delivery_trace`로 증명한다. community ON에서는 fake가 public post로 자연스럽게 퍼질 수 있지만, “fake가 사회적으로 확산되도록” posting 또는 reading을 강제하지 않는다. 그 경로는 관찰 대상이다.

### 6.4 fake phase의 별도 safety gate

- baseline `RN_*` bundle과 fake registry 간 overlap은 0이다.
- injection event에만 fake count가 1이며, 각 event의 real count/shortage status는 baseline과 동일하게 frozen 기록된다.
- fake가 cutoff 후 미래 실제 결과를 암시하거나 target label을 직접 알려주지 않는다.
- payload metadata가 agent prompt·community post prompt·selection prompt에 새지 않는다.
- bearish/bullish content의 길이, 형식, source style, insertion position이 systematic confound가 되지 않는지 review한다.
- external dissemination 없이 isolated experiment input으로만 저장·실행한다.

---

## 7. agent cohort, persona, community architecture

### 7.1 current frozen cohort

본 baseline의 canonical cohort는 100명이다.

| group | count | permission |
|---|---:|---|
| Depth 0 | 30 | post/selective read/reaction 없음; 다음 AM Best full-body passive exposure |
| Depth 1 | 55 | post/selective read/reaction 가능 |
| Depth 2 | 15 | post/selective read/reaction 가능 |

따라서 community 참여 가능 인원 70명은 `55+15`에서 나온 **파생값**이다. 매일 70명이 글을 쓰거나 읽는다는 뜻이 아니다. `30/55/15`는 source persona에서 온 고유 속성이 아니라 이번 baseline의 frozen information-access assignment다. current DB의 100개 assignment는 이미 이 비율이므로 그대로 사용하고, `first N` 선택·재추첨·Depth2 자동 치환·새 ratio 배정은 하지 않는다.

persona는 기억이 수정하는 대상이 아니다. 현재 DB `news_depth`와 persona prompt의 depth 문장은 agent ID별로 60/100 불일치한다. 실제 runtime은 DB field로 권한을 실행하므로, DB structured row로 prompt 100개만 canonical renderer에서 재생성해 mismatch를 0으로 만든 sealed persona snapshot을 쓴다. `scripts/01_build_persona.py`는 cohort/depth를 새로 뽑고 agents table을 다시 만들므로 복구에 사용하지 않는다. repair manifest에는 depth 변경=0, non-prompt structured-field 변경=0, old/new prompt hash, parser round-trip, two-arm byte identity를 남긴다. 이후 `agent_id → news_depth` manifest를 유일한 assignment source로 두고 prompt는 deterministic projection으로만 생성한다; 어느 하나만 수정되면 preflight가 FAIL한다.

### 7.2 Best 5의 정확한 의미

`Best 5`는 강제 게시물 수가 아니라 ranking 상한 `K=5`다.

| available post 수 | 노출 |
|---:|---|
| 0 | 없음 |
| 1–4 | 존재하는 post 전부 |
| 5 이상 | 상위 5개 |

community phase는 모든 거래일 PM 뒤 실행한다. Best는 다음 AM에 **원문 본문 전체**를 보여 준다. 제목만 보여 주고 agent가 `post_id`를 따로 검색하는 구조가 아니다. 매 exposure는 viewer, event, post ID, rank, score, delivered original body hash/version, delivered time, channel을 기록한다. 마지막 거래일 PM에도 phase와 checkpoint는 실행·기록하지만 연구기간 안 다음 AM이 없어 next-AM Best broadcast/exposure는 0이다. non-empty Best가 있으면 schedule status는 `right_censored`, Best가 없으면 status는 `empty`다. 같은 PM의 D1/D2 선택 열람 exposure는 존재할 수 있다.

Depth 1/2의 selective reading은 제목 목록에서 선택한 뒤 selected original body를 읽는 기존 흐름을 유지할 수 있다. 다만 thinking summary의 앞 200자 truncation은 원문 delivery의 대체가 아니며, post body가 실제 어떤 prompt에 전달됐는지 trace로 증명돼야 한다.

### 7.3 community privacy and causality boundary

공개글의 내용은 글쓴이의 실제 체결·보유·수익과 일치하도록 강제하지 않는다. reader에게 author의 비공개 portfolio, recent trade, private belief, stable agent ID가 들어가면 public communication effect가 아니라 hidden-information effect가 된다. 그런 private field는 reader prompt에서 제거하고, 실제 fill/portfolio 계산에는 canonical ledger만 사용한다. 게시글이나 interpretation claim의 의미·진실성은 agent가 판단하며 server validator는 visibility·ownership·exact quote provenance·privacy만 검사한다.

---

## 8. FUSE-inspired STB/LTB belief architecture

### 8.1 채택 및 비채택 범위

FUSE의 핵심은 current short state를 만들고 previous long state와 재귀적으로 통합해 새 long state를 계속 쓰는 구조다. 본 연구는 이 재귀 골격을 채택하되, FUSE의 social opinion state나 별도의 세 번째 persistent belief는 복사하지 않는다. 기존 삼성 코드의 `dim_1`~`dim_6`을 그대로 유지하고 시간적 역할만 나눈다.

```text
current permitted real/fake news + actual community exposure
  → STB_t (current-only six dimensions)

previous LTB_(t-1) + STB_t + current price/portfolio/constraint
  → Decision-Making Process → decision_t (BUY/SELL + quantity)
  → constraint validation/execution → committed fill_t

previous LTB_(t-1) + STB_t + decision/fill episode_t + eligible earlier price outcome
  → recursively reinterpreted LTB_t (same six dimensions; visible next event)
```

`decision/fill episode_t`는 단순 append 문자열이 아니다. LTB/STB가 어떤 판단을 만들었는지의 structured decision trace, 실제 `fill_t`, fill price, pre/post cash·holdings, outcome-pending 상태를 한 묶음으로 둔 input이다. LTB updater는 이를 현재 STB·이전 LTB·성숙한 과거 outcome과 함께 해석해 여섯 차원을 새로 작성한다. 모든 decision event에서 STB 1회, LTB 1회를 수행하며 `maintain`으로 LTB call을 생략하지 않는다.

### 8.2 state 역할과 prompt boundary

| object | agent-visible use | 금지되는 use |
|---|---|---|
| `STB_t` | 이번 event의 current-only 정보 해석 | prior LTB, portfolio, historic fill, target label 입력 |
| `LTB_(t-1)` | 이번 거래의 축적된 여섯 차원 | raw current news를 우회해 중복 입력 |
| current execution state | current price, cash, holdings, allowed BUY/SELL constraint | belief state로 저장·재주입 |
| `belief_summary` | 사람/legacy report용 projection | STB/LTB/analysis/decision prompt |
| `view_change` | LTB 변화 근거를 설명하는 deterministic trace; post-writing private context | memory updater, analysis, decision, community interpretation |
| `fill_t` | canonical ledger; **post-fill LTB update**와 PM post-writing에 committed structured fact로 사용 | same-turn STB/analysis/decision 입력, 미래 price outcome을 사실처럼 포함 |

즉 매 거래는 `previous LTB + current STB`라는 두 belief block을 사용한다. summary가 여섯 차원의 대체물이 되면 기존 구조화 belief를 버리는 셈이므로 금지한다.

### 8.3 거래 성찰과 가격 outcome

각 fill episode는 체결 직후의 거래 사실과, 나중에 도래하는 가격 관찰을 분리한다. 체결 직후 LTB에는 actual side/quantity/price·pre/post portfolio·`outcome_pending`만 들어간다. 성공/실패 평가는 아래 관찰이 도래한 뒤에만 들어간다.

| horizon | observation time | LTB에 쓰는 시점 |
|---|---|---|
| next-turn | 다음 decision event | 그 event의 post-decision LTB update |
| H1 | 다음 거래일 동일 subturn | due event의 post-decision LTB update |
| H5 | 5번째 다음 거래일 동일 subturn | due event의 post-decision LTB update |

이것은 미래 정보를 현재 turn에 넣는 것이 아니다. outcome은 `available_at_event_id ≤ current_event_id`일 때만 eligible reflection packet이 된다. 마지막 구간에 due event가 없으면 missing을 임의로 채우지 않고 `right_censored`로 기록한다.

가격 성찰은 직접적으로 `dim_6`의 자기 투자평가에만 반영한다. 새 뉴스·community evidence 없이 H1/H5 결과만으로 `dim_1`~`dim_5`의 시장 전망·가치·거시·심리를 뒤집으면 안 된다. 수수료는 이번 study에서 0원이므로 gross fill-price timing markout을 쓴다.

### 8.4 게시글과 view change

PM 게시글은 내용을 그대로 dump하는 것이 아니라 다음 private context로 자연어 글을 생성한다.

```text
LTB_t six dimensions
+ deterministic LTB-linked view_change
+ committed structured PM fill_t
→ post text
```

post에는 `input_ltb_id`, `view_change_id`, `fill_id`, prompt/output hash를 저장한다. public post가 fill facts를 충실히 말한다는 가정은 하지 않는다. post에 쓰인 private fact가 글을 읽는 다른 agent에게 hidden raw data로 직접 전달되지는 않는다.

### 8.5 FUSE와의 관계를 논문에서 쓰는 방식

- FUSE의 `previous LTM + current STM → new LTM` 재귀 통합을 채택한다.
- FUSE의 LTM을 “최근 5일만 저장”하는 구조로 오해하지 않는다. 5 trading days는 여기서 H5 outcome horizon일 뿐 memory retention limit가 아니다.
- 프로젝트 차이 때문에 `LTB_(t-1) + STB_t`가 별도 Decision-Making Process를 거쳐 `decision_t → fill_t`를 만들고, 그 **결정·실제체결 episode**를 사용해 거래 뒤 `LTB_t`를 다음 event용으로 새로 쓴다. 이것은 FUSE의 문자 그대로 복제가 아니라 double counting을 피하기 위한 project-specific adaptation이다.
- FinMem/TradingGPT의 multi-layer retrieval, top-K/decay, weekly extended reflection, future-label training은 이번 v1에 도입하지 않는다.

참고: [FUSE paper](https://aclanthology.org/2025.emnlp-main.1330/), [TwinMarket paper](https://papers.neurips.cc/paper_files/paper/2025/file/5bf234ecf83cd77bc5b77a24ba9338b0-Paper-Conference.pdf), [FinMem](https://arxiv.org/abs/2311.13743), [TradingGPT](https://arxiv.org/abs/2309.03736).

---

## 9. Decision, fill, fee, and target architecture

### 9.1 Event state machine

각 ordered decision event에는 side effect가 뒤섞이지 않도록 다음 stage를 둔다.

```text
PREPARE
  → freeze permitted input and due earlier outcomes
  → gather staged STB results for all agents
  → freeze STB batch
  → gather analysis/decision results using previous LTB + current STB
  → validate constraints and commit fill/portfolio batch
  → gather/commit post-fill LTB batch using current decision/fill episode + eligible earlier price outcome
  → PM community post/reaction checkpoint (or OFF no-op)
  → atomic scientific commit + integrity digest
```

LLM/network failure는 logical stage 성공과 구분한다. `logical_request_key`는 `(run_id, agent_id, event_id, stage, schema_version)`로 고정하고, physical retry 수는 별도 log에 둔다. 같은 logical call을 재호출할 때 이미 accepted response가 durable journal에 있으면 그 response를 재사용한다.

### 9.2 거래 규칙

- AM에는 실제 시가, PM에는 실제 종가를 제시한다.
- agent는 `BUY` 또는 `SELL`과 수량을 제안한다. `HOLD`는 baseline action space에 없다.
- cash/holdings constraint를 검증한 뒤 가능한 경우 그 공시가격으로 전량 체결한다.
- fill은 requested order와 구별된다. requested quantity가 제한에 걸리면 actual filled quantity와 이유를 ledger에 남긴다.
- baseline fee policy는 `commission_rate=0`, `sell_tax_rate=0`, `fee_amount=0`이다. config 하나만 0.0005로 남아 있거나 report가 fee-adjusted 값을 쓰면 P0 FAIL이다.

### 9.3 primary target and metrics

각 date의 AM/PM fill을 합쳐 gross signed fill value를 계산하고, 삼성전자 개인 `Individuals` 일별 final net trading value의 sign과 비교한다.

| metric class | 정의/용도 |
|---|---|
| primary direction accuracy | evaluation date에서 simulated daily gross signed fill-value sign과 actual individual net value sign의 일치율 |
| coverage | manifest의 exact evaluation dates 중 valid simulated+actual date 비율; actual/simulation 누락은 primary FAIL, news shortage는 별도 coverage flag와 sensitivity로 보고; intersection drop 금지 |
| aggregate signed value | AM+PM 합산 금액, raw/initial-capital-normalized 모두 보고 |
| buy/sell imbalance | agent count, quantity, value 기준 각각 분리 |
| community effect | OFF/ON paired contrast와 date-level dispersion |
| memory trace | STB/LTB six-dimensional change, source lineage, outcome consumption |
| robustness | 1억/10억 group, leave-one-rich-out, 0/1/5 burn-in sensitivity는 exploratory/robustness |

10억 agent 10명은 인원 10%지만 초기 자본의 약 52.6%를 차지한다. raw value만으로 population-like claim을 하면 wealth effect와 demographic effect가 섞이므로 normalized 및 rich-excluded sensitivity를 필수로 같이 보고한다.

---

## 10. strict reasoning-off and reproducible API execution

reasoning UI를 숨기는 것과 provider가 reasoning tokens를 만들지 않는 것은 다르다. paper run의 central request는 아래 policy를 runtime에서 실제로 전송·감사해야 한다.

```json
{
  "reasoning": {"effort": "none", "exclude": true},
  "provider": {"only": ["<approved-provider>"], "allow_fallbacks": false}
}
```

실제 model/provider API contract와 다르면 manifest에 지원되는 strict-off form을 적고 canary로 증명한다. canary와 final audit에는 requested model/provider, returned model/provider, fallback chain, reasoning fields, reasoning token count, raw request/response hash가 있어야 한다. reasoning token이 0인지 증명하지 못하거나 provider drift/fallback이 있으면 pause한다.

offline stub, implicit default model, fallback retry, manual response replacement는 paper run에서 금지한다. timeout/transport retry는 허용할 수 있지만 logical response journal과 immutable request hash를 통해 어떤 응답을 scientific state에 소비했는지 구분해야 한다.

---

## 11. canonical logs, legacy convention, and reporting

### 11.1 로그 원칙

기존 로그를 지우거나 이름을 바꾸지 않는다. legacy `agent_turns`, `submitted_orders`, `exchange_fills`, `portfolio_updates`, `community_posts`, `community_interactions`, `community_best_posts`, `community_logs`는 유지한다. 새 분석 요구는 sidecar/추가 열로 보강한다.

| canonical artifact | 핵심 식별자/내용 | 목적 |
|---|---|---|
| `RN_COMM_*/paper_run.sqlite#paper_fill_ledger` + `traces/rn_comm_*_final_fill_ledger.csv` + `traces/final_fill_export_index.json` | `fill_id`, event/agent/decision/STB/LTB lineage, requested/filled qty, fill price, fee=0, cash/holdings pre/post, CSV hash/row count | arm별 append-only final fill ledger |
| `RN_COMM_*/paper_run.sqlite#turn_belief_trace` | STB/LTB ID, parent, six dimensions, input/output hash, source IDs | memory causal chain |
| `RN_COMM_*/paper_run.sqlite#trade_outcomes` | fill ID, horizon, available-at event, mark price, consumed-by LTB | no-leakage reflection audit |
| `RN_COMM_*/paper_run.sqlite#agent_exposures`와 news/evidence observations | article ID/hash/version, event, delivery channel, real/fake private origin | exact news/exposure proof |
| `community_interactions.csv`, `community_best_posts.csv`, `traces/community_exposure_trace.jsonl` | candidate/Best, reader/post/event/rank/body hash/actual delivery/claim IDs | Best/selected read proof |
| `traces/community_post_trace.jsonl` | post ID, source LTB/view_change/fill, output hash | private post provenance |
| `RN_COMM_*/response_journal.sqlite` + `RN_COMM_*/openrouter_attempts.jsonl` | logical request/accepted response/commit과 physical attempt/acceptance | resume/retry/reasoning-off reproducibility |
| root·arm별 `RUN_RECORD.md` + `RUN_FINALIZATION.json` | artifact paths, schema/hash/status | human index와 final integrity gate |

`paper_fill_ledger`에 `fill_t`를 남기는 이유는 사후 논문 분석에서 trading facts를 다른 파일 여러 개를 뒤져 재구성하지 않기 위해서다. 하지만 ledger 하나가 모든 설명을 대체하지는 않는다. 각 trace는 `fill_id`, `event_id`, `agent_id`, `run_id`로 join 가능해야 한다.

### 11.2 schema and atomicity

- final fill ledger와 memory trace는 `INSERT OR REPLACE`로 과거 scientific fact를 덮어쓰지 않는다.
- event-level unique key가 중복되면 “마지막 행 선택” 대신 integrity failure다.
- DB/CSV/JSONL export에는 `schema_version`, `run_id`, manifest hash를 넣는다.
- checkpoint snapshot/merge/rollback/reset에 새 STB/LTB/fill/outcome/exposure/journal artifact를 모두 등록한다.
- API audit은 phase attempt와 commit status를 기록한다. 실패해 rollback된 physical call과 final committed logical result를 report에서 섞지 않는다.

### 11.3 report architecture

새 validator/PDF report는 `--run-dir`로 명시된 sealed RunBundle과 its `RUN_RECORD.md`만 읽는다. `outputs/logs/current`, global `sys_100.db`, 과거 report의 hardcoded run ID/date/agent count는 paper report의 input이 될 수 없다.

report에는 적어도 다음을 포함한다.

1. study/condition/model/calendar/cohort/news/prompt source hashes와 resolved counts
2. integrity, strict reasoning-off canary, real-news target-10/actual coverage, shortage exception, fake isolation, date equality 결과
3. primary daily direction metric 및 raw/normalized value table
4. community exposure/post/reaction count와 full-body delivery evidence
5. STB/LTB update completeness, lineage, reflection maturity/right-censor summary
6. fill/portfolio reconciliation, fee=0 verification
7. subgroup/robustness results와 exploratory label
8. known limitation, exclusion, right-censor, failed/aborted run record

기존 PDF generator는 historical convention을 보존하는 archival tool로 남길 수 있으나, 새 study report를 만들기 전에 run-local manifest contract로 교체하거나 wrapper를 통해 완전히 입력을 고정해야 한다.

---

## 12. integrity, validation, and adversarial checks

### 12.1 baseline P0 acceptance criteria

아래는 모두 automatic fail-closed가 되어야 한다.

- requested cohort가 100명과 exact roster hash에 일치하지 않음
- D0/D1/D2가 `30/55/15`와 다르거나 persona bytes가 pair에서 다름
- date/event/price/news/target 하나가 빠져 planned calendar가 축소됨
- real article count가 target 10에 못 미치는데 shortage exception record가 없거나, duplicate/unsafe/unknown payload가 있음
- fake row/metadata가 RN bundle 또는 RN runtime prompt에 하나라도 있음
- two arms의 structural diff에 `community_mode` 외 값이 있음
- STB, LTB, decision, fill, portfolio transition의 committed key set이 event calendar와 다름
- future outcome/target label이 prohibited prompt stage에 들어가거나, current fill이 pre-fill STB·analysis·decision에 들어가거나 post-fill LTB에서 빠짐
- D0 Best body delivery, D1/D2 permission, private profile boundary가 policy와 다름
- fee가 0이 아님
- strict reasoning-off telemetry, model/provider pin, no-fallback proof가 없음
- output/report가 global path/current pointer/old DB를 읽음

### 12.2 dynamic property tests

설계가 45일·AM/PM·100명에만 우연히 맞는지 막기 위해 작은 synthetic registry에서도 property test를 둔다.

```text
N ∈ {1, 7, 100}
D ∈ {1, 2, 45, 63}
session set ∈ {AM}, {AM, PM}, non-uniform event list

per arm:
  committed STB transitions = N × U
  committed LTB transitions = N × U
  LTB states including initial LTB0 = N × (U + 1)
  decision/fill/portfolio transitions = N × U
```

community count는 `D×70` 같은 곱셈으로 검사하지 않는다. event별 eligible set, board availability, next-AM existence, right-censoring으로 resolver가 계산한다.

### 12.3 required adversarial fixtures

| fixture | 기대 결과 |
|---|---|
| 9 real articles only | safe pool reselect 후 10을 우선 시도; 불가능하면 9개의 safe bundle과 shortage metadata를 봉인하고 run 계속 |
| one unsafe/backdated article | selection/review rejection; date shrink 금지 |
| duplicate article payload | exact slot validation FAIL |
| RN bundle with hidden fake marker or stale fake journal | clean origin closure FAIL |
| Best board with 0/3/7 posts | 0/3/5 full-body exposure; forced post 없음 |
| Depth0 reader | Best full body next AM only; post/select/reaction=0 |
| D2 reader | no private author portfolio/recent trade/belief field |
| current fill sentinel | same-turn STB/analysis/decision request에서는 0, post-fill LTB request에서는 정확히 1개의 committed fill packet |
| future H1/H5 sentinel | due event 전 LTB request에서 0 occurrences |
| checkpoint interruption | resumed output IDs/hashes equal to uninterrupted logical path |
| provider fallback/reasoning token | strict-off canary FAIL |
| missing evaluation date | primary metric FAIL; actual∩simulation silence 금지 |

---

## 13. Minimal-change implementation map

이 설계는 기존 코드를 전면 재작성하라는 요구가 아니다. 기존 중앙 LLM client, checkpoint/rollback, logger, integrity validator, legacy export는 재사용 가치가 크다. 다만 research input, event lifecycle, trace contract를 명확히 분리해야 한다.

| layer | 최소 변경 책임 | 현재 위험을 해결하는 이유 |
|---|---|---|
| `StudySpec` / resolver | immutable spec→resolved manifest/event calendar | 45/90/42, `×2`, first-N, silent intersection 제거 |
| launcher | RN 전용 2-arm entrypoint | 30명/old six-condition/fake-on default 격리 |
| news preparation | safe pool→target-10 slot registry + shortage exception | 기사 부족을 숨기지 않고 raw version ambiguity 차단 |
| runtime | staged `STB → decision/fill → LTB → community` | single pre-trade belief와 partial side effect 분리 |
| memory DB | STB/LTB states, lineage, outcome records | `belief_summary` only / one-table overwrite 문제 해결 |
| community | full-body Best exposure + public boundary | D0 exclusion/title-only/private portfolio leak 해결 |
| LLM client | strict reasoning-off/provider pin/journal | hidden reasoning/fallback/retry drift 방지 |
| checkpoint | artifact registry, idempotency, committed status | new trace file loss·duplicate·response drift 방지 |
| validator/report | explicit sealed run directory only | global DB/current/legacy default contamination 차단 |

새 state table과 legacy compatibility를 함께 유지하는 권장 방향은 `belief_history`를 삭제하지 않고, 완성된 LTB의 human-readable projection을 남기는 것이다. 실제 decision에는 projection이 아니라 typed six dimensions를 넣는다.

---

## 14. Handoff protocol and change control

### 14.1 역할별 인수인계

| 역할 | 넘겨야 할 것 | 완료 판단 |
|---|---|---|
| data owner | raw snapshot, safe pool, selection/review manifests | target-10/actual slot map와 provenance/shortage proof |
| simulation owner | StudySpec/resolver/launcher/runtime migration | manifest-only execution, no unsafe default |
| LLM owner | client/prompt schema/canary/journal | reasoning=none/provider/no-fallback evidence |
| community owner | permission/full-body exposure/post trace | D0/D1/D2 contract and privacy tests |
| validation owner | integrity/direction/memory/fill validators | fail-closed P0 and dynamic property tests |
| report owner | RUN_RECORD-driven PDF/data dictionary | run-local reproducible report |
| paper owner | preregistration/limitations/figure table mapping | claims match registered outcome/phase |

### 14.2 amendment rules

아래 중 하나가 바뀌면 기존 run에 patch를 덧씌우지 않고 new study/run version을 연다.

- cohort/persona/depth/asset distribution
- target ticker, calendar, session, burn-in, primary metric
- news selection policy, safe pool, article representation, real count
- fake stimulus, injection schedule, fake polarity definition
- STB/LTB semantics, prompt role, visibility timing, reflection horizon
- fee/action/execution policy
- model/provider/reasoning/retry policy
- community permission, Best policy, post/reading semantics

변경 기록에는 `what`, `why`, affected artifact, old/new hashes, compatibility status, whether prior result remains comparable를 적는다. “작은 config 변경”이라도 resolver output이나 prompt bytes가 바뀌면 paired comparison이나 restart parity가 깨질 수 있다.

### 14.3 release gates

```text
Design review
  → implementation PR/review
  → unit + property + red-team fixture PASS
  → deterministic small canary
  → strict reasoning-off live canary
  → 100-agent short dry run
  → interruption/resume parity
  → paid baseline pair
  → integrity PASS 또는 non-blocking PASS_WITH_NEWS_SHORTAGE
  → frozen validation/report
  → fake phase eligibility decision
```

중간 단계 실패는 “실험 결과가 나쁘다”가 아니라 engineering/integrity failure다. 논문용 결과와 분리해 issue log에 남기고, fix 뒤에는 새 manifest/run ID로 다시 시작한다.

---

## 15. 논문·보고서에서 반드시 공개할 사항

1. simulated agents, population representativeness limits, and no human behavioral causal claim
2. target is daily Samsung individual net-trading direction; AM/PM target unavailable
3. current input is real-news-derived title/summary feed unless raw full body is actually delivered
4. community OFF/ON pair only differs by registered community availability
5. target-real-10 policy, shortage exception, and full safe-pool selection/provenance method
6. fake phase is a later synthetic-input experiment with the same frozen real bundle + 1 fake only at registered injection events
7. FUSE-inspired recursive STB/LTB is a project-specific adaptation, not an unchanged reproduction
8. BUY/SELL-only action space, actual open/close fill, zero fee policy
9. burn-in, right-censoring, failures/exclusions, and all run hashes
10. outcome/wealth concentration robustness and any uncorrected multiplicity/exploratory analysis label

---

## 16. Final implementation definition of done

이 아키텍처가 “구현되었다”고 말하려면 다음이 모두 성립해야 한다.

- Phase 1 two-arm paper launcher가 frozen real-news **목표 10개** registry와 documented shortage exception만 사용한다.
- 100명 frozen cohort와 `30/55/15` map이 run-local DB/prompt/report 모두에서 같은 hash로 재현되고, parsed prompt depth/permission도 DB와 agent ID별 100/100 일치한다.
- event마다 current-only STB 1개, Decision-Making/actual fill 1개, **same-turn post-fill LTB 1개**가 typed lineage로 연결되고 LTB는 next event에만 decision-visible이다.
- fill episode와 next-turn/H1/H5 observation이 time gate와 right-censor를 가진다.
- community ON의 actual full-body delivery와 community OFF의 zero exposure가 trace로 증명된다.
- strict reasoning-off is proven from live API telemetry, not presumed from hidden UI output.
- fee=0, target/actual article count와 shortage provenance, no fake in RN, no silent date reduction, no global-report input이 automated integrity에서 강제된다.
- checkpoint-resume가 same resolved manifest와 response journal에서 idempotently 이어지고, final report가 one run directory만으로 재생성된다.
- future fake phase는 real baseline bundle을 보존하면서 `target 10 real (actual frozen) + 1 fake` policy, private label isolation, matched injection schedule을 새 manifest로 증명한다.

이 조건 중 하나라도 빠지면 결과를 exploratory debugging output으로 보관할 수는 있어도, baseline 결과나 논문 근거로 봉인하지 않는다.
