# 0720 논문 실험 코드 인수인계

## 1. 목적과 실행 순서

이 수정은 기존 청크 실행에서 날짜 구간마다 belief·portfolio·turn이 초기화된 문제를 제거하고, 한 번의 논리적으로 연속된 실험을 AM·PM·community 단위로 안전하게 재개할 수 있게 만드는 작업이다.

실제 실험은 이 컴퓨터에서 실행하지 않는다. 코드를 GitHub에 반영한 뒤 OpenRouter가 설정된 다른 컴퓨터에서 다음 순서로 실행한다.

1. clean experiment base를 한 번 생성한다.
2. 오늘 밤 `c00_commoff_fakeoff`만 실행한다.
3. 내일 같은 base·코드·seed·30명 cohort를 사용해 나머지 5조건만 실행한다.
4. 완료된 여섯 조건의 비처치 입력 hash가 모두 같은지 자동 검증한다.

정확한 명령은 `EXPERIMENT_0720_RUNBOOK.md`에 있다.

논문 실행 entrypoint는 `scripts/08_run_six_conditions.py` 하나다. 기존 README의 `05_run_simulation.py`, `scripts/run_full_restart.sh`, 날짜별 `resume_*`는 과거 실행 경로이므로 사용하지 않는다. 이들은 현재 git 원본에 포함된 기존 파일이라 임의 삭제하지 않았으며, 특히 `run_full_restart.sh`에는 다른 컴퓨터의 절대경로가 남아 있다.

## 2. 고정된 논문 실행 설정

- 모델: `qwen/qwen3.5-flash-02-23`만 허용
- seed: 모든 조건에서 2
- 에이전트: `outputs/sys_100.db`에서 결정론적으로 선택한 동일 30명
- 의사결정: `buy_sell_only`; hold와 preferred-direction 단계 없음
- 조건 내부 준비 concurrency: 30
- 같은 컴퓨터의 모든 조건이 공유하는 OpenRouter 실제 동시 호출 상한: 16
- 하루: AM turn → PM turn → 장 종료 후 community
- 실제 가격은 외생적으로 고정하며 모든 정상 주문은 공시 시가/종가에 전량 체결

## 3. 핵심 변경과 이유

### 3.1 clean base와 조건별 독립 DB

관련 파일:

- `twinmarket_kr/experiment_runtime.py`
- `scripts/04_build_experiment_base.py`

`outputs/sim.db`를 조건별로 직접 공유하지 않는다. 새 base에는 StockData와 100명 전원의 turn 0 belief·portfolio만 남기고 아래 runtime 자료를 제거한다.

- turn 1 이상의 belief와 portfolio
- trade log와 TradingDetails
- community post·interaction·log
- 과거 오류 복구용 system message

base 검증은 중복 turn 0, 초기 belief 6개 차원·요약·변화 필드의 누락, 초기 보유주식, 현금·총자산 불일치, PnL 잔존, `sys_100.db`의 초기자금 불일치를 차단한다. 초기자금은 기존 persona 설계대로 1억원과 10억원 두 집단이며, 전원 무보유다.

각 조건은 이 base에서 별도 `runtime_sim.db`를 복사한다. 여섯 조건이 동시에 실행되어도 서로의 belief·portfolio·community 상태를 읽거나 쓰지 않는다.

### 3.2 청크를 상태 초기화가 아닌 로그·검증 단위로 변경

관련 파일:

- `scripts/05_run_simulation_checkpointed.py`
- `twinmarket_kr/simulation.py`
- `twinmarket_kr/run_integrity.py`

청크마다 DB를 초기화하던 흐름을 제거했다. global turn은 전체 기간에서 1부터 126까지 유지되며 다음 날짜는 직전 PM의 portfolio와 belief를 그대로 읽는다.

각 날짜는 다음 세 phase로 분리된다.

1. AM
2. PM
3. community

phase 시작 직전에 DB snapshot과 로그 파일 크기를 기록한다. 성공하면 phase state digest와 완료 키를 checkpoint에 기록한다. 실패하거나 프로세스가 종료되면 다음 실행에서 snapshot을 복구하고 부분 로그를 phase 시작 크기로 되돌린 뒤 같은 phase만 다시 실행한다.

DB backup은 `.tmp`에 완전히 복사하고 SQLite `quick_check`를 통과한 뒤 대상 파일로 원자적 교체한다. snapshot 복사 도중 전원이 꺼져도 미완성 파일이 정상 checkpoint로 오인되지 않는다.

