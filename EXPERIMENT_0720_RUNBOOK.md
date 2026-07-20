# 0720 논문 실험 실행 및 복구 가이드

## 1. 이 실행의 목적

이 코드는 실제 삼성전자 가격을 외생적으로 고정한 상태에서 30명의 LLM 개인투자자 에이전트가 뉴스와 커뮤니티에 반응해 매수 또는 매도하도록 하는 논문용 6개 조건을 실행한다.

가격 재현은 목표가 아니다. 모든 조건에서 같은 실제 가격·실제 뉴스·에이전트 cohort·기본 seed를 사용하고, 커뮤니티와 가짜뉴스 조건만 바꾼다.

## 2. 고정된 실험 원칙

- 모델: `qwen/qwen3.5-flash-02-23`만 허용
- 에이전트: 30명, 동일 cohort
- 기본 seed: 모든 조건에서 2
- 의사결정 공간: `buy_sell_only`
- hold 및 `preferred_direction` 단계 없음
- 가격: 실제 시가와 종가, 에이전트 주문은 가격을 형성하지 않음
- 하루: AM → PM → 커뮤니티
- AM, PM, 커뮤니티를 각각 독립 체크포인트로 저장
- 오류 시 임의의 1주 주문을 만들지 않고 해당 단계 시작 상태로 복구
- 6개 조건은 각자 별도 SQLite DB와 로그 디렉터리를 사용

## 3. 뉴스 depth

- Depth 0: 해당 AM/PM 후보의 모든 헤드라인을 보고, 본문·추가 검색·커뮤니티에는 참여하지 않는다.
- Depth 1: 모든 헤드라인과 모든 본문을 읽고, 추가 뉴스 검색은 하지 않는다. 커뮤니티에서는 최대 5개 본문을 선택해 읽는다.
- Depth 2: 모든 헤드라인과 모든 본문을 읽고, 의사결정 시각 이전 7일 뉴스에서 키워드 검색 결과를 최대 10개 추가로 읽는다. 커뮤니티에서는 최대 10개 본문과 작성자 맥락을 볼 수 있다.
- 가짜뉴스 조건은 실제 뉴스 10개를 유지하고 가짜뉴스 후보 1개를 추가한다.
- 과거에 주입된 가짜뉴스는 게시 시점 이후 7일 검색 창에 포함될 수 있다.

로그에서는 다음을 분리한다.

- `visible_news_ids`: 헤드라인 후보
- `read_news_ids`: 실제로 제공된 본문
- `search_result_ids`: Depth 2의 7일 검색 결과
- `influential_news_ids`: 모두 읽은 뒤 LLM이 판단에 영향을 줬다고 지목한 뉴스
- `fake_visible`, `fake_read`, `fake_searched`, `fake_influential`: 가짜뉴스 접촉 단계

`selected_news`는 사전 선택이 아니라 열람 이후의 영향 뉴스다.

## 4. 6개 조건

| 실행명 | Community | Fake news |
|---|---:|---|
| `c00_commoff_fakeoff` | off | off |
| `c10_common_fakeoff` | on | off |
| `c01_commoff_bearish` | off | bearish |
| `c11_common_bearish` | on | bearish |
| `c02_commoff_bullish` | off | bullish |
| `c12_common_bullish` | on | bullish |

## 5. OpenRouter 병렬 실행

한 OpenRouter 계정과 API key로도 여러 요청을 병렬 실행할 수 있다. 논문 실행의 기본값은 다음과 같다.

- 실험 내부 준비 concurrency: 30
- 6개 실험 전체가 공유하는 실제 동시 API 호출 상한: 16
- 429 및 timeout: 호출 단위 자동 재시도와 backoff
- 프로세스 종료: 동일 조건 최대 5회 자동 재시작
- 각 재시작은 마지막으로 완료된 AM·PM·커뮤니티 체크포인트 이후부터 이어진다.

전역 상한은 같은 컴퓨터에서 실행되는 모든 조건이 `outputs/.openrouter_slots`의 파일 잠금을 공유하여 적용한다. 6개 조건 각각 16개가 아니라 전체 합계가 16개다.

따라서 `SIMULATION_CONCURRENCY=30`은 30개의 네트워크 요청을 동시에 보낸다는 뜻이 아니다. 30명 에이전트 작업을 준비해 두고 실제 OpenRouter 호출은 `OPENROUTER_GLOBAL_CONCURRENCY=16`에서 대기한다. DB write는 별도 lock으로 직렬화되고, 주문 체결은 해당 AM/PM의 30명 판단이 모두 끝난 뒤 처리된다. C00만 선택하면 condition process도 하나만 실행된다.

현재 권장값은 `30/16`을 그대로 유지하는 것이다. 에이전트가 30명이므로 per-run concurrency를 30보다 높여도 빨라지지 않는다. 반대로 실제 API 상한을 16보다 높이는 것은 계정·provider의 실시간 처리량에 따라 429를 늘릴 수 있고, C00 이후 값을 바꾸면 6조건 동일성 검사에서 차단된다. 속도보다 완료 안정성을 위해 오늘 C00부터 나머지 5조건까지 같은 `30/16`을 사용한다.

