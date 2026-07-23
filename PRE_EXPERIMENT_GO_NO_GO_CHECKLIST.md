# 실험 전 Go/No-Go 체크리스트

> 적용 대상: 삼성전자 `005930` 실제뉴스 Community ON/OFF baseline 및 그 후속 fake-news 실험
>
> 상태: **구현 acceptance·실행 승인용 문서**. 한 항목이라도 P0가 미통과면 API 유료 본 실행과 논문용 report 생성을 시작하지 않는다.
>
> 관련 문서: [상세 baseline 설계](REALNEWS_COMMUNITY_AB_100AGENT_FUSE_MEMORY_DESIGN.md), [전체 실험·인수인계 아키텍처](FULL_EXPERIMENT_ARCHITECTURE_FAKE_NEWS_AND_HANDOFF.md), [팀 요약](REALNEWS_COMMUNITY_AB_100AGENT_TEAM_BRIEF.md)

## 사용 방법

각 항목에는 다음 네 가지를 남긴다.

| 필드 | 기록 내용 |
|---|---|
| 상태 | `PASS` / `PASS_WITH_NEWS_SHORTAGE` / `FAIL` / `NOT_RUN` |
| 증거 | artifact path, SHA-256, validator output, test ID |
| 담당 | 확인자와 확인 시각 |
| 예외 | 없으면 `none`; `news_shortage_accepted`는 non-blocking exception ID/hash를 기록하고, 그 밖의 blocking exception은 run을 중단하고 issue ID를 연결 |

`PASS`는 “코드를 읽어 보니 그럴 것 같다”가 아니라 자동 검증 결과와 frozen artifact가 있는 상태를 뜻한다. `PASS_WITH_NEWS_SHORTAGE`는 safe pool 재선정 뒤에도 목표 10개를 못 채운 event가 있으나, actual count·사유·동일 arm bundle·분석 flag를 모두 봉인해 실행은 허용한다는 예외 상태다. `FAIL` 또는 `NOT_RUN`은 우회·수동 보정·기본값으로 계속 실행할 수 없다.

---

## A. 연구 정의와 run bundle

### P0-A1. condition과 treatment

- [ ] 새 baseline condition enum은 정확히 `RN_COMM_OFF`, `RN_COMM_ON` 두 개다.
- [ ] 새 artifact, DB, output directory, report, journal에 legacy `c00_*`, `c10_*`, `C00/C10`, `rn_c00_*`, `rn_c10_*` alias가 없다.
- [ ] 두 arm의 resolved manifest structural diff는 `community_mode` 하나뿐이다.
- [ ] `RN_COMM_OFF`: post/read/reaction/Best actual exposure/claim/community evidence가 0이고 no-op checkpoint만 존재한다.
- [ ] `RN_COMM_ON`: 원 설계의 community permission map과 next-AM visibility가 적용된다.
- [ ] fake mode는 baseline에서 `off`; fake injection path/CSV override/unknown CLI option은 paper launcher가 거부한다.

### P0-A2. authored spec과 resolved manifest

- [ ] 사람이 승인한 입력은 `study_spec.json`, cohort/calendar/news/base registry뿐이다.
- [ ] resolver가 `resolved_study_manifest.json`을 만들고 code/prompt/launcher/scheduler/integrity/evaluator/report가 같은 SHA-256을 사용한다.
- [ ] `N`, `D`, `U`, burn-in set `B`, evaluation set `E`, expected rows/calls/news slots는 authored input이 아니라 resolver output이다.
- [ ] `resolved_study_manifest.json`에 placeholder (`<required>`, `pending`, `TBD`, null)가 없다.
- [ ] run root는 새 namespace이며 `outputs/logs/current`, glob, 기존 runtime DB, symlink를 input으로 쓰지 않는다.

### P0-A3. 현재 baseline의 frozen study assertion