phase 성공 후 임시 snapshot은 삭제한다. 19MB 전후의 DB를 189개 영구 보관해 조건당 수 GB를 소비하던 위험을 없애고, 실행 중에는 조건별로 rollback snapshot 하나만 유지한다.

### 3.3 부분 실패 중 늦은 병렬 쓰기 차단

관련 파일:

- `twinmarket_kr/simulation.py`

기존 `asyncio.gather`는 한 에이전트가 먼저 실패하면 다른 에이전트가 아직 DB·로그를 쓰는 중인데 상위 checkpoint 복구가 시작될 수 있었다. 이제 같은 병렬 묶음의 모든 task가 종료된 뒤 오류를 올린다. 따라서 rollback 뒤에 늦게 끝난 task가 belief나 trade를 다시 쓰지 못한다.

AM/PM의 결과 목록은 agent 입력 순서를 유지한다. 커뮤니티 posting LLM 호출은 병렬이지만 post 저장은 `agent_id` 정렬 순서로 수행한다. 커뮤니티 읽기는 모든 에이전트가 같은 frozen board를 본 뒤 reaction을 `agent_id` 순서로 반영한다. API 응답 도착 순서가 post ID·Best 5·다음 날 context를 바꾸지 않도록 한 것이다.

### 3.4 중복 실행 lock

관련 파일:

- `scripts/05_run_simulation_checkpointed.py`
- `scripts/08_run_six_conditions.py`
- `twinmarket_kr/simulation.py`

같은 output run directory, 같은 matrix root, 같은 runtime DB를 두 프로세스가 동시에 수정하지 못하도록 각각 파일 lock을 둔다. 실수로 같은 명령을 두 번 실행하면 두 번째 프로세스가 즉시 중단된다. 강제 종료 시 OS가 lock을 해제하며, checkpoint의 inflight phase가 다음 실행에서 복구된다.

matrix launcher가 Ctrl-C로 취소되면 자식 condition runner를 종료하고 기다린다. 부모만 종료되고 자식이 계속 API와 DB를 사용하는 orphan 위험을 줄였다. `kill -9` 같은 비정상 종료에서도 condition별 run lock이 중복 writer를 차단한다.

자식 프로세스의 콘솔은 조건이 끝날 때까지 부모 메모리에 보관하지 않고 조건별 `*.console.log`에 즉시 append한다. 장시간 실행의 메모리 증가를 피하고 실행 중에도 진행 상태를 확인할 수 있다.

### 3.5 OpenRouter 호출 제어와 감사 로그

관련 파일:

- `config.py`
- `twinmarket_kr/llm/client.py`

여섯 프로세스는 `outputs/.openrouter_slots`의 16개 lock을 공유한다. 조건당 16개가 아니라 컴퓨터 전체 합계가 16개다.

SDK 내부의 숨은 자동 재시도는 0으로 설정하고, 코드가 직접 최대 6회 재시도한다. timeout·네트워크·408·409·429·5xx만 backoff 대상으로 삼고, 인증·요청 형식 오류 같은 비재시도 오류는 빠르게 중단한다.

각 호출에는 agent×global turn×stage 기반의 안정적인 파생 seed가 들어간다. API 감사 JSONL에는 다음을 남긴다.

- 요청 모델과 반환 모델
- provider와 request ID
- 파생 seed와 호출 단계
- 시도 횟수와 latency
- token usage
- prompt SHA-256
- 오류 종류

API key와 prompt 원문은 기록하지 않는다.

각 호출 직후 요청 모델이 `qwen/qwen3.5-flash-02-23`인지 확인하고, OpenRouter 응답에 반환 모델이 명시된 경우에도 같은 모델인지 검사한다. 불일치는 재시도 가능한 통신 오류로 취급하지 않고 즉시 phase를 복구한다. 완료 시에도 전체 감사 로그를 다시 검사한다. provider는 감사용으로 기록하되 조건 간 provider 집합의 완전 일치를 완료 요건으로 강제하지 않는다. 라우터의 정상적인 provider 전환 때문에 이미 끝난 실험 전체를 마지막에 실패 처리하지 않기 위해서다.

### 3.6 모델 출력 오류는 해당 호출만 재시도

관련 파일:

