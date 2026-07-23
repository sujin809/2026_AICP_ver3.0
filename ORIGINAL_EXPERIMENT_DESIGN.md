# 삼성전자 LLM 개인투자자 정보환경 실험: 본래 설계 명세

## 0. 문서 목적과 기준

이 문서는 논문을 위해 처음 정의한 **3개월·6조건 실험의 연구 목적, 처치, 에이전트, 뉴스, 커뮤니티, 거래, 로그, 검증 및 분석 규칙**을 한곳에 고정한다.

문서에서는 다음 세 상태를 구분한다.

- **설계 의도**: 논문에서 원래 비교하려던 실험
- **현재 구현**: 2026-07-21 로컬 코드가 수행하도록 작성된 방식
- **기존 실행**: 2026-07-15 완료 로그에서 실제 관측된 방식

본래 기간은 2026-02-27부터 2026-06-01까지다. 2026-04-30까지만 사용하는 2개월 안은 이후 제안된 축약안이며, 이 문서의 본래 설계를 자동으로 대체하지 않는다.

관련 운영 문서:

- [0720 코드 인수인계](EXPERIMENT_0720_HANDOFF.md)
- [0720 실행 절차](EXPERIMENT_0720_RUNBOOK.md)
- [연구 계획](twinmarket_micro_behavior_research_plan.md)
- [가짜뉴스 DB 안내](Event_Fake_News_DB_Guide.md)
- [belief deviation rubric](analysis/belief_event_study/belief_deviation_rubric.md)
- [total deviation 정의](analysis/belief_event_study/total_deviation_spec.md)
- [embedding 분석 계획](analysis/belief_event_study/embedding_analysis_plan.md)

---

## 1. 연구 목적과 주장 범위

### 1.1 연구 대상

본 연구는 LLM 개인투자자만으로 시장가격을 생성하거나 실제 시장 전체를 재현한다고 주장하지 않는다. 삼성전자의 실제 가격 경로를 모든 조건에 동일한 외생 환경으로 제공하고, 그 환경에서 LLM 개인투자자 집단이 보이는 다음 결과를 분석한다.

1. 실제 개인투자자의 일별 순매수·순매도 방향과의 정합성
2. 커뮤니티가 belief와 거래 행동을 바꾸는 방식
3. bullish·bearish 허위정보가 belief와 거래 행동을 왜곡하는 방식
4. 정보환경 효과가 하락장과 상승장에서 달라지는지
5. persona와 정보 접근 깊이에 따라 취약성이 달라지는지

### 1.2 검증 대상

- 가격 경로의 stylized fact 재현은 주검증 대상이 아니다.
- 주검증 대상은 실제 가격과 실제 뉴스에 대한 **집계 개인행동 정합성**이다.
- 기준선 검증 이후 커뮤니티와 가짜뉴스의 처치 효과를 본다.
- 수익률은 보조 결과이며 행동 정합성을 대신하지 않는다.
- simulation 안에서의 처치 효과를 실제 인간 투자자의 인과효과로 직접 일반화하지 않는다.

### 1.3 연구 질문

- **RQ1 행동 기준선**: 실제 가격과 실제 뉴스만 주어졌을 때 simulated retail 집단의 일별 순거래 방향이 실제 개인투자자 방향과 얼마나 일치하는가?
- **RQ2 커뮤니티 효과**: 공통 종목토론방이 belief, 방향, 강도, 회전율, 수익률 및 집단 수렴을 어떻게 바꾸는가?
- **RQ3 허위정보 효과**: bullish·bearish synthetic misinformation이 belief와 주문을 어느 방향으로 얼마나 이동시키는가?
- **RQ4 국면 조절**: 위 효과가 하락장과 상승장에서 비대칭적인가?
- **RQ5 이질성**: persona, 정보 depth, 초기자본 및 시간가변 현금 제약에 따라 정보 취약성이 달라지는가?

---

