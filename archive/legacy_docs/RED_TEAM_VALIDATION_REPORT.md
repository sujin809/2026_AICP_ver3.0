# RN Community AB 적대적 검증 보고서

> 보존 구분: 과거 검증 결과. 현재 구현·실행 판정 정본이 아니다.

검증일: 2026-07-23 KST  
대상: Samsung `005930` 실제뉴스 `RN_COMM_OFF` / `RN_COMM_ON` 100-agent 연구 경로  
기준 코드: `samsung-baseline-0720` / `8604f9aec041c9929e327a90cc9025b650e9fab6`  
판정: **구현과 무과금 로컬 검증은 통과. 실제 유료 100-agent 실행은 아직 NO-GO.**

## 검증 범위와 결론

이번 검증은 네 설계 문서의 P0 계약을 구현 코드와 fixture로 대조했다. 네트워크와 유료 API는 호출하지 않았다. 따라서 아래의 `PASS`는 local fixture·sealed-input 검증·mock stage의 결과이며, 실뉴스 100명 본실행 결과나 live OpenRouter canary를 뜻하지 않는다.

현재 RN 전용 paired runner와 execution factory는 존재한다. 기존 legacy runner를 붙이지 않고, sealed input/source·STB/LTB·journal·community lifecycle 계약을 따르는 새 경로만 허용한다. paid factory는 실제 sealed StudySpec 입력과 live canary 증거가 없으면 의도적으로 runner 생성을 거부한다.

## 최신 무과금 회귀 결과