- `twinmarket_kr/llm/analysis.py`
- `twinmarket_kr/llm/belief.py`
- `twinmarket_kr/llm/decision.py`
- `twinmarket_kr/community/posting.py`
- `twinmarket_kr/community/reading.py`
- `twinmarket_kr/community/thinking.py`

단순히 JSON object인지 확인하는 데서 끝내지 않고 필수 key, 문자열/list/boolean 타입, enum, 비어 있지 않은 설명을 검사한다. 잘못된 출력은 다른 파생 seed로 해당 LLM 단계만 최대 4회 재시도한다.

4회 모두 실패하면 임의 내용을 채우지 않고 phase를 pause·rollback한다. 따라서 malformed JSON이 가짜 belief, 임의의 community reaction, 임의의 거래로 바뀌지 않는다.

### 3.7 deterministic 1주 fallback 제거

관련 파일:

- `prompts/make_decision.txt`
- `twinmarket_kr/llm/decision.py`
- `twinmarket_kr/run_logger.py`

기존에는 LLM 출력이 두 번 잘못되면 `sell 우선, 아니면 buy, 수량 1주`를 코드가 만들었다. 이를 제거했다. 유효한 buy/sell 및 허용 수량이 나올 때까지 해당 호출을 검증 재시도하고, 끝내 실패하면 phase를 복구한다.

정상 LLM이 선택한 1주는 허용하며 다음을 로그로 구분한다.

- 최대 가능 수량 자체가 1주
- 더 큰 수량이 가능하지만 LLM이 1주 선택
- 당시 현금·보유량·최대 매수/매도 수량
- 최초 응답 성공 또는 validation retry
- `deterministic_fallback_used=false`

### 3.8 뉴스 depth와 시점

관련 파일:

- `twinmarket_kr/agents/news_agent.py`
- `twinmarket_kr/core/daily_cycle.py`
- `twinmarket_kr/run_logger.py`

현행 depth를 유지했다.

- Depth 0: 후보 headline 전부, 본문 0, 검색 0, 커뮤니티 미참여
- Depth 1: 후보 headline·본문 전부, 검색 0, 커뮤니티 본문 최대 5개
- Depth 2: 후보 headline·본문 전부, 직전 7일 검색 최대 10개, 커뮤니티 본문 최대 10개와 작성자 최근 맥락

Depth 2는 기존 prompt대로 매 turn 3~8개 키워드를 만들고 검색한다. `search_needed`는 현재 판단 기록일 뿐 검색 실행을 막지 않는다.

7일 검색은 정확한 AM/PM cutoff 이전 기사만 포함한다. 과거에 주입된 fake도 게시 시점 이후 7일 동안 검색될 수 있으며 feed 노출과 검색 재노출을 별도 기록한다.

로그 필드를 다음처럼 분리했다.

- `visible_news_ids`: headline 후보
- `read_news_ids`: 제공된 본문
- `search_result_ids`: Depth 2 검색 결과
- `influential_news_ids`: 열람 후 판단에 영향을 줬다고 지목한 뉴스
- `fake_visible`, `fake_read`, `fake_searched`, `fake_influential`
- 매핑되지 않은 영향 뉴스 참조

`selected_news`는 사전 본문 선택이 아니라 열람 이후 영향 뉴스다.

### 3.9 커뮤니티 시간 순서

관련 파일:

- `twinmarket_kr/core/collect_context.py`
- `twinmarket_kr/simulation.py`

당일 PM 주문이 모두 끝난 뒤 posting·reading·reaction·Best 5를 계산한다. 이 결과는 같은 PM 주문에 들어가지 않고 다음 거래일 AM에만 한 번 들어간다. 다음 날 PM에는 전날 커뮤니티를 다시 넣지 않는다.

posting 여부는 실제 JSON boolean만 허용한다. 글 유형·제목·본문, 선택 post ID, 모든 선택 글에 대한 reaction을 검증하고 잘못된 값은 해당 호출만 재시도한다.

### 3.10 가짜뉴스 자극물 provenance

관련 파일:

- `scripts/07_prepare_fake_news_injection.py`

기준 실제 뉴스를 삭제하지 않고 fake 한 건을 append한다. `final_approval`, leakage-safe 여부, 생성 정책은 private metadata와 manifest에 유지하며 agent 입력에서는 제거한다.

