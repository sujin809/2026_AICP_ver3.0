# 분석 필드 가이드 — 어느 차원에서 무엇을 봐야 하는가

실험 종료 후 분석할 때 참조하는 문서다. 각 belief 차원의 정의, 그 차원을
분석할 때 실제로 읽어야 하는 테이블·필드, 그리고 **해석 시 반드시 알아야
하는 인공물(artifact)** 을 정리한다.

인공물 항목은 2026-07-30~31 유료 검증 7회에서 실제 관측·수정한 내용이며,
논문 caveat로 그대로 옮겨 쓸 수 있다.

---

## 0. 데이터가 있는 곳

### canonical DB (`<run-dir>/.runtime/runtime_sim.db`)

| 테이블 | 단위 | 핵심 필드 |
| --- | --- | --- |
| `simulation_stb_states` | agent × event | `dim_1`~`dim_6`, `dimension_evidence_json`, `evidence_json`, `stb_id` |
| `simulation_ltb_states` | agent × event (+LTB₀) | `dim_1`~`dim_6`, `integration_evidence_json`, `parent_ltb_id`, `source_stb_id`, `source_fill_id`, `belief_summary`, `view_change_json` |
| `simulation_analyses` | agent × event | `analysis_json`, `source_ltb_id`, `source_stb_id` |
| `simulation_decisions` | agent × event | `action`, `requested_quantity`, `decision_json`, `analysis_id` |
| `simulation_fills` | agent × event | `filled_quantity`, `executed_price`, `fee`, `pre/post_portfolio_json`, `decision_id` |
| `simulation_trade_outcomes` | fill × horizon(3) | `horizon`, `mark_price`, `observed_event_id`, `status` |
| `community_posts` | 게시 시 (agent-PM당 최대 1) | `content`, `score`, `is_best`, `source_ltb_id/fill_id/decision_id` |
| `community_interactions` | reader × post | `reaction` (like/unlike/none) |
| `community_logs` | agent × PM | `best_posts_seen`, `posts_read`, `candidate_posts_seen`, `community_thinking` |

### run-dir 산출물

- `agent_turns.jsonl` — turn별 전체 컨텍스트(뉴스 제목·요약, D2 검색 결과 포함)
- `community_interactions.csv` — 노출 원장. **`exposure_level`이 `title_only`/`full_body`** 를 구분
- `community_selection_inputs.csv` — 후보 화면에 실제로 보인 목록 + 선택 여부
- `community_best_posts.csv` — Best rank, `scheduled/actual_delivery_count`, `self_excluded_count`
- `openrouter_calls.jsonl` — provider 텔레메트리 (토큰·비용·`reasoning_tokens`)
- `llm_validation_errors.jsonl` — **검증 거부 기록**. event 롤백에서 제외되어 실패해도 보존됨
- `.runtime/response_journal.sqlite` — 논리 호출별 요청/응답 원본. `physical_attempts`에 거부 시도와 오류

### 조인 키

```
fill_id → decision_id → analysis_id → (source_stb_id, source_ltb_id)
ltb_id  → parent_ltb_id (재귀 계보)
outcome_id = outcome:<fill_id>:<horizon>
community claim id = community_claim:<agent_id>:t<turn:03d>:<NN>
```

---

## 1. 차원별 분석 가이드

### dim_1 — 향후 1개월 주가 방향 전망

- **분석 축**: 방향성 belief와 실제 거래 방향(`simulation_decisions.action`)의 정합성, 시간에 따른 전망 변화
- **읽을 곳**: `simulation_ltb_states.dim_1` 시계열, `dimension_evidence_json.dim_1`의 support/contradict ID
- **특이사항**: 6차원 중 **support 비율이 가장 낮다**(관측 62.0%). 방향 전망은 반박 근거를 상대적으로 잘 인정하는 차원

### dim_2 — 밸류에이션 관점

- **분석 축**: 저평가/고평가 판단과 매수·매도 방향의 연결
- **읽을 곳**: `dim_2` 텍스트, `analysis_json.valuation_view`와 대조
- **특이사항**: support 비율 81.3%로 높음. 뉴스가 밸류에이션을 지지하는 쪽으로 해석되는 경향

### dim_3 — 거시환경 판단

- **분석 축**: 거시 뉴스(경제 카테고리 2건/event)가 belief에 반영되는 경로
- **읽을 곳**: `dimension_evidence_json.dim_3` ↔ `agent_turns.jsonl`의 경제 카테고리 기사
- **특이사항**: **양면 근거 위반 2위 차원**(dim_4 다음). 거시 기사는 방향 판정이 어렵다

