# RN reasoning-off telemetry 및 final handoff 준비

이 문서는 유료 API를 호출하지 않은 현재 상태와, 이후 실제 live 증거가 생겼을 때 통과해야 하는 자동 검증 경계를 구분한다.

## 현재 상태

- `REASONING_OFF_TELEMETRY_PLAN.json`은 sealed `RNRunContext`에서 자동 생성한다.
- 계획 생성과 검증은 네트워크 요청 0회, 유료 API 호출 0회다.
- 계획에는 `execution_authorized=false`가 고정된다.
- 실제 OpenRouter telemetry가 없으므로 현재 상태는 `NO_GO_LIVE_TELEMETRY_MISSING`이다.
- 승인이나 telemetry를 나타내는 JSON을 임의로 만들지 않는다.

## 오프라인 CLI

아래 명령에는 API 실행 기능이 없다.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3.12 twinmarket_kr/rn_ab/execution.py \
  --run-dir <SEALED_RUN_DIR> \
  --prepare-canary-plan
```

출력 상태는 `PREPARED_NO_NETWORK_NO_PAID_API`이고 `execution_ready=false`다.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3.12 twinmarket_kr/rn_ab/execution.py \
  --run-dir <SEALED_RUN_DIR> \
  --validate-canary-plan
```

이 명령은 현재 source hash와 sealed run을 다시 대조하고 계획을 재계산한다. 파일 내용이 한 필드라도 다르면 실패한다.

실제 live 실행이 별도로 수행되어 양 arm의 run-local JSONL이 존재할 때만 다음 읽기 전용 검증을 사용할 수 있다.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3.12 twinmarket_kr/rn_ab/execution.py \
  --run-dir <SEALED_RUN_DIR> \
  --capture-live-canary-evidence
```

이 명령도 API를 호출하지 않는다. 이미 존재하는 실제 telemetry를 검증해 `REASONING_OFF_TELEMETRY_EVIDENCE.json`으로 봉인할 뿐이다. JSONL이 없거나 일부만 있거나, 테스트용으로 꾸민 최소 행이면 `NO_GO`로 끝난다.

## reasoning-off telemetry 계획의 범위

현재 자동 계획은 첫 decision event에서 다음을 검사하기 위한 telemetry 전용 계획이다.

- sealed 전체 cohort
- `RN_COMM_OFF`, `RN_COMM_ON` 양 arm
- STB, analysis, decision, post-fill LTB 네 model stage
- 요청/반환 model과 provider 일치
- `reasoning={"effort":"none","exclude":true}`
- `reasoning_tokens=0`, reasoning field 없음
- `finish_reason="stop"`
- `temperature=0.2`, JSON object response format
- stage별 고정 max token
- provider request ID와 structured usage 존재
- 예상 logical call마다 성공한 physical request 한 건

100명 study라면 예상 요청은 `100명 × 2 arm × 4 model stage = 800`건이다. 이 수치는 실행 예산 승인이 아니라 검증 대상의 크기다. 현재 명령들은 이 800건을 실행하지 않는다.

감사 행은 client가 기록하는 exact JSONL schema를 그대로 요구한다. 누락 필드와 추가 필드, 중복 logical call, 전역 audit log, 다른 run/arm의 행, 잘린 응답, provider/model drift, reasoning token 누락 또는 0이 아닌 값은 모두 실패한다.

## final P1 canary와의 구분

위 첫-event 계획은 최종 P1 canary가 아니다. 최종 P1은 별도의 resolved spec과 증거 계약으로 다음을 만족해야 한다.

- 2 trading days, `U=4`
- 본 cohort 100명
- 양 arm
- community prepare/read/post/next-event boundary
- 같은 sealed input과 community mode만 다른 pair
- resume/failpoint, fill/outcome, fee 0, D0/D1/D2 경계

따라서 reasoning-off telemetry가 통과해도 `sealed_two_trading_day_p1_canary_spec_and_validated_community_boundary` gate는 자동으로 열리지 않는다. 임의의 `P1_CANARY_EVIDENCE.json` 파일이나 boolean 값도 증거로 인정하지 않는다.

## finalization handoff

`inspect_final_handoff_readiness(run_dir, p1_canary_run_dir=...)`는 다음을 별도로 보고한다.

- reasoning-off live telemetry evidence 검증 여부
- complete run-local reasoning audit 검증 여부
- 로컬 `RUN_FINALIZATION.json` 존재와 run/manifest 결합 여부
- 별도 2-day P1 community-boundary evidence 검증 여부

로컬 CSV와 `RUN_FINALIZATION.json`이 만들어졌다는 사실만으로 paper GO 또는 evaluator handoff GO가 되지 않는다. 위 증거 중 하나라도 없으면 상태는 `NO_GO`다.

## 남은 live-only 작업

현재 준비 코드가 대신 만들 수 없는 것은 실제 provider가 반환한 telemetry와 P1 결과다. 유료 호출 금지 조건 때문에 본 작업에서는 이를 생성하지 않았다. 이후 사용자가 실제 paid 실행을 명시적으로 승인한 경우에만 아래 전용 운영 명령을 사용한다.

## 전용 run/resume/finalize CLI

`scripts/09_run_realnews_community_ab.py`는 계속 preflight 전용이다. 유료 실행은 별도 `scripts/12_operate_realnews_community_ab.py`에서만 가능하다.

읽기 전용 또는 무과금 명령:

```bash
python3.12 scripts/12_operate_realnews_community_ab.py prepare-telemetry \
  --run-dir <RUN_DIR>

python3.12 scripts/12_operate_realnews_community_ab.py status \
  --run-dir <RUN_DIR>