concurrency는 DB 상태나 체결 순서를 바꾸지 않도록 구현되어 있지만, 외부 LLM이 같은 seed에 대해 완전히 동일한 텍스트를 반환한다는 보장은 없다. 따라서 concurrency를 바꾸어도 프로그램 상태가 오염되지는 않지만, 논문 조건 간 비교에서는 운영 조건 차이가 되므로 중간 변경을 허용하지 않는다.

같은 API key를 다른 프로젝트 폴더나 다른 컴퓨터에서 동시에 사용하면 이 lock을 공유하지 않으므로, 논문 실행 중에는 별도의 OpenRouter 작업을 병행하지 않는다.

과거 `outputs/logs/simulation_*` 폴더는 새 논문 실행의 입력이 아니다. 새 run은 `experiment_base_sim.db`의 turn 0 상태를 조건별 독립 DB로 복사한다. 다만 동일한 새 output root의 동일 조건 폴더에 checkpoint가 있으면 의도적으로 resume하므로, 새로운 독립 실험을 시작할 때는 기존 7월 15일 폴더가 아닌 새 output root를 사용한다.

## 6. 실행 전 설정

논문 0720 재실행에는 이 문서의 `scripts/08_run_six_conditions.py` 명령만 사용한다. 기존 `README.md`의 `05_run_simulation.py` 예시, `scripts/run_full_restart.sh`, 날짜가 박힌 `resume_*` 스크립트는 과거 실행용이며 clean base·6조건 동일성·새 checkpoint 보장을 공유하지 않는다. 특히 `run_full_restart.sh`에는 과거 컴퓨터의 절대경로가 있으므로 실행하지 않는다.

실제 API가 있는 컴퓨터의 `.env`에서 다음을 확인한다.

```text
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=qwen/qwen3.5-flash-02-23
OPENROUTER_COMMUNITY_MODEL=qwen/qwen3.5-flash-02-23
OPENROUTER_GLOBAL_CONCURRENCY=16
SIMULATION_CONCURRENCY=30
```

Python 3.12를 사용한다.

```bash
python3.12 -m pip install -r requirements.txt
python3.12 scripts/04_build_experiment_base.py --force
```

이 명령은 `outputs/sim.db`에서 시장·에이전트 turn 0 상태만 남긴 `outputs/experiment_base_sim.db`를 만든다. 기존 거래, belief turn 1 이상, 커뮤니티, 시스템 오류 메시지는 제거된다.

## 7. 가짜뉴스 자극물 상태

현재 bullish와 bearish PKL의 `final_approval`은 모두 false다. 이는 OpenRouter 실행 권한이 아니라 내부 사람 검토 완료 여부를 나타내는 메타데이터다. 기존 실행은 `leakage_safe=true`와 agent-visible fake label 제거 조건을 충족한 review 자극물을 사용했다.

현재 정책은 실행을 차단하지 않고 다음을 manifest에 기록하는 것이다.

- 승인된 자극물 수와 미승인 자극물 수
- 입력 PKL·기준 뉴스·출력 CSV의 SHA-256
- `append` 주입 정책
- 프로젝트 상대경로

현재 저장소에 체크인된 기존 manifest는 이전 컴퓨터의 절대경로를 담고 있다. 이번 작업은 사용자의 요청대로 코드만 수정했으므로 CSV/manifest를 여기서 다시 생성하지 않았다. C00에는 영향이 없지만, 내일 fake 조건 실행 전 선택한 승인 정책으로 한 번 다시 생성한다.

현재 review 자극물을 그대로 사용할 때:

```bash
python3.12 scripts/07_prepare_fake_news_injection.py --variant both
```

사람 검토 완료 자극물만 강제하려면 `--approved-only`를 사용한다. 현재 데이터에서는 승인 행이 없으므로 이 옵션은 의도적으로 실행을 중단한다. 재생성 후 생성된 상대경로 manifest와 출력 hash를 확인한 다음 5조건을 시작한다.

## 8. 오늘 밤: C00 한 조건만 실행

먼저 위 6절의 clean base 생성 명령을 **한 번만** 실행한다. 그 다음 아래 명령으로 커뮤니티 off·가짜뉴스 off 기준조건만 실행한다.

```bash
python3.12 scripts/08_run_six_conditions.py \
  --start-date 2026-02-27 \
  --end-date 2026-06-01 \
  --chunk-days 1 \
  --output-root outputs/logs/paper_0720 \
  --conditions c00_commoff_fakeoff
```

일시적인 API/프로세스 오류는 최대 5회 자동 재시작하며, 그래도 중단되면 **완전히 같은 명령**을 다시 실행한다. 마지막으로 완료된 AM·PM·community 완료 지점 이후부터 재개한다. community off의 community phase는 DB를 바꾸지 않는 완료 경계다. 같은 출력 경로를 두 프로세스가 동시에 실행하면 runner lock이 두 번째 실행을 차단한다.

오늘 밤 실행이 끝났는지는 아래 두 파일로 확인한다.

