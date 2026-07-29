# 최신 C00 실험 재분석과 논문 재설계 제안

분석 기준: `off_result_20260721`, push commit `5605732`  
실행 시작 코드: `8604f9`  
분석일: 2026-07-22

## 기술 요약

결론부터 말하면, 최신 C00은 **실행 가능한 연속-state baseline**으로는 살릴 수 있지만, 현재 60% 방향 일치율을 “개인투자자 수급을 재현했다”거나 “예측력이 있다”는 주결과로 쓰기는 어렵다.

이번 결과는 예전 7월 15일 C00과 다르다. 실행 메타데이터가 기록한 시작 commit `8604f9`는 사용자가 확인을 요청한 `81f35f8` 이후 commit이며, 7월 20일 restart-safety 보완을 포함한다. 실제로 선언된 45거래일 범위 안에서는 30명 × 45일 × AM/PM = 2,700개의 agent-turn이 중복·누락 없이 존재하고, turn 1–90과 portfolio·belief 상태가 연속된다. 이전처럼 chunk마다 1억 원으로 초기화된 실험은 아니다.

다만 `run_complete.json`의 상태는 정상적인 63일 완주가 아니라 `complete_through_scope`와 `truncated_posthoc`다. 설정상 종료일은 2026-06-01이지만 봉인된 분석 범위는 2026-05-04까지 45거래일이며, 이후 5월 6일 inflight checkpoint와 partial chunk가 남아 있다. `paused.json`은 4월 29일 PM에 발생했다가 이후 복구된 과거 실패 trace이므로 최종 절단의 원인으로 해석하지 않았다. 따라서 이 결과는 **45일 사후 절단 partial run**으로 명시해야 한다.

핵심 결과는 다음과 같다.

| 항목 | 최신 C00 | 해석 |
|---|---:|---|
| 개인 수급 방향 일치 | 27/45 = 60.0% | Wilson 95% 구간 45.5%–73.0% |
| 매수일 recall | 16/28 = 57.1% | 매수일을 특별히 잘 맞힌 결과는 아님 |
| 매도일 recall | 11/17 = 64.7% | 양 방향이 완전히 무너지지는 않음 |
| Balanced accuracy | 60.9% | class imbalance를 보정한 주지표 후보 |
| 항상 매수 방향 일치 | 28/45 = 62.2% | C00보다 한 날짜 더 맞음 |
| 전일 시장방향 baseline BA | 57.4% | C00의 우위는 3.6%p |
| AM 시가갭 역행 규칙 | 일치 80.0%, BA 82.8% | AM prompt에 이미 보이는 가격만 사용 |
| PM 당일수익률 역행 규칙 | 일치 91.1%, BA 92.9% | PM prompt의 당일 수익률만 사용 |
| 일별 Pearson | 0.503 | 첫 자금 투입일에 크게 의존 |
| 첫 5일 제외 Pearson | 0.018 | 크기 정합성 증거가 사실상 사라짐 |
| AM-only BA, 첫 5일 제외 | 51.9% | ex-ante에 가까운 신호는 약함 |
| PM-only BA, 첫 5일 제외 | 59.8% | 당일 종가·뉴스를 본 same-day reconstruction |

방향 일치가 50%보다 크다는 단순 단측 이항검정은 p=0.116이고, 예측 매수/매도 일수까지 고정한 초등적 hypergeometric 검정은 p=0.133이다. Fisher exact two-sided p=0.221이며, 항상 매수와의 paired McNemar exact p=1.000이다. 더 결정적으로, C00은 decision-time price만 쓴 단순 역행 규칙에 크게 못 미친다. AM C00과 AM 시가갭 역행 규칙의 paired exact p는 0.0169, PM C00과 PM 당일수익률 역행 규칙은 0.00052다. 날짜 자기상관과 단일 window 때문에 이 p값도 진단용이지만, 현재 60%를 유의미한 시장상태 부가가치로 해석할 수 없다는 결론은 훨씬 강해진다.

가장 중요한 설계 문제는 `hold`가 없다는 것이다. 모든 agent가 매 AM/PM에 반드시 buy 또는 sell을 하고 전량 체결된다. 2,700개 turn 중 306개는 자금·보유 제약 때문에 한 방향만 가능하고, 589개는 1주 주문이다. belief에 “관망”이 들어간 213개 turn도 72개 매수와 141개 매도로 강제 변환됐다. 지금의 일별 순주문은 자연스러운 투자 의향뿐 아니라 **강제거래, feasibility, 초기 배치, 최대수량 선택 규칙**의 합성 결과다.

따라서 가장 설득력 있는 새 논문은 “long memory를 넣어 일치율을 올렸다”가 아니다. 다음 질문이 더 강하다.

> **출처를 보존하는 short/long memory와 outcome-based reflection은 LLM 투자자 agent의 시간적 행동 정합성을 높이는가, 아니면 잘못된 뉴스와 자기 확신을 더 오래 고착시키는가?**

C00은 이 논문의 `M0` baseline으로 사용할 수 있다. 단, 현재 agent는 완전한 무기억이 아니라 직전 belief summary, 최근 주문 5건, 직전 주문 이유, portfolio/PnL을 받는 **shallow-memory baseline**이다. 다음 실험은 이를 명시하고 memory 구조, hold/action architecture, provenance를 각각 ablation해야 한다.

## 핵심 발견과 해석

### 1. 이번 C00은 어떤 결과인가

분석한 결과는 GitHub의 다음 묶음이다.