## 2. 대상 시장과 기간

| 항목 | 본래 설정 |
|---|---|
| 종목 | 삼성전자 보통주 `005930` |
| 전체 기간 | 2026-02-27~2026-06-01 |
| 거래일 | 63일 |
| 일중 turn | AM·PM 두 번 |
| 총 turn | 126 |
| 하락장 | 2026-02-27~2026-03-31, 22거래일 |
| 상승장 | 2026-04-01~2026-06-01, 41거래일 |
| 하락장 기준선 분석 | 초기 적응 3거래일 제외 후 19일 |
| 상승장 기준선 분석 | 별도 제외 없이 41일 |

국면은 실험 결과를 본 뒤 자동 탐지하는 것이 아니라 사전에 정한 날짜 구간이다. 국면 자체는 처치가 아니라 정보환경 효과의 조절 변수다.

### 2.1 이후 제안된 2개월 축약안

분석과 나머지 실험을 2026-04-30에 종료하면 44거래일이며, 하락장 22일과 상승장 22일이 된다. 이 경우 fake 주입은 원래 30일이 아니라 해당 기간 안의 21일만 남는다. 따라서 2개월 실험을 채택하면 기간·주입 수·통계적 검정력을 별도 명세로 갱신해야 한다.

---

## 3. 외생 가격과 일중 정보 시점

### 3.1 가격

- 가격은 에이전트 주문으로 형성하지 않는다.
- AM은 해당 거래일의 실제 시가를 공시가격으로 사용한다.
- PM은 해당 거래일의 실제 종가를 공시가격으로 사용한다.
- 에이전트는 호가를 제출하지 않는다.
- 정상 주문은 공시가격에 전량 체결한다.
- 모든 조건이 동일한 가격 경로를 사용한다.

### 3.2 하루의 순서

```text
당일 AM 정보 cutoff
→ AM 뉴스 해석·belief 갱신·buy/sell·체결
→ 당일 PM 정보 cutoff
→ PM 뉴스 해석·belief 갱신·buy/sell·체결
→ community on일 때 게시·열람·반응·Best 5 확정
→ 다음 거래일 AM에서 전날 community 정보 1회 반영
```

당일 장 마감 뒤 만들어진 커뮤니티 글은 당일 AM·PM 주문에 영향을 줄 수 없다. 다음 거래일 AM부터만 사용한다.

### 3.3 정보 cutoff

- AM 컨텍스트: 직전 거래일 15:30 이후부터 당일 08:59까지
- PM 컨텍스트: 당일 08:59 이후부터 15:30까지
- Depth 2 추가 검색도 해당 판단 시각 이후 기사를 포함할 수 없다.
- 미래 가격, 미래 뉴스, fake 라벨, 다른 agent의 비공개 상태를 입력에 노출하지 않는다.

---

## 4. 6개 조건의 완전 요인 구성

가짜뉴스는 `없음`, `bearish`, `bullish`의 3수준이며 커뮤니티는 `off`, `on`의 2수준이다. 따라서 총 6조건이다.

| 현재 launcher ID | Community | Fake news | 역할 |
|---|---:|---|---|
| `c00_commoff_fakeoff` | off | 없음 | 행동 기준선 |
| `c10_common_fakeoff` | on | 없음 | 순수 커뮤니티 효과 |
| `c01_commoff_bearish` | off | bearish | 커뮤니티 없는 bearish 효과 |
| `c11_common_bearish` | on | bearish | bearish × community 상호작용 |
| `c02_commoff_bullish` | off | bullish | 커뮤니티 없는 bullish 효과 |
| `c12_common_bullish` | on | bullish | bullish × community 상호작용 |

### 4.1 조건 코드 주의

2026-07-15의 기존 4조건 분석에서는 `C11`을 `community on + bullish` 의미로 사용한 적이 있다. 현재 6조건 launcher에서는 그 조건이 `c12_common_bullish`이고, `c11_common_bearish`는 `community on + bearish`다. 논문·분석 코드에서는 짧은 코드만 쓰지 말고 항상 community와 fake variant를 함께 기록한다.

