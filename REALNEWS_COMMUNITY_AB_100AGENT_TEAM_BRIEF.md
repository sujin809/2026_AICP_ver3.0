# 팀 공유용: Samsung 실제뉴스 Community ON/OFF 100-Agent Baseline

> 상태 (2026-07-23): **설계 계약은 유지하고 RN four-stage core의 구현·무과금 적대적 검증은 완료**. 다만 live canary와 승인 artifact/provider가 없으므로 유료 본 실행은 계속 NO-GO다. 최신 상태는 [적대적 검증 보고서](RED_TEAM_VALIDATION_REPORT.md)를 따른다.
>
> 상세 근거와 파일별 구현 계약: [REALNEWS_COMMUNITY_AB_100AGENT_FUSE_MEMORY_DESIGN.md](REALNEWS_COMMUNITY_AB_100AGENT_FUSE_MEMORY_DESIGN.md)

## 1. 이번 baseline의 목적

삼성전자 `005930`에 대해 실제 뉴스만 사용하여, community 사용 가능 여부가 100명의 LLM 투자자 belief와 거래 반응에 만드는 차이를 확인한다. 주가 예측 실험이 아니라, agent 집단의 거래 방향이 삼성전자 실제 개인투자자 `Individuals` 일별 순거래대금 방향을 얼마나 따라가는지 검증하는 baseline이다.

| 조건 ID | 실제 뉴스 | 가짜 뉴스 | Community |
|---|---:|---:|---|
| `RN_COMM_OFF` | 목표 10개; safe pool 부족 시 실제 count/사유를 봉인 | 0개 | 완전 비활성, no-op checkpoint만 기록 |
| `RN_COMM_ON` | `RN_COMM_OFF`와 동일한 frozen bundle | 0개 | 원 설계 권한으로 활성 |

두 조건에서 달라지는 것은 **community availability 하나뿐**이다. cohort, persona, 초기자산, 가격, 뉴스 bundle, 정보 cutoff, 모델/provider, prompt, seed, STB/LTB 갱신 주기, 수수료, retry 정책은 동일해야 한다.

현재 승인 예시는 45거래일, AM/PM 두 decision event, 총 90 event이며 첫 3거래일을 burn-in으로 제외해 42일을 primary 분석에 쓴다. 다만 코드에는 45·90·42를 쓰지 않는다. 날짜, event 수, 평가 분모는 frozen registry를 resolver가 `D/B/E/U`로 계산한다.

## 2. 핵심 로직: 기존 6차원 belief를 유지한 STB/LTB

기존 `dim_1~dim_6`의 의미·전망 horizon은 바꾸지 않는다. 새 차원이나 세 번째 persistent belief를 만들지 않고, 같은 여섯 차원을 시간 역할만 나눈다.

```text
현재 허용 뉴스 + 실제로 전달된 community 해석
        ↓
current-only STB_t (dim_1~dim_6)
        ↓
previous LTB_(t-1) + STB_t + 현재 가격/portfolio/제약
        ↓
Decision-Making Process → BUY/SELL·수량 decision_t
        ↓
제약 검증·실제 체결 → actual fill_t
        ↓
previous LTB_(t-1) + STB_t + decision/fill episode_t + 관찰 가능한 과거 가격 성찰
        ↓
다음 event용 recursive LTB_t (같은 dim_1~dim_6)
```

- `STB_t`: 현재 event에 새로 허용된 뉴스와 실제 노출된 community만 해석한다. 이전 belief, 시장/portfolio 상태, 과거 체결 성찰은 넣지 않는다.
- `LTB_t`: 이전 LTB, 현재 STB, **이번 turn의 decision/fill episode**와 그 시점에 관찰 가능한 과거 가격 성찰을 재귀 통합해 거래 뒤 새로 작성한다. `fill_t`는 append log가 아니라 “이 판단이 실제 어떤 체결·portfolio 변화를 만들었는가”를 LTB가 재해석하는 structured input이다.
- 거래는 항상 `previous LTB + current STB` 두 block을 분리해 쓴다. 현재 가격·현금·보유수량·허용 action은 execution-state로 직접 들어간다.
- current `fill_t`는 생성 전인 same-turn STB/analysis/decision에는 되돌아가지 않는다. 그러나 체결 뒤 post-fill `LTB_t`에는 즉시 들어간다. `LTB_t`는 다음 event부터 사용하며, `fill_t`의 가격 성과 평가는 next-turn/H1/H5가 관측된 뒤에만 추가된다.
- agent·arm별 불변식은 `committed STB update 수 = committed LTB update 수 = resolved decision-event 수 U`다. transport/schema retry는 별도 physical attempt다.
- LTB는 매 event 여섯 차원을 새 텍스트로 다시 작성한다. `maintain` 또는 이전 문장 그대로 복사는 허용하지 않는다.

