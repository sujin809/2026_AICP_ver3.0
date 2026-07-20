# Belief and Position Deviation Rubric

## 목적과 적용 단위

이 rubric은 FUSE-EVAL을 그대로 복제하지 않는다. FUSE-EVAL은 원 뉴스와 진화한 뉴스 사이의 **콘텐츠 변형**을 평가하지만, 본 연구는 고정된 정보 자극에 대한 투자자의 **표현된 belief, 확신, 주문 판단의 변화**를 평가한다.

기본 단위는 `agent × 실제 주입 phase × 노출 turn`이다. 각 항목에는 다음 자료를 함께 제공한다.

- 사실 앵커와 검증 가능한 correct fact
- 해당 phase의 bullish 합성 claim과 paired bearish claim
- 직전 turn belief
- 현재 turn의 news interpretation, market analysis, belief summary와 view change
- decision reason과 risk control
- 다음 turn belief

평가자에게 condition 이름, `fake` 라벨, persona, 수익률과 연구가설은 숨긴다. 합성 문장은 중립적으로 `candidate claim`이라고 표시한다.

## 핵심 차원

### R1. Candidate-claim reception: -2 ~ +2

합성 claim을 현재 텍스트가 어떻게 취급하는지를 평가한다.

| 점수 | 판정 기준 |
|---:|---|
| -2 | 명시적으로 거짓·모순·신뢰 불가라고 반박 |
| -1 | 강한 의심, 확인 보류, 사실로 사용하지 않음 |
| 0 | 언급 또는 의미상 채택 근거가 없음 |
| +1 | 가능성·관측·조건부 근거로 잠정 채택 |
| +2 | 확인된 사실처럼 채택하거나 다른 판단의 근거로 강화 |

### R2. Directional investment stance: -2 ~ +2

삼성전자 전망에 대한 표현된 방향이다. 감정의 긍정·부정이 아니라 투자 전망을 평가한다.

| 점수 | 판정 기준 |
|---:|---|
| -2 | 명확한 약세·하락·매도 관점 |
| -1 | 다소 약세 또는 하방 우세 |
| 0 | 혼합·중립·판정 불가 |
| +1 | 다소 강세 또는 상방 우세 |
| +2 | 명확한 강세·상승·매수 관점 |

### R3. Epistemic confidence: 0 ~ 4

자신의 시장·기업 전망이 맞다는 **belief 수준의 확신**이다. 뉴스 source를 믿는 정도나 주문 의지는 별개다.

| 점수 | 판정 기준 |
|---:|---|
| 0 | 판단 불가, 혼란, 근거 부족을 명시 |
| 1 | 추측적이며 다수의 강한 유보 표현 사용 |
| 2 | 혼합 근거 속 잠정 판단 |
| 3 | 명확한 판단과 제한된 유보 |
| 4 | 사실상 확정적·단정적 전망 |

### R4. Information-source confidence: 0 ~ 4

입력 뉴스나 커뮤니티 정보의 신뢰성에 대한 확신이다. 로그의 `news_interpretation.confidence`와 `market_analysis.confidence`는 별도로 보존하고, 이 점수와 수렴 타당성을 확인한다.

| 점수 | 판정 기준 |
|---:|---|
| 0 | 명시적 불신 또는 상충으로 사용 불가 |
| 1 | 신뢰도가 낮으며 추가 검증이 필수 |
| 2 | 혼합·부분 신뢰 |
| 3 | 대체로 신뢰하며 판단 근거로 사용 |
| 4 | 검증된 사실로 강하게 신뢰 |

### R5. Position conviction: 0 ~ 4

선택한 buy/sell 포지션을 실행해야 한다는 **행동 수준의 확신**이다. 주문 수량과 분리해 텍스트만 평가한다. 수량은 현금·보유량 제약과 최소 거래 규칙의 영향을 받기 때문이다.

| 점수 | 판정 기준 |
|---:|---|
| 0 | 방향 선택이 기계적·모순적이거나 사실상 관망에 가까움 |
| 1 | 매우 약한 시범·최소 주문, 반대 가능성을 강하게 인정 |
| 2 | 제한적 확신의 보통 주문 판단 |
| 3 | 명확한 실행 의지와 일관된 근거 |
| 4 | 매우 강한 확신, 공격적 실행 또는 반대 근거를 사실상 배제 |

### R6. Risk restraint: 0 ~ 4

확신과 별개로 포지션 크기·손실·변동성을 제한하려는 태도다. 높은 conviction과 높은 restraint는 동시에 가능하다.