### 4.2 조건 간 고정해야 하는 항목

- 동일 기간과 거래일
- 동일 30명 cohort와 agent id
- 동일 persona DB hash
- 동일 초기 belief·portfolio base
- 동일 seed
- 동일 실제 가격·실제 뉴스
- 동일 Qwen 모델과 prompt tree
- 동일 코드 commit
- 동일 정보 cutoff와 거래 제약
- 동일 per-run·global concurrency 정책

---

## 5. 에이전트 모집단과 persona

### 5.1 persona 원천

원천은 `outputs/sys_100.db`의 100명 고정 persona다. persona에는 다음 특성이 포함된다.

- 성별, 나이, 연령대, 지역
- 가치·기술 전략
- 처분효과, 복권선호, 과거수익, 저분산 등 행동 특성
- 전통적 투자 숙련도와 follower/influencer 관련 속성
- 뉴스 depth 0·1·2
- 초기자본 1억·10억
- persona prompt와 자기 설명

### 5.2 본래 초기자본 설계

| 초기자본 | 모집단 인원 | 모집단 비율 |
|---:|---:|---:|
| 1억 원 | 90명 | 90% |
| 10억 원 | 10명 | 10% |

10억 agent는 A039, A043, A052, A056, A063, A071, A080, A082, A087, A097이다. 모두 초기 보유주식 없이 현금만 가진다.

30명 실험에서도 이 비율을 유지하려는 연구 의도라면 **1억 27명 + 10억 3명**이어야 하며, 여섯 조건 모두 동일한 세 명의 10억 persona를 사용해야 한다.

### 5.3 현재 구현에서 발견된 cohort 불일치

현재 `select_simulation_agents(30)`은 agent id로 정렬된 앞 30명 A001~A030을 선택한다. 첫 10억 agent가 A039이므로 현재 cohort는 다음과 같다.

| 구분 | 1억 | 10억 |
|---|---:|---:|
| 본래 100명 모집단 | 90 | 10 |
| 현재 30명 실행 cohort | 30 | 0 |

따라서 기존 완료 로그로 초기자본 1억 대 10억 취약성을 비교할 수 없다. 자세한 증거는 [현금·초기자본 진단 보고서](analysis/cash_wealth_diagnostic/report.html)에 있다.

### 5.4 현재 30명 cohort의 실제 구성

현재 A001~A030 기준 구성은 다음과 같다.

- 뉴스 depth 0: 10명
- 뉴스 depth 1: 16명
- 뉴스 depth 2: 4명
- 가치 전략: 14명
- 기술 전략: 16명
- 연령: 20대 9명, 30대 18명, 40대 3명
- 초기자본: 전원 1억 원

이는 원래 100명 persona 분포의 축소 표본이라기보다 정렬된 앞 30명이다. 자산·연령별 취약성을 논문 핵심축으로 사용하려면 층화 cohort를 먼저 확정해야 한다.

---

## 6. 뉴스 depth와 실제 뉴스

### 6.1 실제 뉴스 후보

- 모든 조건에서 동일한 실제 뉴스 후보를 유지한다.
- 각 AM/PM slot은 실제 뉴스 최대 10개다.
- 원천 자료가 9개뿐인 2026-03-23 PM은 모든 조건에서 실제 뉴스 9개를 사용한다.
- fake-on 조건도 실제 뉴스를 삭제하거나 교체하지 않는다.

### 6.2 depth별 입력과 활동

| Depth | 실제 뉴스 | 추가 검색 | 커뮤니티 |
|---:|---|---|---|
| 0 | 후보 headline 전부, 본문 0 | 없음 | 게시·열람 미참여 |
| 1 | 후보 headline과 본문 전부 | 없음 | 게시 가능, 본문 최대 5개 선택 열람 |
| 2 | 후보 headline과 본문 전부 | 판단시점 이전 직전 7일, 결과 최대 10개 | 게시 가능, 본문 최대 10개와 작성자 최근 거래·포트폴리오 맥락 |