### 거래 성찰과 `dim_6`

각 fill은 하나의 episode로 다음을 순서대로 남긴다.

- `next-turn`: 다음 decision event 가격
- `H1`: 다음 거래일 동일 subturn 가격
- `H5`: 5거래일 뒤 동일 subturn 가격

아직 도래하지 않은 관찰은 `right_censored`다. 가격 성찰은 LTB `dim_6`의 자기 투자평가에만 들어가며, 새 뉴스·community 근거 없이 `dim_1~dim_5`를 바꾸면 안 된다.

이번 baseline은 **수수료 0원**이다. `commission_rate=0`, `sell_tax_rate=0`, 모든 `fee_amount=0`을 config·exchange·portfolio·PnL·ledger·CSV·manifest에 강제한다. 그래서 `dim_6`은 공시 체결가격 기준 gross timing markout을 사용한다.

### 사람용 summary와 post-writing 예외

- `belief_summary`: 사람이 읽는 로그 전용이며 어떤 agent prompt에도 넣지 않는다.
- `view_change`: 이전/새 LTB의 6차원 차이와 integration evidence에서 서버가 결정론적으로 만든 trace다.
- PM post-writing private context에서만 `LTB_t 6D + deterministic view_change + 이미 commit된 PM structured fill_t`를 사용해 자연어 게시글을 만든다.
- `belief_summary`, `view_change`는 STB/LTB updater, analysis, decision, community interpretation에 넣지 않는다. current fill은 STB/analysis/decision/community interpretation에는 넣지 않지만 **post-fill LTB updater에는 넣는다.**

공개 게시글의 내용은 실제 fill/portfolio와 일치하도록 강제하지 않는다. 실제 체결과 보유 상태를 계산할 때는 public post가 아니라 canonical ledger를 사용한다.

## 3. Community 원 설계의 확정 해석

| 구분 | 인원 | 권한 |
|---|---:|---|
| Depth 0 | 30 | 글쓰기·선택 열람·reaction 없음. 다음 AM 해석 입력으로 Best 원문을 수동 선택 없이 전달받음 |
| Depth 1 | 55 | posting·선택 열람·reaction 가능 |
| Depth 2 | 15 | posting·선택 열람·reaction 가능 |

- Depth 2가 가장 적은 `30/55/15`가 맞다. 이는 source persona의 외부 고유 속성이 아니라 이번 baseline의 **frozen information-access assignment**다. 현재 DB의 100개 assignment를 유지하고, 실제 DB 권한과 persona prompt의 depth/permission 문구를 agent ID별 100/100 일치시킨다.
- active 70명은 **참여 가능 인원**이지 매일 반드시 글을 쓰거나 읽는 인원이 아니다.
- `Best 5`는 **상위 최대 5개**다. 0개면 노출 0, 1~4개면 있는 글 전부, 5개 이상이면 상위 5개를 노출한다. 5개를 채우기 위한 강제 posting·합성 post는 금지한다.
- community phase는 **모든 거래일 PM 뒤** 실행한다. non-empty Best는 제목뿐 아니라 **원문 본문 전체**를 다음 거래일 AM에 100명 모두에게 보여 준다. 본문을 제목만 보고 agent가 별도로 찾아가는 구조가 아니다.
- 마지막 거래일 PM에도 community phase와 checkpoint는 실행·기록한다. 연구기간 안 다음 AM이 없어 next-AM Best broadcast/exposure는 0이다. non-empty Best가 있으면 schedule status는 `right_censored`, Best가 없으면 status는 `empty`다. 같은 PM의 D1/D2 선택 열람 exposure는 존재할 수 있다.
- Community 효과는 좋아요·게시글 수가 아니라 `full-body exposure → interpretation claim → STB evidence → decision/fill` trace로만 해석한다.
- Depth 2가 작성자의 실제 portfolio·최근 trade를 public community prompt에서 보던 현재 경로는 제거한다. 공개 badge/허용 profile만 쓴다.

## 4. 기존 코드 문제와 수정 방향

