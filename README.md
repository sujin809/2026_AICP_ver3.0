# TwinMarket Korea

> 상태: **무과금 P0 리팩터링·재봉인·검증 PASS — live canary·45일 본실험은 별도 승인 전 NO-GO**
>
> 현 code·prompt·persona projection으로 StudySpec을 다시 봉인했고, 전체
> 무과금 회귀·sealed profile 검증·1 agent/45거래일 OFF/ON 실제 중단·재개 offline
> 검증·PDF fixture 시각 검수를 마쳤다. 유료 API canary와 본실험은 승인·실행하지 않았으므로
> paper run은 여전히 NO-GO다.

삼성전자 실제 가격을 외생적으로 고정하고, LLM 개인투자자 에이전트의 거래
방향과 정보환경 효과를 연구하는 시뮬레이션이다. 현재 목표는 검증된 실제뉴스
기능을 기존 번호형 파이프라인 하나에서 재현 가능하게 실행하는 것이다.

## 현재 기준과 Git 근거

- 원격 저장소: `sujin809/2026_AICP_ver3.0`
- 기준 브랜치: `sujin_0727`
- 확인된 기준 커밋: `f4e17956f39e0cb0d94974cb03684d68f5e53ce7`
- 커밋 작성자: `sujinjung <e62974347@gmail.com>`
- 커밋 제목: `RN Community A/B: 뉴스 재구축, 입력 봉인, D2 후보 풀 재정의`
- Git이 추적하는 실제뉴스 정본:
  `preparation/rn_ab_sealed_v1/news.json`

따라서 sujin이 만든 최신 봉인 뉴스가 현재 기준 입력이라는 점은 Git에서 확인할
수 있다. 현 profile은 이 뉴스 파일을 바꾸지 않은 채 current code·prompt·persona
projection으로 다시 봉인하고 검증했다. 이후 code·prompt·입력을 바꾸면 새
candidate profile에서 같은 절차를 다시 수행한다.

## 유일한 목표 실행 흐름

```text
scripts/00_* … scripts/04_*
  -> scripts/05_run_simulation.py
     -> twinmarket_kr/simulation.py
        -> twinmarket_kr/core + agents + community
           -> canonical DB/journal
              -> validator -> report
```

새 실험은 이 번호형 흐름만 사용한다. 과거 RN 전용 09/12 실행기와
`twinmarket_kr/rn_ab` runtime은 공통 본류로 필요한 기능을 옮긴 뒤
working tree에서 제거했다. 과거 잘못된 실행 흐름은 별도 archive runtime으로
복제하지 않고 Git history로만 복구한다.

통합 엔진의 event 순서는 다음과 같이 고정한다.

```text
사용 가능한 과거 성과와 다음 AM community 노출 확정
  -> 현재 STB
  -> 이전 LTB + 현재 STB로 analysis/decision
  -> 잔고·보유량 제약을 반영한 실제 fill
  -> 이전 LTB + 현재 STB + 실제 fill + 사용 가능한 과거 성과로 post-fill LTB
  -> PM이면 community phase
```

`decision`은 의도이고 `fill`은 실제 체결이다. 둘을 합치거나 실제 체결 전에
LTB를 갱신하지 않는다.

핵심 정책값은 다음과 같다.

- 실제뉴스 event 목표: 종목 5·섹터 3·경제 2, 합계 최대 10개
- 카테고리 부족: 다른 카테고리 기사로 backfill하지 않고 실제 전달 수와
  shortage를 기록한 채 계속 실행
- community 선택 읽기: D1 최대 5개, D2 최대 5개
- 전역 Best: 최대 5개
- 뉴스 D2 추가 검색: 최근 7일의 cutoff-safe 후보 중 최대 5건
- 게시글 본문: 최대 500자, 501자는 거부하고 자동 절단하지 않음
- Best 전달: 작성자 자기 글 제외, 6위 글로 backfill하지 않음
- 거래 outcome: `next_turn`, `H1`, `H5`; due 이후 post-fill LTB에서만 반영

## STB·LTB production prompt

STB와 LTB는 별도 prompt를 사용하며, 이름만 나뉜 같은 호출이 아니다.

| 단계 | production prompt | 입력 경계 | 사용 시점 |
| --- | --- | --- | --- |
| STB | `prompts/update_short_term_belief.txt` | 현재 event의 허용 뉴스·D2 검색·실제로 읽은 community claim과 persona | analysis·decision 전 |
| LTB | `prompts/update_long_term_belief.txt` | 이전 LTB, 현재 STB, 실제 decision/fill, 그 event에 성숙한 과거 outcome과 persona | 실제 fill 뒤, 다음 event 전 |

두 prompt 모두 기존 `dim_1`~`dim_6` 스키마를 유지한다. 거래는
`belief_summary`가 아니라 이전 LTB 6차원과 현재 STB 6차원을 분리 입력으로
받아 비교·종합한다. LTB는 `maintain` 한 단어 또는 이전 문장 복사를 허용하지
않고 매 event 여섯 차원을 모두 다시 쓴다. 자세한 causal 순서는
[`ARCHITECTURE.md`](ARCHITECTURE.md)의 STB/LTB 절을 따른다.

## 입력 정본과 격리 경계

baseline의 뉴스·달력·가격·cohort 기준은
`preparation/rn_ab_sealed_v1/`의 봉인 묶음이다. 현 `study_spec.json`은 현재
production prompt와 persona projection을 반영해 다시 봉인했으며, 검증 중
`news.json`의 바이트·5/3/2 quota·no-backfill 정책은 그대로 유지됐다. 이 사실은
유료 provider의 reasoning-off telemetry나 본실험 승인을 뜻하지 않는다.