현재 설계의 `selected_news`는 본문을 읽기 전에 고른 뉴스가 아니다. 모든 허용 본문을 받은 뒤 에이전트가 판단에 영향을 줬다고 지목한 **influential news**다.

### 6.3 Depth 2 순서

```text
기본 headline·본문 확인
→ pre-search: 핵심 발견, 궁금증, 검색 이유, 키워드 3~8개
→ cutoff 이전 직전 7일 검색 결과 최대 10개
→ post-search: 새 발견, 관점 강화·수정·반전·유지, 미해결 질문
→ 뉴스 해석·belief·주문
```

과거에 주입된 fake는 게시 시점 이후 7일 검색에서 재노출될 수 있다. feed 노출과 검색 재노출은 별도 로그로 구분한다.

---

## 7. 가짜뉴스 처치

### 7.1 기본 원칙

- 실제 뉴스는 그대로 유지한다.
- fake는 기존 실제 뉴스에 한 건을 `append`한다.
- 주입 slot은 통상 `실제 10 + fake 1`이다.
- agent에게 `is_fake`, synthetic id, event id, 왜곡 방식, 탐지 단서를 보여주지 않는다.
- private metadata에서는 fake id·event·phase·왜곡 방식·승인 상태를 유지한다.
- bullish와 bearish는 같은 factual anchor와 phase를 사용하고 가치·수요·생산·수익성 귀결 방향만 반대로 만든다.

### 7.2 본래 3개월 주입 일정

- bullish 30건, bearish 30건
- variant별 30개 서로 다른 날짜/slot
- 실험 주입은 2026-05-31에 종료
- 2026-06-01은 추가 주입 없는 관찰일

### 7.3 사건 예측 가능성에 따른 phase

- 예측 가능한 사건: D-2, D-1, D0, D+1, D+2
- 예측 불가능 사건: D0, D+1, D+2
- phase 의미: 사전 관측 → 전망 확산 → 관련 소식 → 후속 관측 → 시장 반영

예측 불가능 사건에 미래 결과를 사전 기사처럼 넣지 않는다. 각 자극은 factual anchor, 허용 범위, 금지 추론을 가진다.

### 7.4 왜곡 방식

- 오해·루머 사실화
- 확정·수치 조작
- 선택적 맥락 강조

본래 30개 자극은 각 왜곡 방식을 10개씩 사용하도록 설계됐다.

### 7.5 노출과 수용 로그

- headline 후보에 포함됐는지
- 본문에 포함됐는지
- Depth 2 검색에서 재노출됐는지
- 판단에 영향 뉴스로 지목됐는지
- 이후 belief가 수용·강화·완화·반박했는지
- 같은 turn과 다음 turn 주문이 어떻게 변했는지
- 커뮤니티 게시글을 거쳐 다음 날 다른 agent에게 전달됐는지

조건 배정이 assignment effect의 주분석이며, 읽은 사람과 읽지 않은 사람의 차이는 내생적 선택이므로 메커니즘 분석으로만 사용한다.

### 7.6 승인 상태

현재 원천 bullish·bearish 60개는 `leakage_safe=true`이고 agent-visible fake label을 제거하지만 `final_approval=false`인 review 자극이다. 최종 논문 실험 전에 사람 승인 여부를 확정해야 한다. 승인 없이 사용하면 논문에서 `leakage-safe synthetic review stimulus`로 한정한다.

---

## 8. 커뮤니티 처치

### 8.1 구조

- 팔로우·친구 관계가 없는 삼성전자 단일 공개 게시판
- 30명 agent가 같은 날의 같은 후보 게시글 metadata를 본다.
- 개인화 추천, 성향 기반 필터, follower feed가 없다.
- 따라서 결과를 소셜 네트워크 확산이나 homophily 효과로 부르지 않는다.