### dim_4 — 시장 심리 감지

- **분석 축**: **커뮤니티 처치의 주 수신 차원.** ON/OFF arm 간 가장 큰 차이가 예상되는 곳
- **읽을 곳**: `dimension_evidence_json.dim_4`에 `community_claim:` ID가 인용된 빈도, ON vs OFF 비교
- **특이사항**: ⚠️ **양면 근거 위반이 압도적으로 집중되는 차원**(관측 43/73건). 같은 기사가 낙관과 불안을 동시에 보여주므로 모델이 support·contradict 양쪽에 넣으려 함 → §2-A 참조

### dim_5 — 뉴스·커뮤니티에 대한 해석

- **분석 축**: 정보 수용의 서술적 내용. BMDI blind coding의 주 대상
- **읽을 곳**: `dim_5` 텍스트 + `community_thinking`의 `claims`
- **특이사항**: support 비율 81.5%로 최고

### dim_6 — ⚠️ STB와 LTB의 의미가 다르다

**이 차원은 STB와 LTB에서 완전히 다른 것을 담는다. 절대 같은 축으로 비교하면 안 된다.**

| | STB `dim_6` | LTB `dim_6` |
| --- | --- | --- |
| 의미 | **오늘 정보의 한계와 주의점** | **누적 자기평가** (판단이 맞았나) |
| 형식 | `정보 한계: ... / 주의점: ...` 고정 | 자유 서술 |
| 입력 | 과거 성과 없음 | 실제 fill + 도래한 H1/H5 outcome |
| 분석 용도 | 정보 불확실성 인식 | 성과 피드백 학습 |

- **읽을 곳(LTB)**: `integration_evidence_json.dim_6`의 `outcome:` ID → `simulation_trade_outcomes`로 조인
- **outcome 방향 규칙**: `action_aligned_markout > 0` → `support`, `< 0` → `contradict`로 **시스템이 강제**. 모델이 고르지 않는다

---

## 2. 해석 시 반드시 알아야 하는 인공물

### A. 양면 근거의 support 편입 ⚠️ 가장 중요

같은 근거 ID를 한 차원의 support·contradict 양쪽에 넣으면 검증기가 거부한다.
모델은 재시도에서 한쪽을 골라야 한다.

- **결과**: "실은 양면적이었던 근거"가 support 또는 contradict **한쪽으로 편입된 채로만 데이터에 남는다**. 양면성 자체는 최종 evidence에 표현되지 않는다
- **집중도**: 소수 기사에 극단적으로 몰린다. 관측 예 — 하루 위반 87건 중 **단일 기사가 61건**, 관련 기사는 4개뿐
- **차원 분포**: dim_4 ≫ dim_3 > dim_2 > dim_1 > dim_5
- **정량화 방법**: `llm_validation_errors.jsonl`에서 정규식으로 추출

  ```
  dimension_evidence\.(dim_\d):same_id_in_both_relations:\[(.*)\]
  ```

  → **어느 기사가 어느 차원에서 몇 번 양면적으로 판정됐는지** 집계 가능.
  "기사별 양면성 지수"로 논문에 쓸 수 있다
- **편향 주입 없음**: 2026-07-31에 재시도 힌트의 "애매하면 support에" 문구를 제거했다. 방향은 모델이 결정한다. **그 이전 실행 데이터에는 이 계도가 들어 있으므로 섞어 쓰면 안 된다**
- **참고 기저값**: 전체 evidence의 support 비율 약 73.6%(계도 문구 제거 전 관측). 대부분 첫 시도 수락이므로 이는 모델의 기저 성향에 가깝다

### B. 강제 매매(hold 없음)가 dim_6 outcome 근거를 오염시킨다

`markout = direction × (mark_price − entry_price) / entry_price`이고
entry_price는 그 event의 공시가(전원 동일)다. 따라서:

- 같은 event에서 **매수한 모든 agent의 markout이 동일**, 매도한 agent는 정확한 부호 반전
- hold가 비활성이라 관점이 없는 agent도 방향을 강제로 골라야 한다
- ⇒ LTB dim_6의 support/contradict는 **(강제로 고른 방향) × (시장 방향)** 으로 결정되며, **그 agent의 추론 품질을 반영하지 않는다**
- 임계값 `OUTCOME_MARKOUT_RELATION_EPSILON = 1e-12`라 사실상 모든 결과가 방향을 갖는다. 사후에 더 큰 임계값(예 ±0.1%)으로 재분류하려면 `simulation_trade_outcomes.mark_price`와 fill의 `executed_price`로 markout을 재계산하면 된다