실행 명령:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3.12 -m py_compile \
  config.py twinmarket_kr/rn_model_pin.py twinmarket_kr/llm/client.py \
  twinmarket_kr/rn_ab/*.py validation/validate_realnews_community_ab.py \
  scripts/09_run_realnews_community_ab.py scripts/10_prepare_rn_ab_source_personas.py

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /opt/homebrew/bin/python3.12 -m unittest discover -s tests -q
```

결과: **314/314 PASS** (`Ran 314 tests in 69.908s, OK`). 모든 테스트는 local SQLite, fixture, fake stage model만 사용했다. OpenRouter·외부 네트워크·유료 API 호출은 0회다.

## 적대적 검증 lane

| lane | 공격 또는 실패 주입 | 확인한 방어 |
|---|---|---|
| baseline·source provenance | baseline HEAD 불일치, sealed source snapshot hash 변조 | preflight/run-context가 거부하고 execution factory가 source byte drift 시 중단 |
| input sealing | `current`/symlink root, runtime input copy 변조, calendar/news/review hash 재봉인 | 명시 input root·run-local copy·exact hash/calendar coverage만 허용 |
| fake/leakage | fake marker·invisible Unicode·미래 snapshot·masked article | fake/target leakage 및 unsafe snapshot을 fail-closed로 거부 |
| strict model/reasoning | 다른 모델, `effort` 변경, hide-only setting, offline stub | RN model pin과 final request body의 `reasoning: {"effort":"none","exclude":true}`를 HTTP 전 강제 |
| JSON/parser | top-level·nested duplicate key, `NaN`/`Infinity`, array, trailing prose/fence, unknown key | strict decoder와 exact key set이 거부하고, durable journal도 canonical JSON/hash를 재검증 |
| prompt rendering | payload 안의 `{...}`·sentinel, authoring malformed/doubled brace | runtime text는 데이터로 한 번만 삽입하고, 허용되지 않은 source token은 prompt load 전에 거부 |
| request shape | `max_tokens`/temperature/JSON format drift, `finish_reason=length` | token budget·`temperature=0.2`·`response_format=json_object`를 request hash와 physical call에 고정하고 정상 종료 외 응답을 거부 |
| API audit mixing | 전역 audit log 또는 다른 arm/run 행으로 reasoning-off 증명 | arm별 run-local `openrouter_attempts.jsonl`에 run ID/arm/manifest를 기록하고 exact context만 검증 |
| journal/restart | accepted response 후 rollback, 두 번째 arm journal commit 실패, 같은 phase 동시 재진입 | rollback revival 거부, restart에서 journal commit만 완료, event loop를 막지 않고 기존 결과 재사용 |
| 100-worker scheduling | 2 arm × 100 agent mock composite turn, post-fill LTB 실패 | arm당 worker cap 유지, 양 arm DB rollback, 800 accepted response replay에 추가 모델 호출 0 |
| STB/LTB lineage | forged evidence, missing stage consumption/edge, future outcome 혼입 | four journaled stages의 response digest·logical ID와 evidence edge를 same-transaction 기록하고 final gate에서 exact set 검증 |
| prompt contract/semantic drift | legacy 6D 의미·cap 누락, LTB outcome ID 지시 충돌, STB evidence polarity 이동, analysis 입력 block 누락 | versioned output contract를 prompt payload에 주입하고, 6D/cap·due outcome·four analysis source·same-polarity evidence를 local fixture로 검증 |
| community timing | invented/missing/final-PM phase, D0 write/read/reaction, partial interpretation failure, restart | 모든 PM 뒤 phase 실행, 마지막 PM의 next-AM Best broadcast/exposure는 0이고 non-empty Best만 `right_censored`·empty board는 `empty`; 같은 PM의 eligible D1/D2 선택 열람은 허용, 그 외 PM board→다음 AM Best full-body 전달, D0 posting/selective-read/reaction 0, partial claim commit 0, durable reload |
| final fill/evaluator | missing/extra/duplicate fill, wrong price/stock/status/fee, target/contract drift | canonical fill exact key set·fee 0·manifest/price/target binding을 거부하고 immutable export index 생성 |
| wealth robustness | initial cash map 변조, rich 한 명 제외 시 RQ2 방향 전환 | agent별 fixed initial cash map을 evaluator contract에 봉인하고 `wealth_fragile` 사유를 출력 |

## 구현된 핵심 계약

### 1. 100명 동시성·재시작

- [`runner.py`](../../twinmarket_kr/rn_ab/runner.py)는 event 전체를 두 arm에 대해 함께 조율한다. 한 arm의 worker cap은 sealed call policy와 같아야 하며, 두 arm 사이 semaphore를 공유하지 않는다.
- [`phase_runner.py`](../../twinmarket_kr/rn_ab/phase_runner.py)는 두 arm DB snapshot, durable `commit_decided` checkpoint, journal replay를 사용한다. 둘째 arm의 journal commit 직후 실패하면 DB를 되돌려 journal과 DB를 분리하지 않고, restart가 model call 없이 남은 journal commit을 끝낸다.
- [`test_rn_ab_runner_concurrency.py`](../../tests/test_rn_ab_runner_concurrency.py)는 100명/arm의 five-stage mock workflow와 injected failure/retry를 검증한다. 이는 실제 유료 모델 성능 검증이 아니라 scheduler·atomicity·replay 검증이다.

### 2. STB/LTB와 FUSE-inspired memory

- [`stage_adapter.py`](../../twinmarket_kr/rn_ab/stage_adapter.py)와 [`memory.py`](../../twinmarket_kr/rn_ab/memory.py)는 STB, analysis, decision, post-fill LTB 각각의 journal logical call과 response digest를 scientific artifact와 같은 SQLite transaction에 기록한다.
- `memory_evidence_edges`에는 STB의 current evidence와 LTB의 parent LTB/current STB/decision/fill/due outcome edge가 기록된다.
- `assert_complete_lineage()`는 stage별 call-consumption, canonical edge set, outcome 소유자/성숙 시점, current-fill과 later-outcome의 분리를 확인한다. metadata 없이 직접 store API를 부르면 final lineage gate를 통과할 수 없다.

### 3. 기존 prompt 계승과 RN 계층 계약

- [`PROMPT_LINEAGE.md`](../../prompts/PROMPT_LINEAGE.md)는 최상위 production 프롬프트 묶음이 legacy `update_belief`, `market_analysis`, `make_decision`에서 계승한 판단 원칙과 입력 경계만 바꾼 내용을 표로 고정한다. 뉴스가 실제인지 가짜인지와 community 조건은 prompt가 아니라 입력 묶음·실험 설정에서만 결정한다.
- 최상위 `prompts/`에 RN production bundle 11개를 통합했다. `initial_belief`와 세 news 역할의 4개는 0720 원문과 byte-identical이다. `posting_decision`은 conditional 예시를 유효한 strict JSON으로, `community_reading`은 문자열 post ID와 public-only 읽기 경계만, `community_thinking`은 다음 AM claim JSON 계약만 최소 변경했다. STB·market analysis·decision은 기존 골격을 보존한 계약상 최소 수정이며, post-fill LTB만 새 역할이다. `update_belief.txt`는 legacy 호환용으로 남지만 RN bundle hash에는 포함하지 않는다. `rn-trusted-system-v2`는 exact source 복사를 quote field에만 요구하고, 다른 interpretation field의 의미·신뢰도 판단은 agent에게 맡긴다.
- [`belief_contract.py`](../../twinmarket_kr/rn_ab/belief_contract.py)는 0720 Samsung baseline의 정확한 6D 한도(`dim_1=150`, `dim_2~dim_6=100`)를 단일 원천으로 둔다. StudySpec·stage adapter·persistence가 모두 이 값 이외를 호출 전 거부한다.
- STB는 current-only이며 이전 Belief·portfolio·fill을 받지 않는다. LTB 모델 출력은 여섯 차원과 `integration_evidence`뿐이다. `view_change`는 사라지는 것이 아니라 post-fill LTB commit 뒤 서버가 이전/새 6D의 before/after SHA-256와 integration evidence를 담은 결정론적 구조체로 만들고, 별도 human-log hash로 LTB·trace에 저장·재구성한다. 이 human field는 STB/LTB/analysis/decision 입력·출력에 재주입되지 않는다.
- analysis는 previous LTB/current STB/market/execution-state 네 block을 모두 reference해야 하며, `uncertain` stance를 기록할 수 있다. 새 `rn_ab_v9` schema는 empty old DB만 additive upgrade하고 populated old scientific DB는 조용히 relabel하지 않고 새 run DB를 요구한다.

### 4. 모델과 reasoning-off

- RN 허용 모델은 [`rn_model_pin.py`](../../twinmarket_kr/rn_model_pin.py)의 `qwen/qwen3.5-flash-02-23` 하나다. `.env`의 `OPENROUTER_MODEL`은 RN model selector가 아니다.
- [`client.py`](../../twinmarket_kr/llm/client.py)는 final `extra_body` 직전에 `reasoning.effort="none"`과 `exclude=true`를 강제한다. `exclude`만 있는 요청은 reasoning을 끈 것으로 처리하지 않는다. RN strict call은 `temperature=0.2`, `response_format={"type":"json_object"}`, positive stage token budget도 정확히 요구한다.
- [`execution.py`](../../twinmarket_kr/rn_ab/execution.py)는 live model에 arm별 audit path/context를 주고, `validate_run_local_reasoning_audits()`가 요청/반환 모델, provider, response reasoning field, `reasoning_tokens=0`, 정상 `finish_reason`, request shape를 fail-closed로 확인한다.
- 현재 사용·승인 계약은
  [`RUNBOOK_AND_PREFLIGHT.md`](../../RUNBOOK_AND_PREFLIGHT.md)를 따른다.

### 5. 평가 산식과 wealth sensitivity

RQ1의 일별 simulated signed fill value는 다음처럼 계산한다.

```text
BUY:  + filled_quantity × actual_fill_price
SELL: - filled_quantity × actual_fill_price
date value: 모든 100 agent의 AM + PM actual fill value 합
```

그 날짜 합의 부호만 `005930`의 실제 `Individuals` 최종 순거래대금 부호와 비교한다. 0은 `FLAT`이며 primary direction match로 조용히 바꾸지 않는다. AM-only/PM-only는 보조 진단이다.

RQ2는 agent별로 AM+PM signed notional을 먼저 합산하고, **각 agent의 고정 initial cash**로 나눈 뒤 100명을 동일가중 평균하고 마지막에 `ON − OFF`를 계산한다. evaluator는 다음을 함께 출력한다.

- raw 100명
- 1억-only와 같은 90명 rich-excluded alias (byte-identical 검증)
- 10억-only
- initial-capital-normalized equal-agent mean
- 10억 agent 한 명씩 제외한 10개 leave-one-rich-out
- P3-B 가능 여부와 RQ2 mean sign/zero transition을 근거로 한 `wealth_fragile`

초기자본 map은 evaluator contract v2 및 authoritative resolved manifest에서 exact equality를 요구한다. 독립 news-leakage proof가 evaluator 입력에 없는 경우 `core_p3b_pass`/`robust_p3b_pass`를 임의로 true로 만들지 않고 `None`/명시 상태로 둔다.

## 현 시점의 OPEN NO-GO

아래는 코드가 없어서가 아니라, 실제 연구 입력 또는 live 증거가 아직 없어서 paid run을 막는 조건이다. 수동 hash 계산·서명·승인 절차는 gate가 아니다.

1. **RN용 sealed StudySpec artifact 부재 및 뉴스 as-of provenance 결손** — 원천 데이터는 작업공간에 있다: 100명 DB, 347거래일 가격, 6,900개 daily selection/19,542개 processed news, 개인 순매수 target이다. 다만 이들을 RN schema의 calendar·price·cohort·stage-input·clean-news·known-injection·target registry와 StudySpec으로 변환·봉인한 파일은 아직 없고, 원천/processed 뉴스에는 immutable `observed_at`·version hash·`last_modified_at`이 없다. 특히 2026-04-27 09:11 기사가 당일 종가/최종 수급을 포함한 확정 leakage가 있어, 기존 CSV를 그대로 자동 bundle로 만들 수 없다. 코드는 임의 provenance를 만들지 않으며, 안전한 source version/명시 mask가 확보된 뒤에만 이를 hash-pin한다.
2. **live reasoning-off telemetry 부재** — 실제 provider 요청/반환 model, provider, empty reasoning field, `reasoning_tokens=0`, `finish_reason="stop"`, sealed temperature/JSON response format이 각 arm run-local audit에 남은 증거가 없다. 코드는 이를 기록·검증하지만, 유료 호출 금지 지시에 따라 아직 호출하지 않아 gate는 열리지 않는다.
3. **최종 P1·handoff 증거 부재** — scheduler의 100-worker/restart proof는 무과금 mock stage로 검증했다. 별도 2거래일·U=4·100명·community boundary P1과 그 결과물(`RUN_FINALIZATION.json`, canonical two-arm fill CSV, evaluator index, run-local audit)은 아직 실제로 실행되지 않았다.

이미 해소된 항목은 다음이다. public-author-profile registry, community public-claim/ledger-boundary policy, D2 recent-search registry, journalled production-ready community provider, 11개 prompt bundle v2의 hash-pinning은 모두 run preparation/실행 경로에 연결됐다. Community validator는 게시글·claim의 진실성이나 `claim_text`와 인용문의 의미적 함의를 판정하지 않고, 실제 visible source ownership·exact `supporting_quote` provenance·privacy만 검사한다.

따라서 지금 가능한 정확한 표현은 다음이다: **RN 전용 구현·자동 준비물·적대 fixture는 준비됐지만, 유료 API를 쓰는 실제 연구 실행은 실제 StudySpec 입력과 live telemetry/P1 증거가 생긴 뒤에만 GO가 될 수 있다.**

## 변경 관리

- source/dependency byte hash는 preflight 때 [`source_hashes.json`](../../twinmarket_kr/rn_ab/provenance.py)에, common의 11개 prompt byte hash와 역할 구분은 bundle v2 manifest에 자동 봉인된다. STB/analysis/decision/LTB 네 개는 매 event 실행되고, posting/reading/thinking 세 community role은 해당 phase에서 journalled provider가 조건부 실행한다. initial belief와 세 news compatibility prompt는 0720 계승·재현성 검토를 위해 함께 봉인하지만 RN 설계상 별도 모델 호출로 실행하지 않는다. execution factory는 현재 source/dependency tree가 다르면 시작 전 거부한다.
- 모델, provider, reasoning, cohort, calendar, news, initial capital, concurrency를 바꾸면 `.env` 수정이나 legacy resume이 아니라 새 StudySpec/run bundle과 paired run을 만들어야 한다.
- `outputs/runtime_dbs/*`의 기존 사용자 runtime 파일은 이번 검증에서 수정·삭제하지 않았다.