- [최신 결과 브랜치](https://github.com/ujlee1661/2026_AICP_ver2.0/tree/off_result_20260721)
- [run_complete.json](https://github.com/ujlee1661/2026_AICP_ver2.0/blob/off_result_20260721/outputs/logs/paper_0721/c00_commoff_fakeoff/run_complete.json)
- [integrity_report.json](https://github.com/ujlee1661/2026_AICP_ver2.0/blob/off_result_20260721/outputs/logs/paper_0721/c00_commoff_fakeoff/integrity_report.json)
- [validation_output.json](https://github.com/ujlee1661/2026_AICP_ver2.0/blob/off_result_20260721/outputs/logs/paper_0721/c00_commoff_fakeoff/validation_output.json)
- [validation PDF](https://github.com/ujlee1661/2026_AICP_ver2.0/blob/off_result_20260721/validation/outputs/c00_commoff_fakeoff_2026-05-04/validation_report.pdf)
- [실행 PDF](https://github.com/ujlee1661/2026_AICP_ver2.0/blob/off_result_20260721/outputs/reports/paper_0721_c00_commoff_fakeoff_2026-05-04_report.pdf)

commit 관계도 확인했다.

- 사용자가 지정한 기준 commit: `81f35f8`
- 실행 시작 메타데이터: `8604f9`
- Git ancestry: `81f35f8`은 `8604f9`의 ancestor
- 결과를 push한 commit: `5605732`

즉 이번에 재분석한 결과는 7월 20일 보완 이전 legacy 결과가 아니라, 해당 보완을 고려해 시작된 최신 C00이다.

### 2. 무결성은 좋아졌지만 63일 완주는 아니다

봉인된 범위 안의 실행 무결성은 좋다.

- 30 agents
- 45 trading days
- 90 global turns
- 2,700 unique `agent × date × subturn` keys
- duplicate 0, missing 0
- submitted orders 2,700, fills 2,700, portfolio updates 2,700
- full fill 100%
- 1,320개 날짜 간 cash/quantity state transition mismatch 0
- 직전 belief summary가 다음 turn의 `previous_belief`에 연결되는 2,670개 transition 모두 일치
- deterministic fallback 0

하지만 완료 상태는 다음과 같이 읽어야 한다.

- configured end: 2026-06-01
- sealed end: 2026-05-04
- declared days: 45
- 의도한 63일 대비 coverage: 71.4%
- 2026-05-06 partial chunk 존재
- checkpoint는 5월 6일 AM inflight partial turn을 가리킴
- `paused.json`은 4월 29일 PM의 복구된 과거 실패 trace이며 최종 cutoff의 원인이 아님

논문 Methods에는 “63-day simulation”이 아니라 “45-trading-day post-hoc sealed run”이라고 써야 한다. 이후 정상 완주 결과가 나오면 이 C00과 새 결과를 한 표본처럼 합치지 말고 legacy partial baseline과 confirmatory rerun으로 분리하는 것이 안전하다.

### 3. 60%는 양방향 variation이지 성과 증명이 아니다

실제 개인투자자 수급은 28일 순매수, 17일 순매도였다. C00 aggregate flow는 22일 매수, 23일 매도를 예측했고 confusion matrix는 다음과 같다.

| 실제 \ C00 | 매수 | 매도 |
|---|---:|---:|
| 실제 순매수 | 16 | 12 |
| 실제 순매도 | 6 | 11 |

좋은 점은 항상 매수처럼 한 방향으로만 쏠린 결과는 아니라는 것이다. 실제 매도일 17일 중 11일을 맞혔고 balanced accuracy가 60.9%다. 다만 이는 “양방향 주문 variation이 존재한다”는 기술통계이지, prompt에 들어온 시장상태를 유효하게 이용했다는 증거는 아니다.

그러나 raw direction match는 60.0%로 항상 매수 62.2%보다 낮다. 균형 정확도는 전일 시장수익률 방향 baseline 57.4%보다 3.6%p 높을 뿐이다. 그리고 같은 decision-time에 agent가 실제로 본 가격을 이용한 역행 규칙에는 크게 뒤진다.

#### 결정적인 same-information baseline

한국 개인투자자 수급은 이 구간에서 당일 가격변화와 매우 강한 역행 관계를 보인다. 이 관계가 agent prompt에도 이미 들어 있으므로, 항상 매수나 전일 시장방향만이 아니라 다음 규칙과 비교해야 한다.

- AM: 시가가 전일 종가보다 내리면 개인 순매수, 오르면 순매도로 예측
- PM: 당일 종가수익률이 음수면 개인 순매수, 양수면 순매도로 예측

| 같은 정보시점 비교 | 방향 일치 | Balanced accuracy | C00 BA |
|---|---:|---:|---:|
| AM opening-gap contrarian | 80.0% | 82.8% | AM C00 55.6% |
| PM current-return contrarian | 91.1% | 92.9% | PM C00 59.8% |
| AM opening-gap, 첫 5일 제외 | 80.0% | 81.8% | AM C00 51.9% |
| PM current-return, 첫 5일 제외 | 92.5% | 93.5% | PM C00 59.8% |

PM 규칙은 종가를 사용하므로 ex-ante forecast가 아니라 reconstruction baseline이다. 그러나 PM C00도 같은 정보를 보기 때문에 PM의 behavioral reconstruction claim에는 반드시 비교해야 한다. AM 시가갭 규칙도 이미 80%라서 ex-ante에 더 가까운 AM C00 역시 단순 가격반응을 보존하지 못한다.

따라서 현재 결과의 핵심은 “LLM이 약한 market signal을 보였다”가 아니라 **LLM의 복잡한 persona·news·belief pipeline이 이미 입력에 존재하는 강한 개인투자자 역행 패턴을 희석했다**는 것이다. 다음 실험에서는 price-only baseline 대비 incremental improvement 또는 price-only model로 설명되지 않는 residual retail flow를 주목표로 삼아야 한다.

validation에 있는 `actual_ratio_random = 66.7%`도 주의해야 한다. 이것은 고정된 RNG의 단 한 번 draw다. 그 draw가 C00보다 높거나 낮다는 사실은 baseline 성능이 아니다. 실제 매수 비율을 사용한 독립 random predictor의 기대 direction accuracy는 약 53%이고 balanced accuracy의 기대값은 50%다. 다음부터는 analytic expectation 또는 최소 수천 번 Monte Carlo 분포를 보고해야 한다.

### 4. Pearson 0.503은 초기 자금 배치가 만든 상관이다

첫날 30명 전원은 현금 1억 원, 보유 0주다. 매도할 수 없으므로 전원 buy-only이고, 모두 매수했다. 이 날 simulated net buy는 약 16.91억 원으로 이후 절대 일별 순주문 중위값의 약 15.6배다. 실제 개인투자자 수급도 분석 창에서 가장 큰 순매수였다.

이 공통 outlier가 value-flow Pearson을 크게 끌어올린다.

| 초기 제외 | 방향 일치 | Balanced accuracy | Pearson | Spearman |
|---:|---:|---:|---:|---:|
| 0일 | 60.0% | 60.9% | 0.503 | 0.213 |
| 1일 | 59.1% | 60.1% | 0.299 | 0.159 |
| 3일 | 59.5% | 60.4% | 0.104 | 0.112 |
| 5일 | 57.5% | 58.4% | 0.018 | 0.046 |
| 10일 | 57.1% | 58.1% | 0.120 | 0.132 |

첫 5일 방향 일치는 4/5=80%지만, 나머지 40일은 23/40=57.5%다. 그러므로 Pearson 0.503을 논문 headline으로 쓰면 안 된다. 초기 allocation을 별도 burn-in으로 처리하거나, agent마다 이미 관측 시작 전 portfolio를 주거나, target exposure를 초기화한 뒤 평가 창을 시작해야 한다.

### 5. PM 결과는 예측보다 same-day reconstruction에 가깝다

AM과 PM을 분리하면 해석이 달라진다.

| 집계 | 전체 45일 BA | 첫 5일 제외 BA | 첫 5일 제외 Pearson |
|---|---:|---:|---:|
| AM only | 55.6% | 51.9% | -0.250 |
| PM only | 59.8% | 59.8% | 0.284 |
| AM+PM | 60.9% | 58.4% | 0.018 |

PM agent는 당일 장중/종가 시장 특징과 15:30까지의 뉴스를 보고 종가에 주문한다. 실제 개인투자자의 그날 전체 수급과 비교하면 정보시점이 겹친다. 따라서 PM 결과는 미래를 맞히는 forecast라기보다 **그날 시장과 뉴스에 반응한 개인 flow를 합성 agent가 얼마나 재구성하는가**에 가깝다.

논문 선택지는 둘 중 하나다.

1. behavioral reconstruction 논문이라면 PM을 포함하되 같은 날 정보를 사용했다고 명확히 쓴다.
2. prediction 논문이라면 전일 종가 또는 당일 08:59를 cutoff로 고정하고 AM-only를 primary로 둔다.

현재 결과에서 두 framing을 섞으면 60%의 의미가 과장된다.

### 6. 이것은 시장 수급·가격형성 모형이 아니다

현재 engine은 실제 가격을 외생적으로 공급하고, agent order를 전량 그 가격에 체결한다. agent들 사이의 주문이 서로 매칭되지 않고, aggregate demand가 가격을 바꾸지도 않는다. 따라서 다음 표현은 피해야 한다.

- 시장 균형을 재현했다
- supply-demand matching을 구현했다
- agent 수급이 가격을 형성했다
- simulated market가 실제 시장을 복제했다

현재 가능한 표현은 다음이 더 정확하다.

> **실제 가격·뉴스 맥락을 조건으로 생성된 synthetic investor population의 일별 signed net-flow가 실제 개인투자자 net-flow 방향과 얼마나 정렬되는가**

영문으로는 `individual-investor net-flow direction alignment` 또는 `behavioral flow reconstruction`이 안전하다.

## 데이터, 행동공간, belief 메커니즘

### 7. 30명 전원 1억 원은 실수이지만 분석상 통제 조건으로 바꿀 수 있다

활성 cohort는 A001–A030을 ID 순서로 자른 비무작위 표본이고, 30명 전원이 정확히 1억 원, 무보유로 시작한다.

| Persona 축 | 분포 |
|---|---|
| 성별 | female 17, male 13 |
| 연령 | 20대 9, 30대 18, 40대 3 |
| 전략 | technical 16, value 14 |
| news depth | 0: 10, 1: 16, 2: 4 |
| user type | ordinary 27, small influencer 2, big influencer 1 |
| disposition trait | low 12, medium 10, high 8 |
| lottery/risk trait | low 19, medium 3, high 8 |

이 실수 때문에 wealth heterogeneity나 10억 원 agent의 행동 차이는 전혀 분석할 수 없다. 실제 투자자 인구의 대표 표본도 아니다. 50대 이상이 없고 일부 cell은 1–4명뿐이다.

다만 논문에서 이를 `homogeneous-endowment descriptive baseline`으로 재정의할 수 있다. 자본 차이를 제거한 동일 제약 환경에서 persona prompt와 행동이 함께 어떻게 나타나는지 보는 feasibility baseline이다. Persona trait가 공동으로 비무작위 설정되어 있으므로 이것만으로 내부 타당성이 생기지는 않는다. 내부 construct validity는 단일 trait를 무작위로 바꾸는 counterfactual clone 실험에서 확보해야 한다.

- wealth/persona interaction은 관측하지 않음
- 실제 투자자 분포를 복제했다는 주장은 하지 않음
- demographic 차이는 synthetic prompt effect의 기술통계일 뿐임
- 다음 실험에서는 동일자본 baseline과 현실적 wealth distribution을 별도 arm으로 둠

### 8. `hold` 부재가 수급과 belief–action 평가를 오염시킨다

이번 C00은 `allow_hold=false`다. 모든 agent가 90번씩 거래해 총 2,700개 주문이 생겼다.

- buy 1,509, sell 1,191
- buy/sell 모두 가능한 turn 2,394
- buy-only 124
- sell-only 182
- 한 방향만 가능한 turn 306 = 11.3%
- 1주 주문 589 = 21.8%
- 더 크게 거래할 수 있었지만 1주만 선택한 turn 324
- 가능한 최대수량을 그대로 선택한 turn 996 = 36.9%
- agent당 평균 명목 turnover = 초기자본의 7.09배

belief summary에 `관망`이 포함된 turn은 213개인데도 action은 buy 72, sell 141이다. reason에는 `관망 불가`가 직접 쓰인 turn이 11개, `강제 거래 제약`이 쓰인 turn이 17개 있다. 따라서 지금의 R8 belief–action consistency에서 “belief는 hold인데 action은 sell”을 agent 모순으로 코딩하면 안 된다. 환경이 hold를 금지해 만든 conflict다.

다음 로그 스키마가 필요하다.

1. `desired_stance`: buy / hold / sell / unclear
2. `trade_intent`: trade / no-trade
3. `target_exposure`: 현 자산 대비 목표 보유비율
4. `confidence`와 `risk_budget`
5. `feasible_action_set`
6. `allocator_output`
7. `executed_action`
8. `forced_trade_conflict`와 `constraint_reason`

LLM이 주식 수량을 직접 정하기보다 desired exposure와 confidence를 출력하고 deterministic allocator가 현금·보유·최소단위에 맞춰 quantity를 계산하도록 바꾸는 것이 좋다.

### 9. 현재 agent도 완전한 무기억은 아니다

사용자가 제안한 short/long memory를 설계할 때 C00을 `no-memory`라고 부르면 안 된다. 현재 prompt에는 이미 다음 상태가 들어간다.

- 직전 `belief_summary`
- 최근 주문 5건
- 직전 action reason
- 현재 portfolio, 현금, 보유수량, 누적 PnL

현재 `system_message` channel은 2,700개 turn 모두 null이라 실제 memory input으로 작동하지 않았다. `dim_6`은 해당 turn에서 새로 생성되는 자기점검 output이지 다음 turn에 별도 필드로 저장되는 memory가 아니다. 다음 turn으로 넘어가는 텍스트 상태는 직전 `belief_summary`가 핵심이다.

반면 다음은 없다.

- 사건 단위로 검색 가능한 여러 turn의 장기 기억
- 어떤 뉴스/claim에서 belief가 생겼는지 provenance
- 출처 신뢰도와 correction/invalidation 상태
- 이전 예측과 이후 실제 결과의 연결
- regime 변화에 따른 기억 만료
- reflection이 실제 prediction error에 근거했는지 검증하는 구조

따라서 C00은 `M0 = one-step belief summary + recent-order state`로 정의하는 것이 정확하다.

### 10. belief 로그는 완전하지만 생성 안정성은 별도 문제다

최종 로그에는 belief의 6개 dimension, `belief_summary`, `view_change`가 2,700개 turn 모두 존재한다. agent별 summary exact duplicate도 3개뿐이라 텍스트 다양성은 높다. state 연결도 온전하다.

하지만 생성 과정은 retry에 상당히 의존한다.

- belief retry 발생 turn: 540/2,700 = 20.0%
- decision retry 발생 turn: 209/2,700 = 7.7%
- validation error event: 1,053
  - belief 601
  - decision 292
  - news 130
  - depth2 post-search 17
  - market 13
- OpenRouter requests: 13,516
- API errors: 295 = 2.18%
  - timeout 291
  - JSON decode 3
  - rate limit 1
- 전체 API cost: 약 USD 17.28
- 평균 latency 약 45.6초, p95 약 157초

최종 결과는 복구됐고 deterministic fallback은 0이지만, 시스템 품질 평가에서는 retry rate, timeout rate, latency, token/cost도 함께 보고해야 한다. memory가 길어지면 prompt length와 timeout/retry가 늘 수 있으므로, memory architecture의 효과는 행동 지표뿐 아니라 운영 비용과 실패율도 포함해야 한다.

### 11. 뉴스 provenance 버그는 fake 조건 전에 반드시 고쳐야 한다

모든 turn에 raw selected-news reference가 있지만, 정규화된 source mapping은 거의 사라져 있다.

- raw selected refs: 8,957
- mapped refs: 143
- unmapped refs: 8,814 = 98.4%
- mapped source가 하나라도 있는 turn: 77/2,700

원인은 LLM이 `news_...` ID 문자열을 반환할 때 `normalize_influential_news()`가 이를 ID가 아니라 title처럼 처리하는 경로다. 다행히 raw reference를 각 turn의 visible/search news ID 또는 제목과 다시 대조하면 8,750개, 약 97.7%가 exact match로 복원된다. C00은 offline repair가 가능하다.

이 버그는 C00의 belief text 자체를 없애지는 않지만 다음 분석을 막는다.

- 어떤 source가 belief를 바꿨는지
- news–belief grounding
- source-confidence rubric
- fake가 influential하게 선택됐는지
- correction 후 이전 source가 재사용되는지

fake/community condition을 돌리기 전에 코드 수정과 backfill unit test가 필요하다. 특히 `R7 evidence grounding`과 memory retrieval provenance는 이 mapping이 고쳐진 뒤에만 자동화해야 한다.

## Persona, rubric, embedding 평가

### 12. Persona별 수급 일치율은 exploratory 기술통계다

현재 aggregate persona subgroup의 balanced accuracy는 다음과 같다.

| 그룹 | agents | Balanced accuracy |
|---|---:|---:|
| technical | 16 | 53.9% |
| value | 14 | 58.0% |
| news depth 0 | 10 | 49.7% |
| news depth 1 | 16 | 60.9% |
| news depth 2 | 4 | 54.4% |
| 20대 | 9 | 62.7% |
| 30대 | 18 | 59.1% |
| 40대 | 3 | 59.8% |
| female | 17 | 51.5% |
| male | 13 | 62.7% |

이 차이는 hypothesis generator로만 써야 한다. cell이 작고, persona가 무작위 배정되지 않았으며, agent들이 같은 날짜에 같은 종목과 뉴스를 공유하므로 45일을 각 subgroup의 독립 반복으로 볼 수 없다. 특히 성별·연령 결과를 실제 인간집단 행동으로 표현하면 안 된다.

### 13. 더 논문화할 만한 것은 persona construct validity다

동일 시장 안에서 prompt로 설정한 trait가 의도한 방향의 행동을 만들었는지를 검사하면 더 좋은 신호가 있다.

#### 처분효과 enactment

매수와 매도가 모두 가능한 turn만 사용하고, 직전 평균단가 대비 현재 체결가격이 이익인지 손실인지 구분했다. agent별로 다음 지표를 계산했다.

`P(sell | unrealized gain) − P(sell | unrealized loss)`

| disposition prompt | 평균 gap |
|---|---:|
| low | +15.3%p |
| medium | +31.3%p |
| high | +44.0%p |

agent-level ordinal Spearman은 ρ=0.445, exploratory p=0.0137이다.

#### 위험선호 enactment

양 방향이 가능한 turn에서 `선택수량 / 선택방향 최대가능수량`을 agent별로 평균 냈다.

| lottery/risk prompt | 평균 주문강도 |
|---|---:|
| low | 0.521 |
| medium | 0.577 |
| high | 0.706 |

agent-level ordinal Spearman은 ρ=0.597, exploratory p=0.00050이다.

이 결과는 실제 인간의 처분효과나 위험선호를 재현했다는 증거가 아니다. **prompt에 쓰인 trait를 모델이 행동으로 enact했다**는 내부 construct-validity 신호다. 한 모델·한 seed·비무작위 30명·다중 탐색 분석이라는 한계가 있다.

이를 논문화하려면 동일한 base persona를 복제하고 다른 모든 문구를 고정한 채 단 하나의 trait만 low/medium/high로 바꾸는 counterfactual clone 실험이 필요하다. hold를 허용하고 최소 3–5 seeds와 두 개 이상의 backbone을 써야 한다.

### 14. News depth 조작은 실제 입력량 차이를 만들었다

news depth는 단순 persona label이 아니라 관측 정보량을 바꾼다.

- depth 0: 약 10개 headline, 본문 summary 없음
- depth 1: 약 10개 headline과 약 10개 summary
- depth 2: depth 1 + 추가 검색
  - 360 turns 중 179 turns에서 검색결과 존재
  - turn당 평균 검색결과 1.96개
  - 검색결과가 있던 179 turns 중 160 turns에서 실제 선택뉴스에 검색결과 포함

따라서 depth를 실험 treatment로 발전시킬 수 있다. 단, 현재 assignment는 무작위가 아니므로 C00의 depth별 BA 차이를 정보량의 인과효과로 해석하면 안 된다. 다음에는 같은 base persona를 depth 0/1/2에 무작위 또는 balanced paired assignment하고, 실제 read count와 retrieved-source quality를 manipulation check로 둬야 한다.

### 15. Rubric은 C00 pilot과 처치 실험용을 분리해야 한다

기존 계획의 rubric은 다음처럼 정리할 수 있다.

| Rubric | C00 적용 | 다음 처치 실험 |
|---|---|---|
| R1 claim reception | 불가: fake claim 없음 | factual/fake/correction claim 수용 |
| R2 directional stance | 가능 | pre→post stance 이동 |
| R3 epistemic confidence | 가능 | 과신과 calibration |
| R4 source confidence | provenance repair 후 가능 | source 신뢰도 변화 |
| R5 position conviction | 가능 | target exposure와 연결 |
| R6 risk restraint | 가능 | hold·risk budget과 연결 |
| R7 evidence grounding | provenance repair 후 가능 | claim/source citation 정확성 |
| R8 belief–action consistency | 정의 수정 후 가능 | desired stance와 execution 분리 |

Memory 논문에는 다음 항목을 추가하는 것이 좋다.

- obsolete-memory reuse
- correction uptake
- provenance citation accuracy
- unsupported-memory amplification
- contradiction resolution
- reflection groundedness
- belief revision latency
- stale-regime carryover

R8은 다음 규칙으로 바꿔야 한다.

- desired=hold이고 hold 불가이면 inconsistency가 아니라 `forced_trade_conflict`
- 한 방향만 feasible이면 action consistency는 NA
- 양 방향 feasible일 때만 desired direction과 action을 비교
- quantity/target exposure는 conviction의 별도 차원

코딩 표본은 최소 100개보다 360개가 낫다. 30 agents × 상승/하락/혼조 날짜 각 2일 × AM/PM으로 stratified sample을 만들 수 있다. 두 코더가 condition, persona, 실제 action, 연구가설을 가린 상태에서 먼저 stance와 source grounding을 코딩하고, 이후 action을 결합해야 한다. weighted Cohen κ 또는 Krippendorff α를 보고하고 LLM judge는 human calibration 이후 보조로만 사용한다.

### 16. Embedding은 cluster가 아니라 사전 정의한 trajectory를 측정해야 한다

C00 단독으로 fake bullish↔bearish claim axis의 treatment effect를 측정할 수는 없다. C00에서 가능한 baseline embedding 분석은 다음이다.

- adjacent belief drift: 직전 belief와의 cosine distance
- AM→PM과 PM→다음 AM 변화 분리
- repaired news source와 belief의 grounding similarity
- belief summary와 decision reason의 semantic alignment
- 같은 날짜 agent 간 convergence/dispersion
- persona별 trajectory stability
- bullish / neutral / bearish anchor에 대한 상대 위치

주의할 점은 2,700개 summary 중 2,697개가 고유 문장이라는 것이다. cosine drift가 신념 변화가 아니라 LLM의 표현 바꾸기를 잡을 수 있다. 다음 안전장치가 필요하다.

- `dim_1`, `belief_summary`, `view_change`를 별도로 임베딩
- boilerplate 제거 전후 결과 비교
- 최소 2개 multilingual embedding model
- human rubric stance와 convergent validity
- UMAP은 시각화로만 사용하고 주검정 금지
- agent/event/seed cluster 또는 bootstrap
- persona classifier에서는 나이·전략명·MA20 같은 prompt leakage 제거
- leave-one-agent-out validation

fake/correction 실험에서는 paired factual↔misinformation claim axis를 사전 고정하고, `pre → exposed → next turn → correction → post-correction` 위치 변화를 주결과로 두는 것이 좋다.

## 새로운 논문 방향

### 방향 A — 가장 추천: Memory as a double-edged mechanism

가제:

> **When Memory Helps—and Hurts—LLM Investor Agents: Behavioral Fidelity and Misinformation Persistence under Real Market Context**

핵심 질문:

> source-aware short/long memory와 outcome-based reflection이 실제 개인투자자 flow alignment, persona consistency, evidence grounding을 높이는가? 또는 misinformation과 자기확신을 더 오래 유지시키는가?

이 방향이 좋은 이유는 다음과 같다.

- C00을 shallow-memory operational baseline으로 살릴 수 있다.
- 사용자가 이미 구상한 short/long memory와 reflection이 중심 contribution이 된다.
- 수급 일치율이 오르지 않아도 correction failure나 stale-memory persistence라는 논문 결과가 남는다.
- fake/community 조건, persona, embedding, rubric을 하나의 메커니즘 체계로 묶을 수 있다.
- 단순 trading return 경쟁보다 현재 코드베이스의 강점인 belief log와 synthetic population을 활용한다.

권장 memory arms:

| Arm | 구조 |
|---|---|
| M0 | 현재 C00: one-step belief summary + 최근 주문/portfolio |
| M1 | structured short memory: 최근 1–5 turn의 관측·예측·행동 |
| M2 | M1 + event-level long memory with source/timestamp/status |
| M3 | M2 + outcome-based reflection and correction/invalidation |

Memory record에는 최소한 다음이 있어야 한다.

- event/claim ID
- observed timestamp와 decision cutoff
- source ID와 source type
- factual observation / interpretation / prediction 구분
- confidence
- related action과 target exposure
- 이후 확인된 outcome
- confirmed / contradicted / unresolved / expired 상태
- retrieval reason과 retrieved-at timestamp

reflection은 “내 판단을 돌아봐라”라는 자유문장보다 구조화해야 한다.

1. 이전에 무엇을 예측했는가
2. 어느 horizon의 outcome으로 평가하는가
3. 어떤 근거가 맞고 틀렸는가
4. 새 정보가 이전 claim을 반박했는가
5. memory confidence를 얼마나 갱신하는가
6. 다음 행동 규칙을 어떻게 조정하는가

중요한 것은 reflection에 실제 개인투자자 수급을 정답으로 직접 보여 주면 안 된다는 점이다. 그러면 논문이 인간 flow를 예측하는 것이 아니라 target leakage를 학습하는 구조가 된다. reflection feedback은 agent가 실제로 관측할 수 있는 가격·공시·뉴스 outcome 또는 별도 training window에서만 제공하고, evaluation window의 실제 수급은 숨겨야 한다.

관련 연구는 memory가 에이전트의 행동과 계획에 중요하고 layered financial memory가 trading agent에 쓰일 수 있음을 보여 준다. [Generative Agents](https://arxiv.org/abs/2304.03442)는 experience record, retrieval, reflection을 분리하고 ablation했으며, [Reflexion](https://proceedings.neurips.cc/paper_files/paper/2023/hash/1b44b878bb782e6954cd888628510e90-Abstract-Conference.html)은 feedback 기반 언어적 reflection을 episodic memory에 저장한다. [FinMem](https://ojs.aaai.org/index.php/AAAI-SS/article/view/31290)은 금융정보를 계층적으로 처리하는 memory 구조를 제안한다. 그러나 이 선행연구들이 삼성전자 개인수급 정합성이나 misinformation resilience를 보장하는 것은 아니다. 바로 그 간극이 본 연구의 contribution이 될 수 있다.

### 방향 B — 기존 6개 조건을 살리기: community × directional information

기존 6개 조건이 `community {off,on} × information {clean,bearish fake,bullish fake}`라면, 설계 자체는 논문화 가능하다. 현재 실제 관측은 C00 하나뿐이므로 C00에서 조건 효과를 찾을 수는 없다.

가능한 질문:

> ranked community feed가 방향성 misinformation의 belief deviation, persistence, action translation을 증폭하는가?

주요 설계 원칙:

- fake를 clean news에 추가하지 말고 동일 factual anchor를 matched fake로 교체해 information dose를 맞춤
- bullish/bearish event를 같은 날짜·주제·문장길이·source style로 paired matching
- community off/on에서 같은 agent와 seed를 사용
- depth 0은 community/fake를 보지 못할 수 있으므로 ITT와 eligible-agent effect를 분리
- `read`, `selected`, `influential`은 post-treatment mechanism이므로 primary causal model의 control로 넣지 않음
- community는 일반 social network가 아니라 현재 구현된 ranked shared feed임을 정확히 기술

금융 소셜 네트워크 연구는 social transmission이 뉴스 반영과 거래량·의견분산을 함께 키울 수 있고, selective exposure가 echo chamber를 만들 수 있음을 보여 준다. [News Diffusion in Social Networks and Stock Market Reactions](https://academic.oup.com/rfs/article-abstract/38/3/883/7698199), [Echo Chambers](https://academic.oup.com/rfs/article-abstract/36/2/450/6670640). 이 문헌과 연결하면 단순 “가짜뉴스에 속았다”보다 information diffusion, selective exposure, excessive trading, belief persistence를 다룰 수 있다.

### 방향 C — Synthetic persona construct-validity benchmark

가제:

> **Do LLM Investor Personas Behave as Designed? A Counterfactual Construct-Validity Benchmark**

핵심은 실제 시장 수급을 잘 맞혔는가보다, 동일 환경·동일 자본에서 설정한 behavioral trait가 의도한 행동을 만드는가다.

현재 C00에는 처분효과와 위험선호의 단조 신호가 있어 출발점이 있다. 다음에는 base persona clone을 만들고 하나의 trait만 바꾼다.

- disposition low/medium/high
- lottery/risk low/medium/high
- news depth 0/1/2
- technical/value strategy
- memory/no-memory
- hold/no-hold

주지표는 agent-level behavioral construct score이고, 외부 flow alignment는 secondary다. 장점은 명확한 타당성 논문이 된다는 점이고, 단점은 여러 backbone·seed·자산·market regime가 필요해 실험량이 가장 크다는 점이다.

### 권장 선택

메인은 방향 A가 가장 좋다. 방향 B를 정보환경 robustness extension으로, 방향 C의 construct measures를 평가 모듈로 포함하는 구성이 적절하다.

한 문장 contribution은 다음처럼 만들 수 있다.

> We introduce a source-aware memory and reflection framework for LLM investor agents and show when it improves temporal behavioral fidelity—and when it amplifies misinformation persistence—under time-gated real-market contexts.

## 수급 일치율을 높이기 위한 구조적 방법

Memory만 추가하는 것보다 다음 순서가 중요하다.

### 0순위: price-only baseline과 residual target 고정

현재 가장 큰 개선 여지는 memory보다 먼저, prompt에 이미 있는 가격정보에 대한 개인투자자의 역행 반응을 보존하는 것이다. AM 시가갭 규칙의 BA가 82.8%, PM 당일수익률 규칙이 92.9%인 반면 C00은 각각 55.6%, 59.8%다.

- 모든 실험에 AM opening-gap contrarian와 PM current-return contrarian를 필수 기준선으로 둔다.
- training window에서 가격·거래량만 쓴 logistic/linear baseline을 고정한다.
- agent architecture의 성과는 raw 수급뿐 아니라 `실제 수급 − price-only 예측` residual을 얼마나 설명하는지 평가한다.
- PM contemporaneous rule은 reconstruction용, AM rule은 예측에 가까운 benchmark로 분리한다.
- 실제 평가일 수급을 prompt나 reflection에 넣어 역행 규칙을 학습시키는 leakage는 금지한다.

수급 일치율만 올리는 것이 목표라면 단순 가격 규칙이 이미 LLM보다 훨씬 낫다. 따라서 LLM 연구의 가치는 가격만으로 설명되지 않는 news, persona, memory, correction의 **incremental contribution**을 보이는 데 있어야 한다.

### 1순위: action architecture 수정

- hold 허용
- stance와 quantity 분리
- target exposure 출력
- deterministic feasibility allocator
- constraint와 execution을 별도 로그
- 모든 turn 강제체결 제거 또는 no-trade 보존

이 변경은 agent가 관망하고 싶은 날 억지로 방향 noise를 만드는 것을 줄인다.

### 2순위: 정보시점과 목표 정렬

- 예측이면 AM cutoff를 primary로 사용
- reconstruction이면 PM 포함을 명시
- news/market/price 각각 observed-at timestamp 저장
- t 시점 prompt에서 t 이후 정보가 들어가지 않는 time gate test
- 실제 개인수급을 reflection input에서 제외

### 3순위: population calibration

- 실제 개인투자자 순매수 비율에 맞추려고 직접 target을 주입하지 않음
- 별도 train period에서 agent별 inactivity, turnover, buy/sell propensity를 calibration
- hold frequency, order-size distribution, turnover distribution을 현실 자료에 맞춤
- wealth distribution과 initial holdings를 현실적으로 sampling
- 평가 기간은 완전히 holdout

### 4순위: source-aware memory와 reflection

- short memory는 최근 관측과 미해결 prediction
- long memory는 사건·claim·source·outcome 중심
- confidence decay와 expiry
- correction/invalidation
- retrieval provenance
- outcome-grounded reflection
- contradictory evidence를 함께 retrieval

### 5순위: heterogeneous model ensemble

30명이 같은 backbone과 비슷한 prompt style을 쓰면 aggregate order가 동조하기 쉽다. 다음을 제한적으로 ablate할 수 있다.

- 2–3 backbone mixture
- temperature/decision-threshold heterogeneity
- persona별 attention/retrieval policy 차이
- correlated common news와 idiosyncratic source exposure 분리

다만 모델 ensemble이 무조건 현실성을 높이는 것은 아니다. 실제로는 calibration loss와 behavioral moments를 기준으로 선택해야 한다.

### 6순위: 학습 가능한 aggregation

현재 모든 agent order를 그대로 합산한다. 별도 training period에서 다음 weighting을 학습할 수 있다.

- agent reliability
- regime-specific weight
- persona cell population weight
- confidence-calibrated target exposure

하지만 evaluation 날짜의 실제 수급으로 weight를 조정하면 leakage다. 모든 weight는 train/validation/test를 시간순 분할해 고정해야 한다.

## 권장 실험 설계

### Phase 0 — 코드와 데이터 gate

다음 자동 gate를 모두 통과하기 전에는 비싼 API 실험을 시작하지 않는 것이 좋다.

- run scope 100% 또는 사전 정의된 cutoff
- global turn continuity
- portfolio transition mismatch 0
- duplicate/missing agent-turn 0
- news ID mapping ≥99.5%
- fake delivery unit tests
- time leakage tests
- hold/intention/allocator fields 존재
- deterministic fallback rate 사전 기준 이하
- retry/error/cost summary 자동 생성
- partial chunk가 validation에 합쳐지지 않음

### Phase 1 — clean-news memory ablation

community off, fake off에서 다음을 비교한다.

- M0 current shallow state
- M1 structured short memory
- M2 short + long memory
- M3 short + long + reflection

최소 3 seeds, 권장 5 seeds다. 같은 agent/date/event를 arm 간 paired하게 유지한다. primary는 balanced accuracy 하나가 아니라 다음 묶음이다.

- AM-only direction BA
- AM opening-gap contrarian 대비 incremental BA
- PM을 보고할 경우 current-return contrarian 대비 incremental BA
- signed-flow Spearman
- standardized flow MAE
- direction-switch recall
- hold rate와 turnover
- source-grounding
- belief temporal consistency
- calibration과 Brier score, 확률 출력 시
- runtime retry, latency, cost

### Phase 2 — factual/fake/correction resilience

M0과 Phase 1의 best architecture를 비교한다.

- matched factual
- matched misinformation
- misinformation followed by correction

주지표:

- immediate belief shift
- next-turn persistence
- correction recovery
- unsupported claim repetition
- obsolete-memory reuse
- source confidence update
- belief→intention→execution translation

### Phase 3 — community extension

Phase 2에서 효과가 확인된 architecture만 community off/on에 넣는다. 처음부터 community, fake valence, memory, persona를 전부 full factorial로 돌리면 비용과 해석이 모두 폭발한다.

### 통계 설계

- agent-turn 2,700개를 독립 관측치로 세지 않음
- 동일 date/event/agent의 paired contrast
- date-level moving-block bootstrap
- seed를 독립 반복 단위로 포함
- mixed model의 random intercept: agent, event/date, seed
- rubric ordinal outcome은 cumulative-link mixed model
- action은 hold 포함 multinomial mixed model
- persona interaction은 2–3개만 사전 지정
- 나머지는 exploratory로 FDR 보정
- 모델·hyperparameter 선택은 train/validation, 최종 claim은 untouched test window

[InvestorBench](https://aclanthology.org/2025.acl-long.126/)가 강조하는 것처럼 금융 agent 평가는 다양한 환경과 일관된 benchmark가 필요하다. 현재 실험도 single aggregate return보다 동일한 event/time gate와 multi-metric protocol을 고정하는 쪽이 더 경쟁력이 있다.

## PnL과 거래 성과의 위치

최종 agent 수익률은 평균 8.51%, 중앙값 8.43%, 최소 -2.05%, 최대 18.07%다. 같은 첫 시가에서 마지막 종가까지 단순 buy-and-hold는 약 10.71%다. 현재 평균 agent는 buy-and-hold보다 낮고, 거래비용·슬리피지·미체결이 없다. 또한 45일 partial scope다.

PnL을 주결과로 쓰기 어려운 이유는 다음과 같다.

- 실제 가격이 외생적이고 agent order가 가격에 영향을 주지 않음
- 모든 주문 전량 체결
- 수수료와 market impact 없음
- hold 부재로 turnover 과다
- 한 종목·한 기간·한 seed
- 예측과 reconstruction timing 혼재

따라서 PnL은 secondary sanity check로 두고, 주결과는 behavioral fidelity, misinformation resilience, calibration, action feasibility가 더 적절하다.

## C00 로그를 읽는 순서

전체 상세 로그는 최신 branch의 `outputs/logs/paper_0721/c00_commoff_fakeoff/`에 있다. 해석 순서는 다음이 좋다.

1. `run_metadata.json`  
   실행 코드 commit, 모델, 비용, retry, 설정을 확인한다.
2. `run_complete.json` + `integrity_report.json` + `checkpoint.json` + `paused.json`  
   정상완주인지, 어떤 범위가 봉인됐는지, partial chunk가 있는지 판정한다. `paused.json`의 4월 29일 실패는 이후 복구됐으므로 5월 4일 cutoff 원인과 분리한다.
3. `agent_turns.jsonl`  
   한 turn의 context → visible/read news → interpretation → belief six dimensions → market analysis → decision → submitted order를 연결한다. 텍스트 분석의 원본이다.
4. `agent_turns.csv`  
   2,700개 turn의 action, quantity, feasibility, retry, belief summary를 정량 집계한다.
5. `submitted_orders.csv` → `exchange_fills.csv` → `portfolio_updates.jsonl`  
   의도, 주문, 체결, 다음 state가 맞는지 추적한다.
6. `daily_exchange_summary.csv` / `daily_exchange.jsonl`  
   agent flow를 날짜 단위로 집계한다.
7. `validation_output.json`과 validation folder의 `daily_comparison_value.csv`  
   실제 개인투자자 flow와 날짜별 비교한다.
8. `openrouter_calls.jsonl` + `llm_validation_errors.jsonl`  
   retry, timeout, invalid schema가 특정 날짜·persona·news depth에 집중되는지 본다.
9. `news_input_audit.json`  
   news window, visible/read/search item과 cutoff를 검사한다.
10. community 관련 CSV  
    C00은 community off이므로 비어 있거나 inactive인 것이 정상이다. 효과 로그로 해석하지 않는다.

`chunks/`는 root log와 별개 독립 표본이 아니다. resume/audit용 분할 파일이고, 선언된 45일 root log에 이미 포함된 chunk를 다시 합산하면 중복된다. 5월 6일 partial chunk는 `run_complete.json`의 봉인 범위 밖이므로 main analysis에 넣으면 안 된다.

## 방법론과 재현 파일

이번 재분석은 다음 원칙을 사용했다.

- 최신 branch를 detached worktree로 분리해 현재 workspace branch와 혼합하지 않음
- `run_complete.json`의 sealed scope만 사용
- fills에서 signed value/volume을 재계산
- 실제 개인투자자 일별 value flow와 날짜 exact join
- direction match, class recalls, balanced accuracy, Pearson, Spearman 재계산
- AM/PM 및 burn-in sensitivity 계산
- Wilson interval과 day-stratified bootstrap을 진단용으로 계산
- feasibility와 1-share/max-quantity 메커니즘 집계
- portfolio transition과 belief continuity 검사
- agent별 average cost를 full-fill trace에서 재구성해 처분효과 enactment 측정
- selected-news raw/mapped/unmapped item을 전수 집계

재현물:

- `analyze_latest_c00.py`: 원본 로그에서 표와 JSON을 재생성
- `latest_c00_review.ipynb`: 실행된 companion notebook
- `outputs/audit_summary.json`: 핵심 감사 결과
- `outputs/headline_metrics.csv`: headline metrics
- `outputs/sensitivity_metrics.csv`: AM/PM, burn-in sensitivity
- `outputs/decision_time_price_baselines.csv`: AM 시가갭·PM 당일수익률 역행 기준선
- `outputs/decision_feasibility.csv`: action-space 분해
- `outputs/persona_group_metrics.csv`: persona 기술통계
- `outputs/persona_construct_agent_scores.csv`: agent-level construct score
- `outputs/persona_construct_group_metrics.csv`: trait level 요약
- `outputs/llm_api_summary.csv`: runtime/provider 요약
- `outputs/daily_flow_comparison.csv`: 일별 재계산 데이터

## 한계와 robustness

- C00 하나뿐이라 6조건 treatment effect는 전혀 추정할 수 없다.
- 45일 partial run이고 seed가 하나다.
- date series에 자기상관이 있으며 45일을 완전 독립 표본으로 볼 수 없다.
- 30명은 A001–A030 deterministic cohort이고 전원 1억 원이다.
- hold가 없고 full fill이라 action distribution이 구조적으로 왜곡된다.
- PM은 same-day information을 사용한다.
- selected-news provenance가 현재 mapping bug로 깨져 있다.
- 한 종목, 한 market regime, 한 backbone 중심이다.
- persona construct 결과는 prompt enactment이지 human validity가 아니다.
- embedding과 rubric은 아직 C00 전체에 실제 실행된 결과가 아니라 평가 설계다.
- 현재 실험은 market clearing이나 endogenous price formation을 구현하지 않는다.
- C00은 prompt-visible price contrarian baselines에 크게 뒤진다.

## 추천 실행 순서

1. selected-news ID normalization 수정과 C00 offline backfill
2. hold + target exposure + deterministic allocator 구현
3. AM/PM information-time contract와 leakage test 고정
4. M0/M1/M2/M3 memory schema 구현
5. clean-news 2–3일 smoke run으로 integrity, cost, retry 확인
6. 45–63일 clean-news multi-seed ablation
7. rubric human calibration과 embedding baseline
8. factual/fake/correction paired event 실험
9. 효과가 확인된 architecture만 community on/off 확장

## 남은 연구 질문

- memory는 방향 일치보다 belief inertia를 먼저 높이는가?
- outcome-based reflection은 calibration을 개선하면서 persona consistency를 약화시키는가?
- long memory가 correction을 보존하는가, 최초 misinformation을 보존하는가?
- hold를 추가하면 aggregate signed flow의 방향성이 좋아지는가, 단순히 turnover만 줄어드는가?
- news depth 효과는 정보량 때문인가, 검색 결과의 질과 선택 메커니즘 때문인가?
- persona trait enactment는 backbone을 바꿔도 유지되는가?
- source provenance를 강제하면 belief grounding은 좋아지지만 행동 다양성은 줄어드는가?
- community가 정보반영 속도를 높이는 동시에 excessive trading과 belief polarization을 키우는가?

## 최종 판단

현재 C00의 가장 정직한 논문용 문장은 다음이다.

> Post-0720 C00 provides a state-continuous 45-trading-day operational baseline for 30 equal-endowment LLM investor agents. It generates two-sided order-flow variation but underperforms simple price-contrarian rules using information already visible at each decision time; its reported value correlation is largely an initialization artifact, and its PM result reflects same-day reconstruction rather than clean ex-ante prediction.

그리고 다음 논문의 방향은 다음이 가장 좋다.

> 이 baseline 위에서 source-aware short/long memory와 outcome-based reflection을 인과적으로 ablate하고, 그것이 behavioral fidelity를 높이는 조건과 misinformation을 고착시키는 조건을 함께 밝힌다.

이렇게 가면 현재의 60%가 기대보다 낮아도 실패가 아니다. “memory가 언제 도움이 되고 언제 해로운가”라는 메커니즘 결과가 남고, 기존 persona·embedding·rubric·community 조건도 모두 그 질문 아래에서 역할을 갖게 된다.