manifest는 프로젝트 상대경로, 입력·출력 hash, 승인/미승인 수, append 정책을 기록한다. 현재 기본 실행은 leakage-safe review 자극물을 포함하며, `--approved-only`는 승인된 행이 없으므로 중단한다.

주의: 저장소에 현재 체크인된 manifest는 수정 전 산출물이어서 이전 컴퓨터의 절대경로를 담고 있다. 사용자가 코드만 수정하라고 했기 때문에 이 작업에서는 CSV와 manifest를 다시 쓰지 않았다. C00에는 fake 입력이 없어 영향이 없다. 내일 fake 조건 전 승인 정책을 결정한 뒤 `python3.12 scripts/07_prepare_fake_news_injection.py --variant both`를 실행해야 새 상대경로·hash·승인 수 manifest가 실제로 생성된다.

### 3.11 실행 전·후 무결성 검증

관련 파일:

- `twinmarket_kr/run_integrity.py`

API 호출 전에 다음을 검증한다.

- daily news ID가 processed news 본문에 모두 존재
- 중복·빈 public ID 없음
- fake-off에 fake 행 없음
- fake-on은 실제 뉴스 pool을 그대로 보존하고 fake 30개만 추가
- 각 fake가 서로 다른 30개 날짜/slot에 1개씩 배치
- fake slot은 실제 뉴스 10개+fake 1개
- AM/PM cutoff에 맞는 기사만 feed에 포함

각 phase와 chunk 뒤에는 다음을 검증한다.

- 30명 전원의 global belief·portfolio·trade log가 매 turn 정확히 1개
- out-of-cohort runtime row와 system recovery message 없음
- 음수 현금·음수 보유량 없음
- 로그의 agent×date×subturn 중복·누락 없음
- 모든 buy/sell 수량이 1 이상이고 fallback 0건
- submitted order와 full fill의 수량 일치
- 모든 에이전트가 같은 feed 후보를 받음
- depth별 read/search 규칙 준수
- fake feed 날짜와 30명 노출 완전성
- community on/off별 로그 완전성
- DB boundary state SHA-256

### 3.12 오늘 1조건, 내일 5조건

관련 파일:

- `scripts/08_run_six_conditions.py`

`--conditions`로 실행할 조건을 선택할 수 있다. 오늘은 C00만, 내일은 나머지 5개만 지정한다.

내일 실행 전 완료된 C00과 현재 환경의 기간, seed, agent ID, persona DB hash, base DB hash, 모델, concurrency, retry 정책, prompt hash, Python code hash, Git commit을 비교한다. 하나라도 다르면 API 호출 전에 중단한다.

부분 실행 디렉터리가 있으면 그 조건을 선택 목록에 넣어 먼저 재개해야 한다. C00이 미완료인데 나머지 5조건만 시작하는 것을 차단한다.

## 4. 현재 데이터에서 확인된 사실

- 삼성전자 실험 기간은 63거래일, 126 AM/PM turn이다.
- baseline 실제 뉴스는 126개 slot 중 125개가 10개다.
- `2026-03-23 PM`은 원천 processed news 자체가 9개이므로 baseline과 모든 treatment에서 실제 뉴스 9개다.
- bearish·bullish는 각각 30개 fake가 30개 서로 다른 slot에 배치되며, 해당 slot은 모두 실제 뉴스 10개+fake 1개다.
- clean turn 0은 belief 100개, portfolio 100개이며 초기 보유주식은 없다.
- 초기자금은 persona 설계대로 1억원과 10억원 두 값이다.

## 5. 운동 후 확인이 필요한 결정

### Q1. 2026-03-23 PM의 실제 뉴스 9개를 그대로 둘 것인가

권장: 그대로 두고 논문을 `각 AM/PM 후보 실제 뉴스 최대 10개`라고 쓴다.

이유: 해당 cutoff 안의 원천 처리 기사 자체가 9개다. 다른 시간의 기사를 빌리거나 중복하면 정보 시점 또는 독립 뉴스 수를 왜곡한다. 처치 조건도 동일하게 9개라 조건 비교에는 비대칭이 없다. 현재 코드는 이 한 slot을 명시적으로 audit하고 허용한다.

### Q2. buy/sell-only에서 feasible action이 0개가 되는 극단 상태

현재 1회 매수 상한은 현금의 50%다. `보유 0주`이면서 `현금의 50%로 1주도 살 수 없는 상태`가 되면 buy와 sell 모두 불가능하다. 현재 코드는 가짜 주문을 만들지 않고 phase를 pause한다.