| 기존 문제 | 새 baseline의 수정 방향 |
|---|---|
| launcher가 30명·옛 fake condition을 기본으로 둠 | `RN_COMM_OFF/RN_COMM_ON` 전용 launcher와 frozen 100명 cohort manifest 사용 |
| 특정 날짜 뉴스가 빠지면 stock/news 교집합으로 기간이 조용히 짧아짐 | 시장 calendar/event registry를 먼저 고정하고, 뉴스·가격·target 누락은 fail-closed |
| 현재 90 event 중 2026-03-23 PM이 실뉴스 9개 | provenance-safe 기존 pool에서 우선 10개로 재선정. 불가능하면 9개 safe bundle과 shortage 사유/실제 count를 봉인해 run은 계속하고, 두 arm·report에 동일하게 반영 |
| DB `news_depth`는 30/55/15지만 persona prompt의 depth 문구가 60/100명에서 다른 agent를 가리킴 | `sys_100.db`의 100개 depth 값은 바꾸지 않고, DB structured row에서 prompt 100개를 전부 재-render한 sealed persona snapshot을 만든다. `scripts/01_build_persona.py` 재실행·재추첨은 금지 |
| 기사 URL·원문 version·cutoff provenance가 runtime에서 사라짐 | immutable raw snapshot, source/published/observed time, body hash, leakage review manifest를 봉인 |
| 직전 belief를 6차원이 아닌 `belief_summary` 중심으로 조회 | 기존 `belief_history`는 LTB로 유지하고 current-only STB history·lineage를 추가 |
| 체결 전 belief가 저장되고 PM posting도 체결 전 belief를 사용 | `STB → 거래 → fill → LTB → post` 순서로 재배치 |
| H1/H5·fill ID·관찰시점 원장이 없음 | append-only fill episode/outcome ledger와 horizon별 consumption ID 추가 |
| `COMMISSION_RATE=0.0005` 설정과 실제 fee=0이 불일치 | 이번 study의 모든 계층을 fee=0으로 봉인·검증 |
| reasoning을 실제로 끄는 request가 없음, fallback 가능 | 모든 physical request에 `reasoning.effort="none"`, provider pin, fallback 금지, canary token 0 증명 |
| Best 5가 제목·유형·점수만 전달되고 Depth 0은 완전 제외 | Best frozen full body를 다음 AM 100명에게 broadcast하고 actual exposure trace 추가 |
| legacy `trade_log`가 pending row를 덮어쓰고 fill lineage가 없음 | arm별 `RN_COMM_*/paper_run.sqlite#paper_fill_ledger` append-only ledger + `traces/rn_comm_off_final_fill_ledger.csv`, `traces/rn_comm_on_final_fill_ledger.csv` + hash index |
| 기존 report가 전역 DB·고정 agent/date/legacy C-code에 의존 | run-scoped evaluator/report만 새 metric source로 허용. 기존 report는 archive-only |
| retry/resume이 logical update를 중복하거나 input drift를 놓칠 수 있음 | response journal, idempotency key, phase transaction, resume digest, phase별 input re-hash |

### 모듈성과 하드코딩의 기준

새 paper path의 연결은 `StudySpec → resolved manifest → event scheduler → STB/decision-fill/LTB/community staged result → atomic commit → validator/report` 하나여야 한다. 100명·`30/55/15`·Best 5·목표 뉴스 10개 같은 **연구 설정**은 version/hash가 있는 manifest에만 두고, `90회`, `70명`, `×2`, `2d-1`, fixed date, legacy condition, `current` path는 코드 기본값으로 두지 않는다. agent 수·기간·AM/PM 구성이 바뀌면 resolver가 모든 count와 key set을 다시 계산해야 하며, report도 같은 run-local manifest만 읽는다.

## 5. reasoning off와 재현성

Reasoning을 숨기는 것과 실제로 끄는 것은 다르다. 본 실행의 모든 LLM physical HTTP request는 아래 정책을 만족해야 한다.

```json
{
  "reasoning": {"effort": "none", "exclude": true},
  "provider": {
    "only": ["<pinned-provider>"],
    "order": ["<pinned-provider>"],
    "allow_fallbacks": false,
    "require_parameters": true
  }
}
```

- `exclude=true`만으로는 통과하지 않는다.
- returned model/provider가 request와 같고, reasoning fields가 비어 있으며 `reasoning_tokens=0`임을 stage별 live canary에서 확인해야 한다.
- telemetry 누락, nonzero reasoning token, provider/model mismatch, offline stub, fallback은 즉시 NO-GO다.
- strict-off 범위는 **runtime의 모든 LLM request**다. 기존 frozen news preprocessing은 재실행하지 않고 provenance limitation으로 공개한다.