- `study_spec.json`: 연구 정책과 입력 hash
- `calendar.json`: 거래일과 AM/PM event
- `cohort.json`: 고정 에이전트와 depth
- `news.json`: 실제뉴스 title·summary·version·slot·shortage 기록
- `prices.json`, `stage_inputs.json`: event별 실행 입력
- `known_injection.json`, `review.json`: fake 격리와 누수 검토
- `prompts/`: 해당 봉인 묶음의 재현용 prompt 사본

다음은 신규 실행 입력으로 사용하지 않는다.

- `archive/legacy_inputs/rn_ab_source_candidate_v1/input_candidates/`
- 과거 legacy selected-news CSV
- `outputs/` 아래의 과거 run 결과
- 날짜별 복구 스크립트나 특정 과거 run 경로

뉴스 목표 수를 채우지 못한 event는 안전하지 않은 기사, 중복 기사, 합성 기사로
메우지 않는다. 실제 전달 수와 부족 사유를 봉인하고 두 조건에서 같은 묶음을
사용한 채 계속 실행한다. 상세 연구 계약은
[`EXPERIMENT_DESIGN.md`](EXPERIMENT_DESIGN.md)에 있다.

## 환경 준비

Python 3.12 환경을 권장한다.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

유료 실행에 필요한 키·모델·provider 설정은 승인된 run에서만 사용한다.
일반 smoke test와 정적 검사는 외부 API 없이 먼저 수행한다.

`05`는 유일한 simulation 진입점이지만 live 실행에는
`--allow-paid-api`와 사전 reasoning-off canary audit가 모두 필요하다.
RN `09/12`와 별도 checkpoint runner는 제거됐다. 안전한 검사 순서,
Go/No-Go 기준, 재개·완료·리포트 계약은
[`RUNBOOK_AND_PREFLIGHT.md`](RUNBOOK_AND_PREFLIGHT.md)를 따른다.

## 결과와 재현성

신규 run은 run ID가 있는 독립 디렉터리에 아래 정보를 남겨야 한다.

- resolved StudySpec과 모든 입력·prompt·코드 hash
- STB, analysis, decision, fill, post-fill LTB의 provenance
- community 게시·제목 노출·본문 노출·반응·Best 전달 기록
- logical call, physical attempt, validation, commit/rollback journal
- checkpoint와 resume 기록
- 완료 marker와 integrity 결과; validator·CSV·PDF는 외부 파생물 경로에 생성

CSV와 PDF는 canonical DB/journal에서 생성하는 파생물이다. signed run directory
밖의 명시적 `derived/<condition>/`에만 생성하며, 파생물을 runtime 입력으로
사용하거나 같은 사실을 두 원장에 따로 쓰지 않는다.

팀원이 결과를 확인할 때는 다음 순서로 본다.

| 확인 목적 | 먼저 볼 것 |
| --- | --- |
| 완료·재개 상태 | `run_complete.json`, `.runtime/checkpoint.json`, `run_metadata.json` |
| 실제 거래 | `exchange_fills.csv`; 의도는 `submitted_orders.csv`와 분리 |
| STB/LTB 계보 | `.runtime/committed.db`, `memory_lineage.jsonl`, `agent_turns.jsonl` |
| 뉴스 수·부족 | `run_metadata.json`의 bundle hash와 sealed `news.json` coverage |
| 게시글 원문·source | `community_posts.csv` |
| 제목만 봄 vs 본문 읽음 | `community_interactions.csv`의 `exposure_level` |
| Best 원문·rank·자기 글 제외 | `community_best_posts.csv` |
| API retry·reasoning-off | response journal, `openrouter_calls.jsonl`, canary audit |
| 무결성 | `python scripts/99_validate.py --run-dir <run-dir> --output <pair-root>/derived/<condition>/run_validation.json` |
| 행동 방향 | `python validation/validate_trading_direction.py --run-dir <run-dir> --output-dir <pair-root>/derived/<condition>/direction_validation --skip-initial-days 3` |
| PDF | `python scripts/generate_run_report_pdf.py --run-dir <run-dir> --output <pair-root>/derived/<condition>/run_report.pdf`; ON arm은 community PDF도 같은 방식 |

## 팀 정본 문서

| 문서 | 역할 |
| --- | --- |
| [`README.md`](README.md) | 저장소 입구, 기준 입력, 단일 실행 구조 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 모듈·schema·계보·현재 구현 상태·격리·남은 gate |
| [`EXPERIMENT_DESIGN.md`](EXPERIMENT_DESIGN.md) | 연구질문·조건·정책·분석 계약 |
| [`RUNBOOK_AND_PREFLIGHT.md`](RUNBOOK_AND_PREFLIGHT.md) | 준비, preflight, 실행, 재개, 검증, 보고 |

위 네 파일만 팀의 현재형 정본이다. `AGENTS.md`는 문서 정본이 아니라 별도의
작업 지침이므로 유지한다. 삭제하면 안 되는 과거 연구자료는
`archive/legacy_docs/`, 과거 분석·검증 결과는
`archive/legacy_results/`에 보존하되 현재 정책이나 실행 명령으로 사용하지 않는다.
`analysis/`, `outputs/`, `preparation/`, `prompts/`, `validation/`,
`News_Scraper/`의 Markdown은 결과·데이터·도구 sidecar이며 팀 정본이 아니다.
사용자 데이터와 이전 run 결과는 문서 정리 명목으로 삭제하지 않는다.