기존 부분 로그에서는 이 상태가 관측되지 않았지만 전체 126 turn에서 절대 발생하지 않는다고 수학적으로 보장되지는 않는다.

선택지는 다음과 같다.

1. 현행 유지: 발생 시 pause 후 설계를 다시 판단한다. 가장 보수적이나 야간 무인 완료 가능성이 낮아질 수 있다.
2. **권장**: 총 현금으로 1주를 살 수 있고 보유량이 0일 때만 50% 상한의 예외로 buy 1주를 feasible하게 한다. action을 임의 생성하는 fallback은 아니며, buy/sell-only의 실행 가능성만 보장한다. 다만 포트폴리오 제약 규칙의 명시적 변경이므로 사용자 승인 후 적용해야 한다.

현재는 승인 없이 2번을 적용하지 않았다.

### Q3. 미승인 review 자극물을 최종 5조건에 사용할 것인가

현재 bullish·bearish 60개 행은 모두 `final_approval=false`지만 leakage-safe와 fake-label 비노출 조건은 통과한다.

선택지는 다음과 같다.

1. 현행 파일로 실행하고 논문에서 `leakage-safe synthetic review stimulus`라고 제한한다.
2. 사람이 60개를 검토해 `final_approval=true`를 확정한 뒤 CSV와 hash를 다시 만든다.

C00에는 fake가 없으므로 오늘 밤 기준조건 실행에는 영향을 주지 않는다. 내일 나머지 5조건 전에는 결정해야 한다.

### Q4. GitHub 반영 branch 이름

현재 로컬 branch는 `samsung-baseline`이다. 이전에 말한 `0720`이 새 branch 이름을 뜻한다면 push 전에 `0720` branch를 만들지, 현재 branch에 commit할지 확인해야 한다. 이 작업에서는 임의로 branch를 바꾸거나 push하지 않았다.

## 6. 실행 전 Git 인수인계 체크리스트

- `git diff --check` 통과 확인
- Python compile 및 단위 테스트 결과 확인
- 실제 OpenRouter 호출을 이 컴퓨터에서 하지 않았는지 확인
- `EXPERIMENT_0720_RUNBOOK.md`의 오늘/내일 명령 재확인
- GitHub 반영 후 다른 컴퓨터에서 `.env`의 Qwen 모델과 concurrency 확인
- 다른 컴퓨터에서 `python3.12 scripts/04_build_experiment_base.py --force`를 한 번 실행
- C00 완료 전 나머지 5조건을 시작하지 않음
- `run_complete.json` 없는 조건을 완료로 간주하지 않음

### 6.1 이 컴퓨터에서 완료한 오프라인 검증

2026-07-20 기준 실제 OpenRouter 호출이나 simulation turn 실행 없이 다음을 확인했다.

- `git diff --check`: 통과
- `python3.12 -m compileall -q config.py twinmarket_kr scripts tests`: 통과
- `python3.12 -m unittest discover -s tests -v`: 안전성 테스트 8개 통과
- 뉴스 사전검사: baseline·bearish·bullish 모두 63거래일/126 slot 통과
- fake 입력: bearish·bullish 각각 30개 자극/30개 서로 다른 slot, 모든 fake slot 10+1 통과
- clean base 임시 생성: 100명 turn 0 belief·portfolio, 무보유, runtime table 0건 검증 통과

이 검증은 API 응답 품질·속도·429를 확인하는 smoke run이 아니다. 실제 Qwen 호출은 OpenRouter가 있는 다른 컴퓨터에서만 수행한다.

## 7. 분석 자산 정리

이전 로컬 분석 코드·HTML·모델 다운로드·중간 산출물은 삭제했다. 다른 조건에 재사용해야 하는 방법론 MD 세 개만 유지한다.

- `analysis/belief_event_study/belief_deviation_rubric.md`
- `analysis/belief_event_study/total_deviation_spec.md`
- `analysis/belief_event_study/embedding_analysis_plan.md`

`outputs/analysis/`, 로컬 `data/simulation.db`, 테스트용 `outputs/experiment_base_sim.db`는 제거했다. experiment base는 실제 실행 컴퓨터에서 다시 생성할 수 있다. 기존 실험 DB 및 로그는 삭제하지 않았다.

## 8. 사용자 우려사항 반영 체크리스트