## 6. 로그와 결과를 볼 때의 순서

각 arm run의 첫 진입점은 `RUN_RECORD.md`다. 사람이 수동으로 쓴 일지가 아니라 frozen manifest와 artifact hash에서 렌더된 색인이다.

| 확인 목적 | 우선 artifact |
|---|---|
| 조건·cohort·날짜/event·policy·hash | `inputs/runtime/study_spec.json`, `resolved_study_manifest.json`, `evaluator_contract.json`, root·arm별 `RUN_RECORD.md` |
| 실제 체결·0원 수수료·전후 portfolio | `RN_COMM_*/paper_run.sqlite#paper_fill_ledger`, `traces/rn_comm_off_final_fill_ledger.csv`, `traces/rn_comm_on_final_fill_ledger.csv`, `traces/final_fill_export_index.json` |
| previous LTB + STB → analysis/decision → actual fill → same-turn post-fill LTB → next-event visibility lineage | `RN_COMM_*/paper_run.sqlite#turn_belief_trace` |
| fill의 next-turn/H1/H5 가격 성찰 | `RN_COMM_*/paper_run.sqlite#trade_outcomes` |
| 공개 게시글 원문과 private post context | `RN_COMM_ON/paper_run.sqlite#community_post_trace`, `traces/community_post_trace.jsonl` |
| Best/선택 열람의 실제 본문 노출 | `community_interactions.csv`, `community_best_posts.csv`, `traces/community_exposure_trace.jsonl` |
| reasoning-off·retry·비용·latency | `RN_COMM_OFF/openrouter_attempts.jsonl`, `RN_COMM_ON/openrouter_attempts.jsonl`, 각 arm의 `response_journal.sqlite` |
| 일별 개인수급 검증·두 arm 비교 | evaluator `--output-dir`의 `daily_flow_comparison.csv`, `paired_condition_summary.json`, `direction_validation.json`, `evaluation_artifact_index.json` |

`submitted_orders.csv`, `exchange_fills.csv`, `portfolio_updates.jsonl` 등 기존 CSV/JSONL 파일명과 의미는 legacy 결과에서 보존한다. 현재 RN finalizer의 canonical 파일은 위 표의 arm별 DB·CSV·index다. 과거 `outputs/sys_100.db`, `analysis/current_experiment_review`, 기존 PDF는 비교·보관용일 뿐 새 baseline의 scientific source of truth가 아니다.

## 7. 분석 지표와 report

대상은 코스피 전체가 아니라 **삼성전자 `005930`의 `Individuals` 일별 순거래대금 방향**이다.

- Primary RQ1: `RN_COMM_OFF` 100명의 AM+PM actual fill을 날짜별 gross signed fill value로 합산한 방향 대 실제 `Individuals` 방향.
- Primary 분석은 resolver가 만든 evaluation date set `E`다. 현재 예시는 full `D=45`, burn-in `B=3`, evaluation `E=42`다.
- real-news shortage가 있어도 planned full-calendar primary는 유지한다. 별도로 하루의 모든 AM/PM event가 target 10 및 selected=serialized=delivered-real payload hash equality를 만족한 `complete-news-only` date mask/hash·분모를 resolver가 만들어 sensitivity로 보고한다.
- 필수 지표: confusion matrix, accuracy, buy recall, sell recall, balanced accuracy, MCC, date-level uncertainty.
- 비교선: always-buy/always-sell, 전일 방향, 사전 고정 price-only rule. PM을 포함하므로 primary를 순수 intraday 사전예측이라고 표현하지 않는다.
- AM-only `directional_stance`는 보조 nowcast 진단이다. 실제 개인수급의 intraday label이 없으므로 AM/PM 합산 primary와 혼동하지 않는다.
- RQ2: 동일 날짜/event pair에서 `RN_COMM_ON − RN_COMM_OFF`의 agent-first AM+PM signed notional을 fixed initial capital로 나눈 community 총효과.
- 10억 자산 agent 10명이 총 초기자본의 52.63%이므로 raw, 1억-only, 10억-only, initial-capital-normalized, leave-one-rich-out 결과를 함께 공개한다.

새 report bundle은 `resolved_study_manifest.json`, 양 arm `RUN_RECORD.md`, frozen target, canonical final fill ledger, traces만 읽는다. 기존 `generate_run_report_pdf.py`, `generate_community_report_pdf.py`, `generate_deep_analysis_report.py`, `generate_condition_comparison_report.py`, `validate_trading_direction.py`는 archive/migration 비교용으로만 유지한다.