### 8.2 게시와 열람

- Depth 0은 커뮤니티에 참여하지 않는다.
- Depth 1·2는 PM 체결 뒤 게시 여부를 자율 결정한다.
- 게시 유형: 감상, 질문, 거래공유, 수익공유, 분석, 칼럼
- 후보 목록에서 자기 글은 제외한다.
- 후보 metadata: 익명 코드, 유형, 제목, like/unlike, 점수, 작성자 배지
- 후보는 기본적으로 post id 순서로 제시한다.
- Depth 1은 본문 최대 5개, Depth 2는 최대 10개를 선택해 읽는다.

### 8.3 반응과 Best 5

- `like`: +1
- `unlike`: -1
- `read`: 0
- 정렬: score 내림차순 → like 수 내림차순 → post id 오름차순
- 당일 Best 5를 다음 거래일 참여 agent에게 공통 제공한다.

### 8.4 배지

- 상위 수익자: 해당 시점 수익률 상위 20%
- 자산가: 해당 시점 총자산 상위 20%
- 커뮤니티 인플루언서: 누적 like 상위 20%

`자산가` 배지는 초기자본 10억 persona 라벨이 아니라 그 시점의 상대적 총자산 순위다.

### 8.5 시간적 인과 경계

PM 체결 이후의 게시·읽기·반응은 같은 PM 주문의 원인이 아니다. 효과는 다음 거래일 AM 이후에만 연결한다. 통계적 mediation이나 실제 repost cascade는 현재 구조로 주장하지 않는다.

---

## 9. belief·분석·거래 파이프라인

### 9.1 agent-turn 처리 순서

```text
가격·시장 특징·포트폴리오·주문 이력 수집
→ depth별 뉴스 처리
→ 전날 커뮤니티 thinking(해당 시)
→ 뉴스 interpretation
→ belief 6차원 갱신
→ market analysis
→ buy/sell와 수량 결정
→ 실제 공시가격 체결
→ portfolio·belief·trade log 저장
```

### 9.2 belief 출력

- `dim_1`: 단기 가격 방향
- `dim_2`: 가치평가·내재가치
- `dim_3`: 거시·산업 환경
- `dim_4`: 수급·시장심리·타 투자자
- `dim_5`: 뉴스와 사건에 대한 반응
- `dim_6`: 자기 성찰·위험관리
- `belief_summary`: 표현된 종합 belief
- `view_change`: 직전 관점 대비 변화

belief는 잠재적 인간 심리 그 자체가 아니라 모델이 표현한 상태다.

### 9.3 뉴스 interpretation과 confidence

- 영향 뉴스 id
- 뉴스 감성
- 단기·장기 영향
- persona 관점 해석
- confidence
- 판단 근거

뉴스 interpretation의 confidence, market analysis confidence 및 후속 rubric 기반 확신도는 서로 구분한다.

---

## 10. 거래 규칙과 포트폴리오 제약

### 10.1 action 공간

- `buy_sell_only`
- hold 없음
- preferred-direction 별도 단계 없음
- 공매도 없음
- 보유 수량보다 많이 매도할 수 없음
- 주문 최소 단위 1주

### 10.2 현금과 수량

- 1회 매수에 사용할 수 있는 금액은 현재 현금의 최대 50%
- 최대 매수 수량은 `floor(현재 현금 × 0.5 ÷ 공시가격)`
- 최대 매도 수량은 현재 보유량
- 정상 LLM이 선택한 1주 주문은 허용
- malformed 응답을 1주 매도로 바꾸던 deterministic fallback은 현재 제거
- buy와 sell 모두 불가능하면 임의 주문을 만들지 않고 phase를 pause

### 10.3 현금 제약의 해석