| 우려사항 | 반영 내용 | 상태 |
|---|---|---|
| 청크마다 belief·현금·보유량·turn이 초기화됨 | 하나의 조건별 runtime DB를 계속 사용하고 global turn 1~126 유지 | 완료 |
| 장시간 실행이 멈춰 청크가 필요함 | AM·PM·community별 snapshot/rollback/resume, 같은 명령 재실행 | 완료 |
| 청크 없이 연속 실행도 가능해야 함 | `--chunk-days 0` 지원, 논문 실행은 복구 범위를 줄이기 위해 1 사용 | 완료 |
| 일부 agent 실패 뒤 다른 task가 늦게 DB를 오염시킴 | 병렬 task가 모두 정리된 뒤 phase 전체 rollback | 완료 |
| worker 30이 API를 과도하게 호출함 | agent concurrency 30과 실제 API concurrency를 분리, 컴퓨터 전체 API 상한 16 | 완료 |
| worker 증가가 DB·커뮤니티 순서를 바꿈 | DB write lock, posting 저장·reaction 반영을 agent ID 순서로 고정 | 완료 |
| 429·timeout·일시 장애로 실험이 중단됨 | Retry-After/backoff, 호출 최대 6회, process 최대 5회 재시작, phase resume | 완료 |
| 잘못된 JSON을 기본값으로 덮어 가짜 결과가 생김 | 필수 schema 검증 후 호출 단위 재시도, 실패 시 임의 값 없이 rollback | 완료 |
| Qwen 외 모델이 섞임 | 요청·응답 모델을 매 호출 및 완료 시 검사하고 불일치 즉시 차단 | 완료 |
| 6조건이 다른 초기 DB를 사용함 | 검증된 clean base에서 조건별 독립 runtime DB 생성 | 완료 |
| 오늘 C00, 내일 나머지 5조건을 같은 실험으로 이어야 함 | 같은 output root에서 C00 완료 확인 후 5조건 허용, seed·cohort·hash 비교 | 완료 |
| seed가 조건마다 달라짐 | launcher가 모든 조건을 seed 2로 고정 | 완료 |
| hold/preferred-direction이 다시 들어감 | buy/sell-only 유지, preferred-direction 미도입 | 완료 |
| API 실패가 임의의 1주 매도로 바뀜 | deterministic 1주 fallback 제거 | 완료 |
| 정상적인 1주 주문과 fallback을 구분 못함 | 최대 가능량 1주/자발적 1주/당시 제약을 로그에 기록 | 완료 |
| 1주 주문이 실질적 hold proxy인지 분석 필요 | 전체·1주 제외·거래가치 가중 분석 계획을 문서화 | 분석 단계 |
| 뉴스 depth의 기존 의미가 바뀜 | depth 0 headline, depth 1 전체 본문, depth 2 전체 본문+7일 검색 유지 | 완료 |
| fake를 강제로 선택하게 됨 | 실제 뉴스 10개+fake 1개 후보, influential 여부는 열람 후 별도 기록 | 완료 |
| 노출·본문·검색·영향 뉴스가 섞임 | visible/read/search/influential ID와 fake 접촉 단계를 분리 기록 | 완료 |
| depth 2 검색에 미래 뉴스가 섞임 | 의사결정 cutoff 이전 직전 7일만 검색 | 완료 |
| 과거 fake의 검색 재노출을 놓침 | 게시 이후 7일 검색에 포함하되 feed와 search 접촉 분리 | 완료 |
| 커뮤니티가 같은 날 PM 주문에 역으로 영향 | 장 마감 후 생성하고 다음 거래일 AM에만 1회 반영 | 완료 |
| 병렬 반응 순서가 Best 5를 바꿈 | frozen board를 공통 제공하고 반응을 결정론적 순서로 반영 | 완료 |
| fake 자극물 provenance·승인 상태가 불명확 | 승인 필드·hash·상대경로 manifest 생성 코드 보강 | 코드 완료, 내일 재생성 필요 |
| C00만 끝나도 결과를 확인하고 싶음 | 무결성 JSON은 자동 생성, 일반 실행 PDF는 C00 단독 생성 명령 제공 | 완료 |
| 6조건 비교 보고서를 C00 결과로 오해함 | 조건 비교 보고서는 6조건 완료 뒤 생성한다고 분리 명시 | 완료 |
| 기존 로컬 분석 산출물이 섞임 | HTML·중간 산출물 제거, 재사용 분석 MD 3개만 유지 | 완료 |
| 과거 `outputs/logs/simulation_*` 결과가 새 실험에 섞임 | 새 논문 run은 clean base에서 조건별 독립 DB를 만들며 과거 log 폴더를 입력으로 읽지 않음 | 완료 |
| 같은 새 output 경로를 재사용해 다른 실험이 섞임 | checkpoint가 없는데 조건 폴더가 존재하면 중단하고, checkpoint가 있으면 signature 일치 시에만 의도적으로 resume | 완료 |
| 과거 실행 스크립트를 실수로 사용함 | 논문 entrypoint를 `08_run_six_conditions.py`로 한정하고 경고 | 완료 |
| 실제 API 없이도 오류를 최대한 확인해야 함 | compile·8개 단위 테스트·뉴스 3조건·clean base 오프라인 검증 | 완료 |
| OpenRouter 실시간 429/처리량을 이 컴퓨터에서 알 수 없음 | 16개 상한·retry·resume로 안전하게 처리하고 API audit 기록 | 운영 중 관찰 필요 |
| buy/sell 모두 불가능한 극단 상태 | 임의 주문 없이 pause하도록 안전 처리 | 정책 예외 적용 여부 미결정(Q2) |
| 2026-03-23 PM 실제 뉴스가 9개 | 양 조건에 동일하게 9개 허용, 문구는 최대 10개 권장 | 사용자 확인(Q1) |
| fake 60건이 `final_approval=false` | C00 영향 없음, 내일 5조건 전에 승인 정책 결정 | 사용자 확인(Q3) |
| 어느 branch로 push할지 | 현재 `samsung-baseline`, 임의 branch 생성·push 안 함 | 사용자 확인(Q4) |