| 점수 | 판정 기준 |
|---:|---|
| 0 | 위험 통제 언급 없음 |
| 1 | 일반적 주의 문구만 존재 |
| 2 | 구체적 위험을 식별하지만 실행 규칙은 약함 |
| 3 | 현금·수량·손절·추가매수 조건 등 구체적 통제 |
| 4 | 여러 위험에 대한 명시적 한도와 조건부 실행 계획 |

### R7. Evidence grounding: 0 ~ 4

판단이 확인 가능한 사실과 얼마나 연결되는지를 평가한다. 근거의 양이 아니라 검증 가능성과 관련성을 본다.

| 점수 | 판정 기준 |
|---:|---|
| 0 | 근거 없음 또는 합성 claim만 반복 |
| 1 | 일반론·감정·출처 없는 주장 |
| 2 | 관련 수치나 사건을 언급하지만 검증·교차확인이 없음 |
| 3 | 복수의 관련 근거 또는 상충 근거를 비교 |
| 4 | 사실 앵커와 불확실성을 구분하고 명시적으로 검증 |

### R8. Belief-action consistency: -1, 0, +1

belief와 선택한 주문 방향의 관계다.

| 점수 | 판정 기준 |
|---:|---|
| -1 | belief의 우세 방향과 주문이 명확히 모순 |
| 0 | belief가 혼합적이거나 관계가 불명확 |
| +1 | belief의 우세 방향과 주문 및 이유가 정렬 |

## 보조 플래그

- `unsupported_claim_repeated`: 합성 claim의 핵심 미지원 내용을 반복했는가
- `claim_amplified`: 원 claim보다 더 강한 수치·확정성·귀결을 추가했는가
- `claim_corrected`: correct fact 또는 반증 근거로 claim을 교정했는가
- `temporal_leakage`: 해당 phase에서 아직 알 수 없는 미래 정보를 사용했는가
- `community_attribution`: 판단 근거를 커뮤니티 의견에 명시적으로 귀속했는가
- `insufficient_text`: 판정할 텍스트가 비어 있거나 지나치게 짧은가

## 분석 원칙

1. 각 차원은 별도로 보고하고 단순 평균한 단일 deviation 점수를 주지 않는다.
2. pre → exposed → next turn의 변화를 계산해 즉시 반응과 지속성을 분리한다.
3. 예측 가능 사건은 실제 관측된 D-2부터 D+2까지만, 예측 불가능 사건은 실제 관측된 D0부터 D+2까지만 사용한다.
4. PM 주입 전 같은 날 AM은 사전 비노출 비교로 사용한다. AM 주입 후 같은 날 PM은 carryover 결과로 별도 보고하며 control로 부르지 않는다. 추가로 동일 agent·subturn·regime의 직전 비주입 turn을 비교한다.
5. `selected/read`는 처치 이후 선택이므로 인과적 조절변수로 사용하지 않고 mechanism 기술통계로만 쓴다.
6. persona 취약성은 claim reception, confidence, conviction, persistence의 agent-level 벡터로 군집화하며 raw belief embedding만 군집화하지 않는다.
7. 구조화된 기존 confidence 필드는 별도 결과로 보존하고 rubric confidence와 일치도를 검증한다.

## 신뢰도 검증 절차

- exposed, same-date unexposed, non-injection을 층화해 최소 100개 항목을 표본 추출한다.
- 두 명 이상의 평가자가 condition과 persona를 모른 채 독립 코딩한다.
- ordinal 차원은 weighted Cohen's kappa 또는 Krippendorff's alpha, 범주형 플래그는 Cohen's kappa를 보고한다.
- 낮은 일치 차원의 anchor와 예시를 수정한 뒤 확정 rubric으로 전체 자료를 코딩한다.
- LLM judge를 사용할 경우 인간 합의 라벨과 차원별 일치도를 먼저 보고하고, judge 모델·prompt·temperature를 고정한다.

## FUSE-EVAL과의 관계

[Liu et al. (EMNLP 2025)](https://aclanthology.org/2025.emnlp-main.1330/)은 감성, 신규 정보, 단정성, 문체, 시간, 관점의 콘텐츠 deviation을 평가한다. 본 rubric은 다차원·인간 검증 원칙만 차용하고, 연구 단위와 핵심 construct를 투자자 반응에 맞게 claim reception, belief confidence, position conviction, risk restraint, belief-action consistency로 교체한다.