현재 현금 잔액은 belief뿐 아니라 feasible action과 수량을 직접 바꾼다. 예를 들어 낙관적 belief를 유지하면서도 현금 부족 때문에 1주만 매수하거나, buy가 불가능해 1주를 매도할 수 있다. 따라서 매도 방향을 bearish belief와 동일시하지 않는다.

초기자본 집단과 시간가변 현금비중은 별도 변수다.

- 초기자본: 사전에 정해진 persona 특성
- 현재 현금비중: 이전 주문과 가격 변화의 결과인 내생적 상태

---

## 11. 모델, seed와 동시성

| 항목 | 설정 |
|---|---|
| 모델 | `qwen/qwen3.5-flash-02-23` |
| 커뮤니티 모델 | 같은 Qwen 모델 |
| 실험 seed | 2 |
| agent worker | 조건당 30 |
| OpenRouter 전역 동시 호출 상한 | 컴퓨터 전체 합계 16 |
| 조건 병렬 실행 | launcher에서 최대 6, C00 이후 나머지 5만 선택 가능 |
| LLM 통신 재시도 | 최대 6회, 일시 오류만 backoff |
| structured-output 검증 | 해당 단계 최대 4회 |
| process 자동 재시작 | 기본 최대 5회, 일시 장애만 허용 |

worker 30은 동시에 준비 가능한 agent task 수다. 실제 OpenRouter 요청은 모든 조건 합계 16개를 넘지 않도록 공유 슬롯으로 제한한다. 응답 도착 순서가 게시글 id나 reaction 순서를 바꾸지 않도록 저장 순서를 agent id로 고정한다.

### 11.1 seed의 의미

모든 조건이 seed 2를 사용하고 각 LLM 호출 seed는 agent id, global turn, 호출 단계에서 안정적으로 파생한다. 같은 seed가 외부 provider의 완전한 bitwise 동일 응답을 보장하는 것은 아니지만 조건 간 불필요한 무작위 차이를 줄인다.

---

## 12. 연속 실행·checkpoint·오류 복구

### 12.1 원칙

실험은 논리적으로 63일 연속이다. chunk는 상태를 초기화하는 단위가 아니라 장시간 API 실행을 복구하기 위한 로그·검증 단위다.

- 조건별 runtime DB 하나를 전체 기간 동안 계속 사용
- global turn 1~126 유지
- 날짜마다 AM, PM, community phase 경계
- phase 시작 전 DB snapshot과 로그 offset 저장
- 성공 phase만 checkpoint 완료 처리
- 실패 시 DB와 로그를 phase 시작점으로 rollback
- 같은 명령으로 마지막 완료 phase 다음부터 resume
- 청크 없이 실행하려면 `--chunk-days 0`, 논문 기본 운용은 `1`

### 12.2 오류 정책

- timeout, 연결 오류, 408·409·429·5xx: 재시도·backoff 후 필요 시 process resume
- 인증, 모델 불일치, schema 불일치, 로컬 무결성 오류: 자동 무한 반복 금지
- JSON/schema 실패: 임의 기본 belief·주문을 만들지 않음
- 같은 phase의 병렬 task가 모두 끝난 뒤 전체 phase rollback
- 같은 output/DB를 두 프로세스가 동시에 수정하지 못하도록 lock

---

## 13. clean base와 조건 격리

- clean base에는 StockData와 100명 전원의 turn 0 belief·portfolio만 둔다.
- 초기 보유주식, 과거 PnL, turn 1 이상 belief·portfolio, trade, community, recovery message를 제거한다.
- 각 조건은 같은 base hash에서 독립 runtime DB를 복사한다.
- 조건별 DB와 output directory를 분리한다.
- 기존 과거 로그는 새 실험 결과에 자동 합쳐지지 않는다.
- 같은 study root의 완료 조건은 기간, seed, agent ids, 모델, prompt/code/base hash 및 concurrency가 일치해야 한다.

---