| 항목 | 현재 승인값 | 확인 방법 |
|---|---|---|
| 종목 | `005930` 삼성전자 | target/price/ledger stock code equality |
| agent cohort | 정확히 100명 | ordered agent ID registry hash |
| depth | D0/D1/D2=`30/55/15` (D2 최소) | frozen study-specific information-access assignment의 DB·cohort manifest·parsed persona prompt·report agent-level equality |
| 초기자산 | 1억 90명, 10억 10명 | cohort registry / portfolio turn 0 |
| 날짜 예시 | 2026-02-27~2026-05-04, `D=45` | resolved calendar registry |
| 평가 예시 | `B=3`, `E=42` | frozen date ID/hash |
| 거래 | AM=open, PM=close, BUY/SELL only | event registry / fill validator |
| 수수료 | `commission_rate=0`, `sell_tax_rate=0`, `fee_amount=0` | config/manifest/fill/portfolio/CSV equality |

기간이나 cohort가 바뀌면 위 표의 값은 사람이 직접 수정하지 않는다. registry를 승인하고 resolver가 새 값을 산출한 뒤, 두 arm을 새 run으로 처음부터 실행한다.

---

## B. 데이터·뉴스·시간 안전성

### P0-B1. 목표 10개 real-news bundle과 shortage exception

- [ ] 연구가 사용하는 수집 뉴스 DB에서 provenance-safe article pool을 만들었다.
- [ ] 각 `decision_event_id`에 대해 target `slot_ordinal=1..10` real news ID를 safe pool에서 재선정했다.
- [ ] 현재 example에서는 `U=90` event에 target `900` article slots가 있으며, actual slot total 및 shortage event 수를 manifest에 함께 기록했다.
- [ ] 한 event 안에서 `news_id`, immutable payload hash, article version이 중복되지 않는다.
- [ ] 기존 daily CSV의 9개 slot(현재 2026-03-23 PM)은 수집 DB의 safe pool에서 10개가 되도록 먼저 보충·재선정했다.
- [ ] 보충이 불가능한 event는 unsafe 기사, 새 기사 합성, 같은 기사 중복으로 채우지 않는다. 가능한 safe·unique article만 사용한다.
- [ ] shortage event마다 `target_real_count=10`, `selected_safe_count`, `serialized_count`, `delivered_real_count`, `actual_real_count(=delivered_real_count)`, `missing_real_count(=target_real_count-selected_safe_count)`, candidate pool digest/size, selection algorithm/seed, selection/review reason, ordered ID/payload hash map, pair bundle hash, `news_coverage_status=shortage_accepted`가 `news_shortage_exception_manifest.jsonl`에 있다.
- [ ] `serialized_count`/`delivered_real_count`와 payload hash가 selected map과 다르면 shortage로 덮지 않고 retry 또는 `FAIL`이다.
- [ ] shortage는 run 중단 사유가 아니다. 단, `RN_COMM_OFF`/`RN_COMM_ON`이 같은 shortage bundle과 delivered hash를 쓰고, full-calendar 결과·fixed complete-news-only date mask/hash/denominator·shortage 목록이 report에 함께 있어야 한다.
- [ ] news prompt는 “10개 기사”라는 고정 문구 대신 actual frozen payload만 직렬화하며, shortage quality metadata는 agent-visible input에 없다.
- [ ] `RN_COMM_OFF`와 `RN_COMM_ON`은 같은 read-only news bundle object, canonical row hash, ordered ID map, slot map hash를 쓴다.

필수 artifact:

```text
real_news_bundle_manifest.json
news_slot_manifest.csv/jsonl
news_shortage_exception_manifest.jsonl
article_provenance_registry.jsonl
article_version_leakage_review.csv
```

### P0-B2. provenance와 as-of leakage

- [ ] 각 visible article에 URL, publisher/source, `published_at`, `observed_at`/`scraped_at`, `last_modified_at`, raw body hash, cutoff-time version hash가 있다.
- [ ] AM/PM summary/title/body와 Depth 2 search 결과에 semantic leakage scan을 적용했다.
- [ ] 당일 종가, 확정 고가/저가, 실제 개인수급, 미래 뉴스가 AM 또는 허용되지 않은 PM input에 없음을 human review와 automated scan으로 확인했다.
- [ ] 알려진 `news_20260427_섹터_0032` EOD leakage 반례와 모든 scanner candidate의 reject/mask/allow 결과가 review manifest에 있다.
- [ ] 실제 개인수급 target은 evaluator-only namespace에 있고 runtime prompt, STB, LTB, journal request에 0건이다.
- [ ] stock/news/target date가 하나라도 registry와 다르면 교집합으로 줄이지 않고 `FAIL`이다.