### C. belief 텍스트 변화 = 실제 변화 (강제 패러프레이즈 없음)

- 2026-07-31에 "여섯 차원을 매번 새 문장으로 재서술" 규칙을 제거했다. **관점이 안 변한 차원은 이전 문장이 그대로 유지된다**
- ⇒ 연속 belief의 임베딩 거리가 0에 가까우면 그것은 **진짜 변화 없음**이다
- 유일한 제약: 여섯 차원 **전부**가 직전 LTB와 완전히 동일하면 거부(퇴행적 전체 복사 방지)
- **이 규칙 변경 이전 데이터는 강제 패러프레이즈 노이즈를 포함한다**

### D. 커뮤니티 인용은 시스템이 조회한 원문이다

- 모델은 `[인용 N]` 번호만 고르고, 시스템이 그 번호의 원문 문장을 `supporting_quote`에 채운다
- ⇒ **인용문은 구조상 항상 verbatim**이다. 모델이 지어낸 문장이 아니다
- 모델이 출처 ID를 잘못 적으면 번호가 가리키는 실제 노출 ID로 보정된다. **보정 전 원시 응답은 journal의 `logical_responses.response_json`에 보존**되므로 귀속 정확도를 사후 검증할 수 있다
- 번호표 전체(`quotable_registry`: 번호 → 문장 → 노출 ID)는 journal의 `request_json.semantic_inputs`에 있다

### E. 커뮤니티 노출은 `title_only`와 `full_body`를 절대 합산하지 말 것

- `title_only`: 후보 화면에서 제목만 봄 (claim·STB 근거로 쓸 수 없음)
- `full_body`: 실제로 본문을 읽음 (Best 배달 또는 직접 선택)
- 겹치는 글은 `best_full_body`와 `selected_full_body_replay` **두 관계를 모두** 가진다

### F. 오프라인 스텁 데이터를 실측으로 오해하지 말 것

무과금 실행(`TWINMARKET_OFFLINE_LLM=1`)의 반응 분포는 결정론적 산물이다.

| | offline stub | 실제 모델 (day1 실측) |
| --- | --- | --- |
| like | 40.0% | 54.3% |
| unlike | **0.0%** | 24.3% |
| none | 60.0% | 21.4% |

stub은 `index % 3 == 0`이면 like, 아니면 none을 반환하며 **unlike를 생성하지 않는다**.

### G. 첫날·burn-in 구간의 구조적 특성

- day1에는 전날 Best가 없어 `community_thinking`이 실행되지 않는다 → 커뮤니티 claim이 STB에 없음
- day1 후보 보드의 `score`는 반응 전이라 전부 0
- H1은 1거래일, H5는 5거래일 뒤에 도래하므로 초반 LTB dim_6에는 outcome 근거가 없다
- burn-in 3거래일은 분석에서 제외 대상

---

## 3. 자주 쓸 조회 패턴

### 커뮤니티가 belief에 도달한 경로 추적

```sql
-- 어떤 agent의 STB가 커뮤니티 claim을 인용했는가
SELECT agent_id, turn, dimension_evidence_json
FROM simulation_stb_states
WHERE dimension_evidence_json LIKE '%community_claim:%';
```

claim ID → `community_logs.community_thinking`의 해당 claim →
`supporting_quote`(verbatim 원문) + `source_exposure_ids` →
`community_interactions`에서 그 글을 언제 어떤 경로로 읽었는지.

### 기사별 양면성 지수

```python
import json, re, collections
ids = collections.Counter()
for line in open("<run-dir>/llm_validation_errors.jsonl", encoding="utf-8"):
    row = json.loads(line)
    if row["label"] != "short_term_belief":
        continue
    for error in row.get("validation_errors") or []:
        m = re.match(r"dimension_evidence\.(dim_\d):same_id_in_both_relations:\[(.*)\]", str(error))
        if m:
            for nid in re.findall(r"'([^']+)'", m.group(2)):
                ids[(nid, m.group(1))] += 1
```

### OFF/ON arm 비교 시 필수 확인