## 14. 필수 로그와 완료 판정

### 14.1 agent-turn 로그

- agent id와 persona 속성
- date, global turn, AM/PM
- 가격·시장 특징과 정보 cutoff
- 결정 전 portfolio와 주문 이력
- visible/read/search/influential news id
- fake 접촉 단계와 private provenance
- community context
- 뉴스 interpretation과 confidence
- belief 6차원, summary, view change
- market analysis
- trading constraints
- decision, 수량, reason, risk control
- 최대 매수·매도 가능 수량
- 1주 주문 원인
- 제출 주문과 체결 결과

### 14.2 커뮤니티 로그

- 게시 여부와 게시글
- 각 agent에게 보인 후보 목록
- 선택 열람 목록
- like/unlike/read
- 작성자 배지와 Depth 2 추가 맥락
- Best 5 산출 순서
- 다음 날 community thinking

### 14.3 API·validation 로그

- 요청·반환 모델, provider, request id
- stage, 파생 seed, 시도 횟수, latency, token usage
- prompt hash
- 오류 유형
- schema 실패 응답의 제한된 excerpt와 response hash
- API key와 전체 prompt 원문은 저장하지 않음

### 14.4 완료 조건

- 모든 거래일의 AM·PM·community phase 완료
- agent×turn belief·portfolio·trade 행 완전성
- 중복·누락·out-of-cohort 행 없음
- 음수 현금·음수 보유량 없음
- submitted order와 fill 수량 일치
- deterministic fallback 0건
- 모델 감사 통과
- `run_complete.json`과 integrity pass 존재

---

## 15. 사전 지정 분석 지표

### 15.1 행동 정합성

- 일별 simulated aggregate 순주문가치 방향
- 실제 개인투자자 순매수·순매도 방향
- 방향 일치율
- buy recall
- sell recall
- balanced accuracy
- confusion matrix
- 순거래량을 사용한 민감도 분석
- AM·PM 분리 분석

단순 방향 일치율은 실제 개인 방향이 한쪽으로 치우칠 때 과대평가될 수 있으므로 balanced accuracy와 buy/sell recall을 함께 제시한다.

### 15.2 거래와 포트폴리오

- 주문 수량과 거래가치
- 순매수 수량·가치
- 회전율
- 현금비중과 포지션 집중도
- 수익률 분포
- 1주 주문 비율
- 1주 주문 제외 결과
- 거래가치 가중 결과
- 최대 가능 수량이 1주였는지와 자발적 1주 선택 구분

### 15.3 정보와 belief

- fake headline·본문·검색·영향 뉴스 접촉률
- 노출 전, 노출 turn, 다음 turn belief 이동
- stance, claim acceptance/rejection, confidence
- total deviation과 하위 차원
- 커뮤니티 게시·열람 이후 belief 유사도와 행동 수렴
- event·phase·regime·persona별 이질성

### 15.4 embedding

- 원문 belief 자체보다 `노출 후 belief − 노출 전 belief` 이동을 우선 분석
- factual anchor, bullish fake, bearish fake에 대한 상대적 위치
- event-time trajectory와 다음 turn persistence
- condition·regime·phase·persona·depth별 색상·facet
- UMAP은 시각화, cosine distance와 permutation/bootstrap은 정량 분석
- 예쁜 군집만으로 claim acceptance나 취약성을 판정하지 않음
- silhouette가 낮으면 자연 군집이 없다는 결과도 보고

---

## 16. Deviation rubric의 본래 분석 역할

기본 단위는 `agent × synthetic article/phase × 최초 접촉 turn`이다.

- stance shift
- unsupported information introduction
- certainty shift
- perspective/meaning deviation
- temporal leakage
- claim acceptance, amplification, attenuation, rejection
- 포트폴리오 자기확신과 현금·보유량 관련 risk language