### P0-B3. fake-news isolation

- [ ] baseline manifest: `target_real_news_per_event=10`, `fake_news_per_event=0`, event별 `actual_real_count`/`news_coverage_status`.
- [ ] baseline bundle에는 `is_fake`, `synthetic_id`, `injection_*`, `false_claim`, fake metadata family가 없다.
- [ ] known fake registry ID/title/row-hash와 baseline bundle overlap이 모두 0이다.
- [ ] future fake run은 별도 run namespace와 `target 10 real (actual frozen) + 1 fake` schema를 사용한다. baseline input을 덮어쓰지 않는다.

---

## C. Cohort·persona·community

### P0-C1. cohort integrity

- [ ] requested agent ID set이 frozen registry와 exact equality다. `first_n`, 자동 재추첨, Depth 2 강제 교체가 없다.
- [ ] DB, `fixed_slots.csv`, persona prompt, cohort manifest가 gender/age/age-group/location/strategy/initial cash/depth에 대해 100/100 일치한다.
- [ ] 현재 `sys_100.db`의 `(agent_id, news_depth)` 100개 map을 pre-repair manifest로 hash-pin했다. 이 값은 이번 baseline의 information-access assignment이며, depth를 새로 배정하지 않는다.
- [ ] 현 prompt-depth mismatch 60건을 DB structured row 기반 full rerender로 0건으로 만들었다. `scripts/01_build_persona.py`/`match_agents()` 재실행, source cohort 재선발, depth ratio 재추첨은 수행하지 않았다.
- [ ] `persona_repair_manifest.json`에 pre/post DB hash, mismatch `60→0`, depth 변경 agent=0, non-prompt structured-field 변경 agent=0, renderer hash, ordered `(agent_id,news_depth,prompt_sha256)` hash가 있다.
- [ ] `agent_id → news_depth` manifest가 유일한 assignment source이고 prompt는 deterministic projection이다. prompt depth 문장 하나 변조, 두 agent depth swap, DB/prompt map drift fixture가 모두 FAIL한다.
- [ ] current DB `30/55/15`와 config/persona validation report의 legacy `15/55/30` 불일치를 해소했다. report는 config default가 아니라 frozen cohort registry에서 다시 생성한다.
- [ ] persona prompt renderer는 canonical parse/round-trip, Unicode NFC, LF-only, trailing LF, agent별 hash를 만족한다.
- [ ] 두 arm의 agent별 persona prompt bytes/hash가 동일하다.

### P0-C2. community policy

- [ ] Depth 0=30은 post/selective-read/reaction 0이면서, non-empty Best **최대 5개**의 full body를 다음 AM에 받는다.
- [ ] Depth 1+2=70만 posting/selective-read/reaction 가능하다.
- [ ] 각 거래일 PM 뒤 community/no-op phase가 정확히 1개 있다. 마지막 거래일 PM phase도 실행·기록하며 next-AM Best broadcast/exposure는 0이다. 이때 non-empty Best의 schedule만 `right_censored`이고 Best가 없으면 status는 `empty`다. 같은 PM의 eligible D1/D2 선택 열람 exposure는 허용된다.
- [ ] Best는 게시글이 0개면 0개, 1~4개면 있는 글 전부, 5개 이상이면 상위 5개다. 5개를 채우는 forced posting이 없다.
- [ ] Best payload에는 `post_id`, rank, score, title, full original body, content hash, version이 있다.
- [ ] `community_exposure_trace`는 reader, event, channel, body hash, actual delivered time, interpretation claim IDs를 기록한다.
- [ ] 각 interpretation claim의 source ID는 그 reader/event에 실제 전달된 source여야 하고, `supporting_quote`는 그 source에서 실제 전달된 title/body의 정확한 부분 문자열이다.
- [ ] `claim_text`의 의미가 인용문에 함의되는지와 게시글·claim의 진실성은 server validator의 거부 조건이 아니다. 의미·신뢰도 판단은 agent에게 맡기고, server는 visibility·ownership·exact quote provenance·privacy만 검사한다.
- [ ] Depth 2 public profile에는 actual portfolio, recent trade, private belief, stable agent ID가 들어가지 않는다.
- [ ] post/news 원문과 system/task instruction을 role/serializer/schema boundary로 분리한다.