python3.12 scripts/12_operate_realnews_community_ab.py check-p1 \
  --run-dir <FULL_RUN_DIR> \
  --p1-run-dir <COMPLETED_P1_RUN_DIR>

python3.12 scripts/12_operate_realnews_community_ab.py finalize \
  --run-dir <RUN_DIR>
```

유료 명령은 `--authorize-paid-api-calls`와 sealed run ID의 정확한 재입력을 동시에 요구한다.

```bash
python3.12 scripts/12_operate_realnews_community_ab.py telemetry \
  --run-dir <P1_RUN_DIR> \
  --authorize-paid-api-calls \
  --confirm-run-id <EXACT_P1_RUN_ID>

python3.12 scripts/12_operate_realnews_community_ab.py run-p1 \
  --run-dir <P1_RUN_DIR> \
  --authorize-paid-api-calls \
  --confirm-run-id <EXACT_P1_RUN_ID>
```

P1은 실제 복구 연습을 요구한다. controlled canary 중 실제 phase가 paused/interrupted된 checkpoint에서만 `resume-p1`을 허용하며, 성공하면 `P1_RECOVERY_EVIDENCE.json`이 이전 checkpoint와 최종 checkpoint에 결합된다. 단순히 이미 끝난 P1에 파일을 만들어 넣는 방식은 통과하지 않는다.

```bash
python3.12 scripts/12_operate_realnews_community_ab.py resume-p1 \
  --run-dir <P1_RUN_DIR> \
  --authorize-paid-api-calls \
  --confirm-run-id <EXACT_P1_RUN_ID>
```

P1을 완료하고 `finalize`한 뒤 본 실행을 시작한다.

```bash
python3.12 scripts/12_operate_realnews_community_ab.py run \
  --run-dir <FULL_RUN_DIR> \
  --p1-run-dir <COMPLETED_P1_RUN_DIR> \
  --authorize-paid-api-calls \
  --confirm-run-id <EXACT_FULL_RUN_ID>
```

오류로 `PAUSED_SAFE_TO_RESUME`가 반환된 경우 동일 P1 증거를 명시해 `resume`한다. checkpoint가 없거나 이미 완료된 run은 resume할 수 없다.

## P1 검증이 실제로 다시 읽는 것

P1 gate는 임의의 `P1_CANARY_EVIDENCE.json`을 읽지 않는다. 별도 P1 run directory를 `RNRunContext`로 다시 열어 다음을 직접 검증한다.

- 정확히 100명, 2거래일, AM/PM 4 event
- full run과 동일 cohort 순서, persona, prompt, model/reasoning, memory·community·trade·retry/runtime policy
- 양 arm complete reasoning audit, journal↔phase consumption, complete lineage와 finalization
- 완결된 paired phase checkpoint와 실제 recovery evidence
- OFF community exposure 0 및 PM no-op
- 첫날 PM의 non-empty Best full body가 100명 모두에게 둘째 날 AM에만 전달됨
- Depth 0의 Best-only passive exposure와 selective-read 0

## private community post trace handoff

최종화는 canonical SQLite `community_post_trace`를 읽어 `traces/community_post_trace.jsonl`을 결정적으로 만든다. `created_at`은 제외하고 `phase_id, author_agent_id` 순으로 canonical JSONL을 기록한다. 파일 SHA-256, 행 수, OFF=0/ON 행 수와 privacy 표시는 `final_fill_export_index.json`과 `RUN_FINALIZATION.json` 양쪽에 결합된다.

이 sidecar는 run-local private 감사 자료이며 공개 community payload나 에이전트 입력으로 재사용하지 않는다. 재검증 시 JSONL bytes를 SQLite table에서 다시 계산하므로 파일 변조는 handoff를 실패시킨다.

## community mechanism 최종 산출물

최종화는 같은 canonical SQLite와 committed response journal에서 다음 파일도 결정적으로 재생성한다.

- `community_interactions.csv`: D1/D2 독자가 실제로 본 title-only 후보 snapshot, 선택 여부, 반응. persona·portfolio·private belief·본문은 넣지 않는다.
- `community_best_posts.csv`: frozen public board에서 결정론적으로 선택된 Best 게시글과 next-AM visibility.
- `traces/community_exposure_trace.jsonl`: title/full-body exposure ID, prompt logical-call ID, claim ID, exact supporting-quote hash, STB evidence edge와 STB ID의 private 계보. 게시글 본문과 truth verdict는 넣지 않는다.

검증기는 파일 hash만 확인하지 않는다. 모든 resolved community phase에 대해 `board → reader trace → selected read → reaction/score → Best schedule → 전 cohort delivery 또는 right-censor → checkpoint → interpretation claim → STB edge`를 정확히 대조한다. OFF arm은 phase별 no-op checkpoint만 허용하며 community call, board, reader trace, exposure, claim은 하나라도 있으면 실패한다.

또한 committed auxiliary journal call은 정확히 하나의 semantic sink에 연결되어야 한다. 모든 eligible author의 posting call은 private post trace에, candidate가 있는 reader의 select call과 실제 선택이 있는 reader의 react call은 reader trace에, 실제 exposure가 있는 next-AM interpretation call은 claim observation에 연결된다. D0 selective call, 빈 candidate select, 빈 selection react, exposure 없는 interpretation, 누락·중복·추가 call은 모두 실패한다.

interpretation request는 해당 독자가 실제로 볼 수 있었던 candidate/Best/selected payload로 다시 구성해 journal의 canonical request와 exact 비교한다. claim의 사실 여부는 판정하거나 필터링하지 않는다. 대신 cited exposure가 그 독자와 event에 실제 전달되었는지, `supporting_quote`가 해당 노출 수준(title-only 또는 full-body)의 정확한 substring인지, 그 source ID가 STB evidence edge까지 이어지는지만 보존한다.