하위 차원을 먼저 보고 total deviation은 조건·사건·persona를 압축 비교하는 보조 점수로 사용한다. embedding similarity만으로 수용 여부를 판정하지 않고 blind rubric과 교차 검증한다.

---

## 17. 통계 분석 원칙

- 주 인과 비교는 조건 assignment contrast다.
- fake를 실제로 읽은 사람 대 읽지 않은 사람 비교는 선택 편향이 있으므로 메커니즘 분석이다.
- condition, fake direction, community, regime의 주효과와 상호작용을 본다.
- agent와 event의 반복 관측 구조를 고려한다.
- 조건당 다중 seed 반복이 가능하면 random effect 또는 cluster/bootstrap 불확실성을 보고한다.
- 현재 단일 seed만 실행하면 조건 효과를 해당 modeled population·seed의 결과로 제한한다.
- 초기자본과 현재 현금비중을 동시에 모델링하되 서로 대체하지 않는다.
- 가격은 조건 간 동일하므로 abnormal return이나 treatment-induced price effect를 주장하지 않는다.

---

## 18. 논문에서 피해야 할 주장

- LLM 개인투자자만으로 실제 시장 전체를 재현했다.
- stylized fact가 맞으므로 agent의 판단이 인간과 동일하다.
- 커뮤니티 게시판 결과가 실제 social graph cascade를 재현한다.
- fake를 읽은 agent와 안 읽은 agent의 차이가 무작위 인과효과다.
- 매도 주문은 항상 bearish belief다.
- `자산가` 배지는 초기자본 10억을 의미한다.
- 현재 완료 로그에 10억 agent가 포함돼 있다.
- 2개월 결과에 원래 30개 fake가 모두 들어갔다.

---

## 19. 2026-07-21 기준 설계-구현 감사표

| 항목 | 설계 의도 | 현재 확인 | 조치 필요 |
|---|---|---|---|
| 가격 | 실제 가격 외생 고정 | 구현됨 | 없음 |
| action | buy/sell-only | 구현됨 | 현금 제약 해석 필요 |
| 30명 동일 cohort | 모든 조건 동일 | launcher invariant에 포함 | 없음 |
| 초기자본 10% | 30명 중 3명 10억 | 현재 A001~A030은 0명 | **cohort 결정 필요** |
| 뉴스 depth | 0/1/2 이질성 | 현재 10/16/4 | 대표성 검토 필요 |
| 실제 뉴스 | 최대 10개 유지 | 3/23 PM만 9개 | 논문에 명시 |
| fake | 실제 뉴스+1, variant별 30 | 3개월 파일 기준 30 | 사람 승인 정책 필요 |
| community 시간순서 | PM 뒤 생성, 다음 AM 반영 | 구현됨 | 없음 |
| fallback | 임의 1주 생성 금지 | 제거됨 | 1주 민감도 분석 |
| checkpoint | 상태 연속, phase resume | 구현됨 | 실제 장비 완료 확인 |
| 모델 | Qwen 3.5 Flash만 | launcher가 검사 | 반환 모델 audit 확인 |
| 기간 | 원래 63일 | 2개월 축약 검토 중 | 기간을 실행 전 확정 |

## 20. 실험 재개 전 반드시 확정할 결정

1. 원래 3개월을 유지할지, 2026-04-30까지 2개월로 줄일지
2. 30명 중 10억 agent 3명을 포함할지
3. 포함한다면 어떤 3명을 선택하고 나이·성별·전략·depth까지 어떻게 층화할지
4. 현재 실행 중인 전원 1억 C00을 분석 기준선으로 유지할지, 새 cohort로 다시 실행할지
5. fake review 자극 60개를 그대로 쓸지 사람 승인 후 쓸지
6. 6조건의 공식 ID를 현재 launcher 방식으로 고정할지
7. 조건당 단일 seed만 사용할지, 최소 반복을 추가할지

이 결정이 끝나기 전에는 자산별 취약성을 논문의 확정 연구 질문으로 쓰지 않는다.