---

## D. Memory·거래·수수료

### P0-D1. STB/LTB timing and schema

- [ ] existing six dimensions `dim_1~dim_6`의 이름·의미를 유지한다.
- [ ] `STB_t`는 current news + 실제 노출·출처가 연결된 current community claim만 사용한다.
- [ ] analysis/decision은 full `LTB_(t-1).dim_1~dim_6`과 full `STB_t.dim_1~dim_6`을 분리 block으로 받는다.
- [ ] historical order/fill, `belief_summary`, raw news/community body, `view_change`가 analysis/decision direct input에 없다.
- [ ] Decision-Making Process는 `LTB_(t-1)+STB_t+current execution state`로 `decision_t`를 만들고, exchange가 실제 `fill_t`를 확정한다.
- [ ] current fill은 same-turn STB/analysis/decision에는 없고, post-fill `LTB_t`에는 committed decision/fill episode로 정확히 한 번 있다.
- [ ] `LTB_t`는 `previous LTB + current STB + current decision/fill episode + eligible earlier price outcomes`를 재귀 통합해 한 번 새로 작성되고 다음 decision event부터만 보인다.
- [ ] `belief_summary`는 never agent-visible; `view_change`는 post-writing private context만 허용한다.
- [ ] post-writing private context는 `LTB_t 6D + deterministic view_change + committed PM fill_t`만 쓴다.
- [ ] agent·arm별 committed `STB=U`, LTB transitions=`U`, LTB states=`U+1`이고 exact key set이 같다.

### P0-D2. fill episode와 no-leakage

- [ ] `RN_COMM_OFF/paper_run.sqlite#paper_fill_ledger`와 `RN_COMM_ON/paper_run.sqlite#paper_fill_ledger`는 각각 append-only final fill ledger이며 `fill_id`, event ID, source LTB/STB/decision ID, requested/filled quantity, price, pre/post portfolio를 보유한다.
- [ ] `traces/rn_comm_off_final_fill_ledger.csv`와 `traces/rn_comm_on_final_fill_ledger.csv`는 각 arm `paper_run.sqlite#paper_fill_ledger`의 deterministic canonical export이고, `traces/final_fill_export_index.json`의 path·SHA-256·row count 및 DB와 one-to-one reconciliation이 모두 PASS다.
- [ ] current fill transaction fact는 체결 직후 같은 turn의 post-fill LTB updater에서 한 번만 소비된다. 이는 outcome 평가가 아니라 actual side/quantity/price/pre-post portfolio의 structured episode다.
- [ ] 각 `(fill_id, horizon)`의 `next-turn`, H1, H5는 due observation event에서 한 번만 소비된다.
- [ ] due event 이전 또는 미래 outcome이 LTB input에 있으면 `FAIL`이다.
- [ ] price feedback은 LTB `dim_6`만 직접 바꾸며 `dim_1~dim_5`는 새 news/community evidence 없이는 변경 불가다.

### P0-D3. fee-free policy

- [ ] `config`, resolved manifest, exchange, portfolio, PnL, trade ledger, CSV export, report가 모두 `commission_rate=0`, `sell_tax_rate=0`, `fee_amount=0`을 기록한다.
- [ ] 모든 BUY/SELL fill의 `fee_amount == 0`이다.
- [ ] max buy quantity와 cash debit은 fee-free rule로 일치한다.
- [ ] 누적 PnL/portfolio와 fill ledger 간 fee drift가 없다.