### 8.1 concurrency 결론

- `SIMULATION_CONCURRENCY=30`: 30명 agent coroutine의 준비 상한이다.
- `OPENROUTER_GLOBAL_CONCURRENCY=16`: 같은 컴퓨터의 모든 조건을 합친 실제 동시 API 요청 상한이다.
- C00 단독 실행 중 실제 동시 API 요청은 최대 16개다.
- per-run 값을 30보다 높여도 agent가 30명이므로 이득이 없다.
- global 값을 16보다 높이면 더 빨라질 수도 있지만, OpenRouter 계정·provider의 동적 rate limit 때문에 429가 늘 수 있다. 실제 장비 smoke test를 생략하기로 했으므로 16보다 올리지 않는다.
- 429가 발생해도 검증되지 않은 결과를 저장하지 않으며 Retry-After/backoff 후 재시도하고, 끝내 실패하면 해당 phase를 복구한다. 다만 rate limit 자체가 없을 것이라고 보장하는 것은 아니다.
- concurrency가 달라도 DB·체결·커뮤니티 반영 순서가 응답 완료 순서에 따라 바뀌지는 않는다. 그러나 외부 LLM 서비스는 같은 seed의 완전 동일 출력을 보장하지 않고, concurrency가 provider 배정·retry 발생 시점에 영향을 줄 수 있으므로 논문 6조건에서는 concurrency를 실험 불변값으로 고정한다. C00 이후 다른 값을 넣으면 launcher가 실행 전에 중단한다.

### 8.2 과거 실행 로그와 새 논문 run의 분리

- `outputs/logs/simulation_20260715_*` 등 과거 실험 폴더는 새 0720 runner의 입력 경로가 아니다.
- 새 `experiment_base_sim.db`를 만들 때 기존 `TradingDetails`, `trade_log`, system message, community row를 전부 지우고 belief·portfolio는 turn 0만 남긴 뒤 검증한다.
- 각 조건은 이 clean base를 자기 `runtime_sim.db`로 복사한다. 조건끼리도 DB를 공유하지 않는다.
- API 감사 로그도 각 조건 폴더의 `openrouter_calls.jsonl`에 따로 기록한다.
- 새 output root 안에 같은 조건 폴더가 checkpoint 없이 남아 있으면 새 실행을 거부한다. checkpoint가 있으면 seed·모델·뉴스 hash·base hash·code hash·concurrency가 모두 같을 때만 이어간다.
- 따라서 오늘 C00과 내일 5조건은 동일한 새 output root를 사용해야 하지만, 7월 15일 과거 실험 폴더를 output root로 지정해서는 안 된다.