## 8. P0 Go/No-Go

아래 중 하나라도 실패하면 pause한다. default model, default belief, fallback provider, 조용한 표본 축소로 계속 실행하지 않는다.

- exact 100명 cohort와 30/55/15 depth, persona, 초기자산 hash 고정. `(agent_id, news_depth, parsed permission, prompt_sha256)`가 100/100 일치하고 두 arm byte-identical
- 모든 event의 provenance-safe real news는 10개를 우선 목표로 재선정하고, 부족하면 `news_shortage_exception_manifest.jsonl`에 `selected_safe_count`/`serialized_count`/`delivered_real_count`·payload hash·사유·ordered ID를 봉인한다. `actual_real_count=delivered_real_count`이며 delivery mismatch는 shortage가 아니라 FAIL이다. fake는 0개이고 semantic/article-version leakage review를 통과한다.
- `RN_COMM_OFF/RN_COMM_ON`의 non-treatment manifest hash가 같고 `community_mode`만 다름
- runtime strict reasoning-off canary: request/response/provider/reasoning token 모두 검증
- STB/LTB update 수와 key가 1:1이고, current fill은 post-fill LTB에 정확히 한 번만 들어가며 pre-fill STB/analysis/decision과 미래 outcome leakage는 0
- Best full body와 Depth 0 passive exposure가 trace로 재구성됨
- `commission_rate=0`, `sell_tax_rate=0`, `fee_amount=0`이 fill·portfolio·ledger·export에 모두 일치
- resume 뒤 digest, response journal, prompt/code/news hash가 uninterrupted run과 동일
- evaluator/report가 frozen artifact만 읽고 전역 DB·legacy condition·date intersection fallback을 쓰지 않음
- adversarial fixture가 fake/unsafe news, prompt injection, private portfolio leakage, duplicate retry, cross-arm contamination, report overclaim을 모두 거부

기간·cohort·뉴스 pool·target·memory policy·condition 추가처럼 연구 의미를 바꾸는 결정은 구현자가 임의로 정하지 않고 책임자에게 즉시 확인한다.

## 9. 구현 분담 시 파일 묶음

1. `config.py`, `twinmarket_kr/llm/client.py`, `twinmarket_kr/run_integrity.py`  
   Strict-off, provider pinning, offline/fallback 차단, fee=0 policy, canary/audit.

2. `twinmarket_kr/db/schema.py`, `db/connection.py`, `agents/memory_agent.py`, `experiment_runtime.py`  
   STB history, LTB compatibility, append-only fill/outcome ledger, schema/runtime lifecycle.

3. `core/collect_context.py`, `core/daily_cycle.py`, belief/analysis/decision LLM modules 및 prompts  
   STB/LTB 분리, full 6D 전달, direct historical order 제거, dim6-only reflection.

4. `simulation.py`, `agents/exchange_agent.py`, `run_logger.py`  
   체결 뒤 LTB update, post timing, fill ID, canonical trace, atomic phase/resume.

5. `community/agent.py`, `community/reading.py`, `community/thinking.py`, `community/posting.py`  
   Best full body, Depth0 passive exposure, public-profile allowlist, post private context.

6. `agents/news_agent.py`, news preparation, new RN launcher  
   target-10 provenance-safe slot registry, safe-pool reselect, documented shortage exception, real-only, dynamic calendar/cohort.

7. `validation/validate_realnews_community_ab.py`, report generators  
   `D/B/E` evaluator, paired arm integrity, run-scoped report/PDF bundle.

## 10. 팀이 꼭 기억할 것

- 새 baseline ID는 오직 `RN_COMM_OFF`, `RN_COMM_ON`이다. legacy C-code를 새 artifact/report/run ID에 쓰지 않는다.
- RN core와 local fixture는 구현돼 있다. 다만 실제 유료 연구 실행은 sealed source 입력, live reasoning-off telemetry, 최종 P1/handoff 증거가 모두 생길 때까지 NO-GO이며, 이 문서는 그 acceptance contract다.
- 기존 로그·report convention은 지우지 않는다. sidecar와 run-scoped report로 보강한다.
- 어느 한 agent의 재시도 성공도 STB/LTB logical update를 추가로 만들면 안 된다.
- 실제 개인수급 label은 evaluator 전용이다. prompt, news, STB, LTB에 들어가면 data leakage다.