---

## E. LLM·reasoning off·retry

### P0-E1. strict reasoning-off

- [ ] 모든 physical HTTP request에 `reasoning: {"effort":"none","exclude":true}`가 final request body에 존재한다.
- [ ] provider `only/order`가 pinned provider 하나이며 fallback=false, require_parameters=true다.
- [ ] stage별 live canary가 requested/returned model, provider, empty reasoning fields, `reasoning_tokens=0`, schema pass를 모두 증명했다.
- [ ] telemetry 누락/nonzero reasoning token/provider mismatch/online fallback/offline stub은 `FAIL`이다.
- [ ] `TWINMARKET_OFFLINE_LLM` 또는 동등한 stub이 paper path에서는 API 호출 전 거부된다.

### P0-E2. retry/journal/commit

- [ ] logical call ID와 physical attempt ID를 분리한다.
- [ ] response journal에는 `phase_attempt_id`, request hash, response hash, validation state, committed/rejected/rolled_back status가 있다.
- [ ] rollback된 phase의 API call이 최종 scientific count/cost/reasoning proof에 섞이지 않는다.
- [ ] same logical key의 different payload는 pause하고, identical payload만 idempotent replay한다.
- [ ] failpoint at STB, decision, fill, LTB, community, WAL flush에서 DB/log/trace가 atomic하게 rollback 또는 commit된다.

---

## F. 모듈성·하드코딩·lifecycle

### P0-F1. module contract

- [ ] `StudySpec → resolved manifest → RunContext → event scheduler → typed stage result → atomic commit → evaluator/report` 흐름이 문서와 code contract에서 일치한다.
- [ ] paper path는 global `config.py`로 scientific setting을 읽지 않고 resolved manifest/RunContext를 dependency로 받는다.
- [ ] `simulation`은 orchestration, daily cycle은 stage composition, exchange는 deterministic execution, logger는 export adapter 역할로 분리한다.
- [ ] LLM task가 DB를 직접 부분 write하지 않고 validated staged result를 반환한다.
- [ ] raw dict/free-text context boundary에는 exact schema/additionalProperties rejection이 있다.

### P0-F2. hidden hardcode sweep

- [ ] `30`, `50`, `45`, `63`, `90`, `126`, `*2`, `2d-1`, `skip=5`, fixed date, legacy condition, global run path가 paper execution path에 없다.
- [ ] current-study assertions(100명, 30/55/15, current D/B/E, Best K=5, fee=0)은 manifest에만 있고 resolver output으로 검증된다.
- [ ] AM/PM은 fixed arithmetic가 아니라 ordered `DecisionEvent` registry로 실행한다.
- [ ] retry/concurrency/seed policy는 module별 default가 아니라 one `CallPolicy`/manifest section으로 고정된다.
- [ ] seed key는 study seed, agent ID, event ID, stage, validation attempt로 파생된다.

### P0-F3. DB/log lifecycle

- [ ] run-scoped clean base가 schema version, allowed table row count, LTB0/portfolio count, SHA-256을 통과한다.
- [ ] 0-byte `outputs/experiment_base_sim.db`나 stale base를 paper run에 쓰지 않는다.
- [ ] init-memory, turn-0 belief, launcher, runtime, validator, report가 모두 explicit sealed persona snapshot을 읽고 global `outputs/sys_100.db` fallback은 paper mode에서 FAIL이다.
- [ ] 새 STB/LTB/fill/outcome/exposure table은 schema migration, reset, clean-base builder, checkpoint snapshot/rollback, master merge, integrity digest, archive registry에 모두 등록됐다.
- [ ] checkpoint의 static file list가 아니라 artifact registry가 모든 sidecar를 merge/verify한다.
- [ ] resumed run과 uninterrupted run이 same event key/digest/artifact hash를 만든다.

---

## G. Validator·report·handoff

### P0-G1. evaluator