- `outputs/logs/paper_0720/c00_commoff_fakeoff/run_complete.json`
- `outputs/logs/paper_0720/c00_commoff_fakeoff/run_metadata.json`

`run_complete.json`이 없으면 완료로 간주하지 않는다.

C00 하나만 완료된 상태에서도 일반 실행 PDF는 만들 수 있다. PDF 생성 실패가 실험 재실행을 유발하지 않도록 launcher와 분리했으며, `run_complete.json`을 확인한 뒤 아래 명령을 실행한다.

```bash
python3.12 scripts/generate_run_report_pdf.py \
  --run-dir outputs/logs/paper_0720/c00_commoff_fakeoff \
  --output outputs/logs/paper_0720/c00_commoff_fakeoff/run_report.pdf
```

이 PDF는 C00 자체의 거래·belief·수익률 실행 보고서다. 조건 간 효과 비교 보고서는 나머지 5개 조건까지 완료된 뒤 생성해야 한다. C00에서는 community·fake 전용 보고서가 비어 있는 것이 정상이다.

C00이 미완료라면 내일 5조건 명령을 먼저 실행하지 말고, 위 C00 명령을 그대로 다시 실행해 완료한다. launcher가 미완료 C00을 발견하면 다른 처치 실행을 차단한다.

## 9. 내일: C00을 제외한 나머지 5조건 실행

오늘 만든 `outputs/experiment_base_sim.db`와 `outputs/logs/paper_0720`을 삭제하거나 다시 만들지 않는다. 아래 명령은 C00을 재실행하지 않고 나머지 다섯 조건만 시작한다.

```bash
python3.12 scripts/08_run_six_conditions.py \
  --start-date 2026-02-27 \
  --end-date 2026-06-01 \
  --chunk-days 1 \
  --output-root outputs/logs/paper_0720 \
  --conditions \
    c10_common_fakeoff \
    c01_commoff_bearish \
    c11_common_bearish \
    c02_commoff_bullish \
    c12_common_bullish
```

실행 전 C00과 현재 환경의 다음 항목을 자동 비교한다.

- seed 2와 동일 30명 agent ID
- `sys_100.db` hash와 초기 base DB hash
- Qwen 모델, API concurrency 및 재시도 설정
- 프롬프트·Python 코드 전체 hash와 Git commit
- 기간과 정보 시점 모드

하나라도 다르면 나머지 실험을 시작하지 않는다. 일시적 API 오류로 프로세스가 종료되면 조건별로 최대 5회 재시작하며, 컴퓨터 전체가 종료되면 위 명령을 그대로 다시 실행한다.

## 10. 실행 중 확인할 파일

- 전체 상태: `outputs/logs/paper_0720/matrix_manifest.json`
- 조건별 실시간 누적 콘솔: `outputs/logs/paper_0720/<condition>.console.log`
- 조건별 체크포인트: `outputs/logs/paper_0720/<condition>/checkpoint.json`
- 일시정지 원인: `outputs/logs/paper_0720/<condition>/paused.json`
- API 감사: `outputs/logs/paper_0720/<condition>/openrouter_calls.jsonl`

API 감사 로그에는 원문 API key를 기록하지 않는다. model, provider, request ID, seed, 호출 단계, 재시도 횟수, latency, token usage, prompt hash를 기록한다.

## 11. 완료 판정

조건 하나는 다음을 모두 만족해야 `complete`가 된다.

- 63거래일, 126 AM/PM turn
- 매 turn마다 30명의 belief·buy/sell 결정·portfolio 존재
- 모든 날짜의 AM·PM·커뮤니티 phase 완료
- global turn이 1부터 126까지 연속
- deterministic fallback 주문 0건
- fake-off 조건의 fake 노출 0건
- fake-on 조건의 실제 주입일에 30명 모두 헤드라인 노출
- Depth 0/1/2 정보 접근 규칙 준수
- community-off 조건의 community log 0건
- community-on 조건의 날짜별 참여 로그 완전성
- 조건별 입력 파일·프롬프트·코드·base DB hash 일치

## 12. 1주 주문 분석

정상 LLM이 선택한 1주 주문은 허용한다. 다음을 구분해 기록한다.

- 최대 매수 가능량이 1주인 경우
- 현재 보유량이 1주인 경우
- 더 큰 거래가 가능하지만 LLM이 1주를 선택한 경우
- deterministic fallback 여부: 새 실행에서는 항상 false여야 함

논문 분석에서는 전체 주문, 1주 제외, 거래가치 가중 결과를 함께 비교한다.

## 13. 재사용 가능한 belief 분석 문서

다른 조건에도 같은 방식으로 적용할 분석 정의는 다음 문서에 유지한다.

- `analysis/belief_event_study/belief_deviation_rubric.md`
- `analysis/belief_event_study/total_deviation_spec.md`
- `analysis/belief_event_study/embedding_analysis_plan.md`

새 실행 로그에는 조건명과 입력 경로만 바꾸어 같은 event window, total deviation, persona vulnerability, embedding 이동 분석을 적용한다.