- `run_metadata.json`의 `condition_id`, `community_mode`
- OFF arm은 `community_*` 테이블이 **전부 0행**이어야 한다 (canonical validator가 강제)
- 두 arm의 immutable 입력 차이가 `community_mode` 하나뿐인지 (`pair_evaluation`이 검증)

---

## 4. 검증 재시도 데이터 자체의 분석 가치

`llm_validation_errors.jsonl`과 journal의 `physical_attempts`는 오류 로그가
아니라 **모델이 어디서 판단을 주저했는지의 기록**이다.

- 양면 근거 시도 → 기사·차원별 모호성
- LTB relation 위반 → 성과 피드백을 자기 서술과 맞추려는 시도
- 인용 번호 선택 분포 → 어떤 문장이 근거로 자주 선택되는가

**한계**: `llm_validation_errors.jsonl` 행에는 `agent_id`·`turn`이 없다.
`seed` 값으로 journal의 `seed_schedule`과 조인하면 복원 가능하지만 번거롭다.
45일 본실행 전에 두 필드를 로그에 추가하면 분석이 크게 편해진다(미적용).

---

## 5. 2026-07-31 확정 분석 지표 (v8 기준선 포함)

v8 유료 2일 완주본이 `outputs/logs/live_2day_v8_20260731/`에 있다(git 추적).
45일 결과와 비교할 기준선이며, 아래 수치는 모두 v8 실측이다.

### 5.1 일별 게시율

- v8: 게시 자격 70명 × 2일 = **140/140 게시 (100%)**, `will_post=false` 0회.
- 프롬프트 편향은 아님(균형 확인 완료). 원인 후보: hold 비활성으로 매 턴
  체결이 존재해 항상 글감이 있음, view_change가 상시 비어있지 않음, LLM의
  null 행동 과소 선택 성향.
- 45일에서도 100%면 "게시 여부 판단" 설계 요소의 변별력이 없다 → 논문 한계
  항목. 조회: `community_posts`를 자격자 수로 나눈 일별 비율.

### 5.2 인용 다양성 (개인차 재현의 실질 지표)

- 6차원 합산 인용 조합: t1 85종 → t3 **100종/100명** (전원 상이).
- dim_1 단독은 depth당 2~3종으로 좁다 — 방향 관련 기사가 이벤트당 3~4개인
  구조 탓이며 결함이 아니다. 같은 depth는 같은 10개 기사를 본다(노출 통제).

### 5.3 근거 관계 비율

- v8: STB support 69.4% / LTB support 65.9%.
- 시장 방향과 무관함을 확인(-6.9% 폭락일 t4에 support 75.0%). support는
  "뉴스가 긍정적"이 아니라 "이 근거가 내 서술을 뒷받침"의 뜻이다.
- 뉴스에 감성 라벨은 의도적으로 없다(연구자 편향 차단).

### 5.4 D2 검색 활용 (45일 라이브가 첫 실측)

- 검색 채점이 2026-07-31 토큰 방식으로 수정됨(그 전 라이브는 항상 0건).
- 조회: `agent_turns.jsonl`의 `depth2_flow.step3_search.result_count`,
  STB `dimension_evidence`에서 `kind=depth2_recent_search`인 ID 인용 수.
- 오프라인 45일 실측: 검색 90회 중 72회 5건 만재, STB 유입 87/90,
  검색 기사 인용 258회. pre_search 키워드는 페르소나를 반영한다
  (가치투자자가 "PER PBR 내재가치" 검색).

### 5.5 커뮤니티 반응 분포

- v8: like 52.6% / unlike 25.7% / none 21.7% (스텁의 40/0/60은 인공물).
- unlike 1/4 존재 = 무비판적 동조 아님.

### 5.6 외부 검증 계획

- 삼성전자 실제 개인 순매수/순매도 방향 일별 기록(사용자 보유)과 시뮬레이션
  집계 매매 방향을 비교한다.
- burn-in 3일이 t1 강제 전원매수(보유 0 + hold 비활성)를 주분석에서 제외한다.
- hold 비활성이므로 **방향 비교만 가능, 거래 빈도 비교는 불가** — 논문에
  한계로 명시할 것.

### 5.7 페르소나 행동 분화 (v8, t1 제외)

- 매도 비율: 기술적분석 28.2% vs 가치투자 11.9%; 처분효과 high 33.3%.
- 매수 시 현금 투입: 위험선호 high 37.5% vs low 28.0% (한도 50%).
- 2일 표본이라 확정은 아니나 전부 페르소나와 일치하는 방향.