- [ ] raw target, canonical final fill ledger, resolved `D/B/E` sets의 exact equality를 검사한다.
- [ ] actual∩simulation intersection, malformed→0, non-buy→sell, default skip=5가 없다.
- [ ] AM=open, PM=close, action/status/quantity/price/stock code/full-fill, `N×|Q_d|` fill-key set을 fail-closed로 검사한다.
- [ ] primary는 daily AM+PM gross signed fill value 대 삼성전자 `Individuals` final daily direction이다.
- [ ] AM-only/PM-only는 exploratory이며 intraday 개인수급 target이라고 주장하지 않는다.
- [ ] integrity PASS 후에만 evaluator와 report가 생성된다.

### P0-G2. report

- [ ] report는 `resolved_study_manifest.json`, `RUN_RECORD.md`, frozen target, canonical ledger, traces만 읽는다.
- [ ] global `outputs/sys_100.db`, `outputs/logs/current`, legacy PDF/CSV, glob latest-run fallback은 hard-fail이다.
- [ ] `RUN_RECORD.md`에는 input/output artifact path, schema version, row count, SHA-256, validator/report gate status가 있다.
- [ ] report에는 run integrity, real-news coverage, reasoning-off, daily direction metrics, memory lineage, community mechanism, fee-free diagnostics, wealth sensitivity가 분리되어 있다.
- [ ] like/post count만으로 community가 거래를 바꿨다고 서술하지 않는다.

---

## H. Canary와 본 실행 승인

### P1. live canary

- [ ] canary는 본 cohort 100명을 사용한다.
- [ ] canary event registry와 news bundle은 full run과 같은 format/hash policy를 쓴다.
- [ ] RN pair 모두 실행하고, same frozen input / only community diff를 확인한다.
- [ ] STB/LTB/fill/outcome/community boundary, Best full body, D0 passive exposure, strict-off, fee=0, resume failpoint를 관찰한다.
- [ ] p50/p95/p99 latency, token/cost, retry, disk/WAL, trace volume을 stage별 기록한다.
- [ ] canary 결과로 prompt·schema·policy가 바뀌면 new manifest version으로 두 arm canary를 다시 실행한다.

### P2. final freeze

- [ ] code tree, prompt tree, dependency lock, model/provider metadata, cohort, base DB, calendar, news bundle, target, policies의 hash를 freeze했다.
- [ ] budget cap, time cap, disk cap, pause threshold, supervisor/restart policy가 freeze됐다.
- [ ] required approvals와 reviewer signatures가 `RUN_RECORD.md`/approval log에 있다.
- [ ] `git diff`, run root, external inputs를 archive하고 read-only snapshot을 만들었다.

### 최종 승인 record

```text
Study ID:
Run pair IDs:
Resolved manifest SHA-256:
Code/prompt/dependency SHA-256:
Cohort/calendar/news/target/base SHA-256:
P0 result: PASS / PASS_WITH_NEWS_SHORTAGE / FAIL
P1 canary result: PASS / FAIL
P2 freeze result: PASS / FAIL
Approver / timestamp:
Blocking issue IDs (must be none):
Non-blocking news shortage exception IDs/hashes (or none):
```

## Appendix: future fake-news run delta

Fake-news run은 baseline P0/P1/P2가 통과한 뒤에만 시작한다. baseline 대비 추가 검증은 다음뿐이다.

- [ ] fake study도 real news는 event별 10개를 우선 목표로 하고, accepted shortage는 baseline과 같은 actual real bundle/count를 유지한다. fake는 **추가 1개**다.
- [ ] fake registry에 factual anchor, direction (`bullish`/`bearish`), injection event, private stimulus ID, content hash, approval, leakage review가 있다.
- [ ] agent-visible payload에는 fake label, synthetic ID, generation prompt, direction metadata가 없다.
- [ ] bullish/bearish는 같은 anchor/event schedule을 pair로 하고 방향성만 반대다.
- [ ] direct feed exposure, Depth 2 search re-exposure, community propagation을 각각 별도 trace로 기록한다.
- [ ] fake run report는 actual human direction prediction claim이 아니라 simulated-world treatment contrast로 해석한다.
