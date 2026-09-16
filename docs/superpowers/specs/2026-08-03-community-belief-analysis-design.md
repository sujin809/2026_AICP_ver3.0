# v10 커뮤니티–belief 분석 설계 (2026-08-03)

> 대상 실행: `outputs/logs/experiment_matrix_45day_v10/` (RN_COMM_ON / RN_COMM_OFF)
> 상태: 설계 확정 대기 → 승인 후 구현 계획으로 이관

---

## 0. 절대 제약 — 추가 과금 0원

**이 분석은 어떤 유료 API도 호출하지 않는다.** 사용자 지시(2026-08-03)이며 협상 대상이 아니다.

허용되는 비용은 다음 뿐이다.

- pip 패키지 최초 다운로드 (PyPI, 무료): 약 2~3GB
- HuggingFace 공개 모델 가중치 최초 다운로드 (무료, API 키 불필요): 약 470MB
- 로컬 CPU 시간과 디스크

### 0.1 코드 수준 강제 장치 (구현 필수)

1. **`openai` 패키지를 analysis venv에 설치하지 않는다.** import 자체가 불가능해진다.
2. 모든 분석 스크립트는 진입 시 `.env`를 **로드하지 않고**, 아래 환경변수가 존재하면 `os.environ.pop()`으로 제거한 뒤 시작한다.
   `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `HF_TOKEN`
3. 최초 모델 다운로드 이후 `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`을 고정한다.
4. 공통 모듈 `guard.py`에 위 세 가지를 넣고 **모든 스크립트가 첫 줄에서 import**한다. 누락 시 CI/테스트가 실패하도록 grep 검사를 둔다.

### 0.2 이번 범위에서 명시적으로 제외되는 것 (유료이므로)

- 클러스터 라벨링의 LLM 보조 → **전량 수동 라벨링**
- `analysis/belief_event_study/total_deviation_spec.md`의 **BMDI rubric LLM judge 코딩 전량** → 이번 범위 아님
- `belief_deviation_rubric.md` R1~R8의 자동 코딩 → 이번 범위 아님
- 추가 시뮬레이션 실행, 재실행, resume → 이번 범위 아님

향후 이들을 하려면 **별도 승인 + 별도 예산 건**으로 다시 올린다.

---

## 1. 연구 질문

`EXPERIMENT_DESIGN.md` §1의 질문 2·3에 대응한다.

| # | 질문 | 담당 단계 |
|---|---|---|
| Q1 | 커뮤니티 정보환경(ON)이 belief를 OFF 대비 얼마나·어느 차원에서 바꾸는가 | S3 |
| Q2 | 커뮤니티 글과 실제 뉴스는 어떤 화제 구조를 이루며, 커뮤니티는 뉴스를 되풀이하는가 새 화제를 만드는가 | S1 |
| Q3 | 어떤 토픽의 어떤 글이 실제로 belief에 채택되었는가 (도달 vs 채택) | S2·S4 |
| Q4 | belief 변화가 거래 방향으로 번역되며, 그 결과가 실제 개인투자자 수급 방향에 더 닮아지는가 | S3 |

**비범위**: 가격 경로 재현, 개인 주문 예측, 투자 권고, 인과적 "커뮤니티가 거래를 바꿨다" 단정.

---

## 2. 입력 정본 (실측 확인 완료, 2026-08-03)

### 2.1 경로

```
outputs/logs/experiment_matrix_45day_v10/
  RN_COMM_ON/   RN_COMM_OFF/
    .runtime/runtime_sim.db          ← canonical DB
    community_interactions.csv       ← exposure_level 있음 (DB 테이블에는 없음!)
    community_best_posts.csv
    community_selection_inputs.csv
    agent_turns.jsonl(.gz)
    memory_lineage.jsonl
preparation/rn_ab_sealed_v1/news.json  ← 봉인 뉴스 정본
```

### 2.2 DB 열기 방법 (중요)

WAL 상태라 `mode=ro`로는 열리지 않는다. **반드시 `immutable=1`을 쓴다.**

```python
sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)
```

원본은 **읽기 전용으로만** 접근한다. 어떤 스크립트도 run-dir 안에 파일을 쓰지 않는다.

### 2.3 실측 행 수 (S0 검증 기준값)

| 테이블 | RN_COMM_ON | RN_COMM_OFF |
|---|---|---|
| `simulation_stb_states` | 9,000 | 9,000 |
| `simulation_ltb_states` | 9,100 | 9,100 |
| `simulation_decisions` | 9,000 | 9,000 |
| `simulation_fills` | 9,000 | 9,000 |
| `simulation_trade_outcomes` | 27,000 | 27,000 |
| `community_posts` | 3,150 (Best 225 / 비Best 2,925) | 0 |
| `community_interactions` | 15,745 | 0 |
| `community_logs` | 4,500 | 0 |

봉인 뉴스: `articles` 760건, `slots` 760건, 90 event.

### 2.4 확인된 스키마

```
community_posts        post_id, agent_id, anonymous_code, turn, date, post_type,
                       title, content, like_count, unlike_count, score, is_best,
                       source_ltb_id, source_fill_id, source_decision_id
community_interactions(DB)  interaction_id, agent_id, post_id, turn, date, reaction
community_interactions(CSV) run_id, date, turn, agent_id, selected_post_ids, post_id,
                       exposure_level, selected, is_best, anonymous_code, title,
                       post_type, content, body_sha256, reaction, author_profile,
                       profile_scope, source_date, delivery_date, source_turn,
                       delivery_turn, delivery_status, replay, provenance_id
community_logs         log_id, agent_id, turn, date, best_posts_seen, posts_read,
                       community_thinking, candidate_posts_seen
simulation_stb_states  stb_id, agent_id, turn, date, subturn, dim_1..dim_6,
                       evidence_json, dimension_evidence_json, scientific_sha256, created_at
simulation_ltb_states  ltb_id, agent_id, turn, visible_from_turn, date, subturn,
                       parent_ltb_id, source_stb_id, source_decision_id, source_fill_id,
                       dim_1..dim_6, integration_evidence_json, belief_summary,
                       view_change_json, ...
news.json articles[]   article_id, title, summary, published_at, observed_at,
                       last_modified_at, source, source_url, payload_sha256, ...
news.json slots[]      article_id, event_id, slot_ordinal
```

**주의**: `exposure_level`(`title_only` / `full_body`)은 **CSV에만** 있다. DB의 `community_interactions`에는 없다. 노출 수준 분리는 반드시 CSV를 쓴다.

**카테고리**: `article_id`에 인코딩되어 있다 (`news_20260227_종목_4aa6a00f` → 종목/섹터/경제). 파싱해서 열로 만든다.

---

## 3. 산출물 구조

```
analysis/community_belief_v10/
  .venv/                  ← 이 분석 전용 (봉인 실행용 .venv와 분리)
  guard.py                ← §0.1 무과금 강제
  s0_build_panels.py
  s1_cluster.py
  s2_link_beliefs.py
  s3_arm_contrast.py
  s4_mechanism.py
  s5_robustness.py
  panels/                 ← 중간 산출 (parquet)
  figures/
  cluster_labels.csv      ← 사람이 채우는 파일
  REPORT.md
```

`analysis/.venv`는 봉인된 실행 환경 `.venv`와 **완전히 분리**한다. 루트 `requirements.txt`는 건드리지 않는다(재봉인 이슈 회피).

---

## 4. S0 — 패널 구축

### 4.1 만드는 표

| 파일 | 단위 | 예상 행 수 |
|---|---|---|
| `belief_panel.parquet` | arm × agent × turn × {STB,LTB} × dim | 217,200 |
| `evidence_panel.parquet` | 위 belief가 인용한 근거 1건 = 1행 | 수십만 |
| `exposure_panel.parquet` | reader × post × exposure_level | 15,745 |
| `claim_panel.parquet` | community_thinking의 claim 1건 = 1행 | — |
| `post_panel.parquet` | 게시글 | 3,150 |
| `news_panel.parquet` | 봉인 기사 × event slot | 760 |
| `action_panel.parquet` | arm × agent × turn의 decision/fill/outcome | 9,000 |

### 4.2 evidence_panel 파싱 규칙

`dimension_evidence_json`(STB) / `integration_evidence_json`(LTB)을 펼쳐
`(arm, agent_id, turn, layer, dim, relation, evidence_id)` 행으로 만들고, `evidence_id` 접두어로 `kind`를 분류한다.

| kind | 접두어/형태 | 의미 |
|---|---|---|
| `news` | 봉인 article_id | 실제 뉴스 |
| `community_claim` | `community_claim:<agent>:t<NNN>:<NN>` | 커뮤니티 |
| `depth2_recent_search` | D2 검색 결과 | D2 전용 |
| `outcome` | `outcome:<fill_id>:<horizon>` | 성과 피드백 |

**실제 접두어 집합은 구현 시 데이터에서 열거해 확인하고, 미분류 ID가 1건이라도 남으면 실패시킨다.** (조용한 누락 방지)

### 4.3 burn-in

첫 3거래일 = turn 1~6. **삭제하지 않고 `is_burnin` 플래그만 단다.** 주분석에서 제외하되 민감도 분석에서 되살린다.

### 4.4 S0 검증 게이트 (assert)

1. 행 수가 §2.3 표와 정확히 일치
2. **OFF arm의 community 관련 행이 0** (설계 위반 감지)
3. 미분류 `evidence_id` 0건
4. `exposure_level` 값 집합이 `{title_only, full_body}` 뿐
5. ON/OFF의 `(agent_id, turn)` 키 집합이 완전히 동일 (짝 대조 가능성 확인)
6. `community_posts` 3,150 = 자격자 70명 × 45일 → **게시율 100% 재확인**(v8 관측의 45일 재현 여부)

---

## 5. S1 — 뉴스·커뮤니티 공동 클러스터링

### 5.1 임베딩

- 모델: `intfloat/multilingual-e5-small` (로컬, 무료). `embedding_analysis_plan.md`의 파일럿과 동일
- 전처리: `query: ` prefix 통일, L2 정규화
- 대상 텍스트
  - 뉴스 760: `title + "\n" + summary`
  - 게시글 3,150: `title + "\n" + content`

### 5.2 군집

- 알고리즘: k-means (cosine = 정규화 후 유클리드)
- **k 선택**: silhouette + 다중 시드 안정성(ARI). 단일 시드 최적값을 채택하지 않는다
- 클러스터별 산출: centroid 최근접 문장 10개, 뉴스/게시글 구성비, 날짜 분포, 카테고리(종목/섹터/경제) 분포
- **라벨링은 사람이 한다.** `cluster_labels.csv`에 대표 문장을 출력하고 사용자가 이름을 채운다. LLM 보조 없음(§0.2)

### 5.3 클러스터에서 바로 나오는 지표

| 지표 | 정의 |
|---|---|
| **에코 비율** | 클러스터 내 게시글 수 / 뉴스 수 — echo(뉴스 반복) vs novel(커뮤니티 고유 화제) 판별 |
| **전이 지연** | 뉴스가 D일 등장 → 같은 클러스터 게시글이 D+k일 등장하는 분포 |

### 5.4 필수 진단 (오해 방지)

`embedding_analysis_plan.md`가 못 박은 대로 **비지도 군집은 가설 생성용**이다. 다음을 반드시 함께 보고한다.

- **날짜 라벨에 대한 silhouette** — 클러스터가 화제가 아니라 시장 국면/날짜로 갈렸을 가능성
- post_type 라벨, 카테고리 라벨에 대한 silhouette
- 낮은 점수는 "실패"가 아니라 **"보기 좋은 지도를 자연 군집으로 해석하지 말라"는 정보**로 보고한다

---

## 6. S2 — belief 붙이기 (2층 측정)

### 6.1 1층 · 인용 (확정적, 비용 0)

커뮤니티 claim은 번호 기반 인용이라 원문이 **구조적으로 verbatim**이고 `dimension_evidence_json`에 ID가 남는다. 추정이 아니라 원장에서 직접 집계한다.

```
community_claim ID → claim_panel.source_exposure_ids → post_id → S1 클러스터
                                                              ↓
                                            "토픽 × belief 차원" 교차표
```

보고 항목:

- agent × turn × dim별 **커뮤니티 인용 비중** = community_claim 인용 수 / 전체 evidence 수
- **dim_4(시장 심리)가 커뮤니티의 주 수신 차원인가** — `ANALYSIS_FIELD_GUIDE.md` §1의 가설 직접 검증
- 경로 분리: Best 배달 경유 vs 직접 선택 읽기 경유
- `title_only` 노출은 규정상 근거로 쓸 수 없으므로 분석에서 원천 제외

### 6.2 2층 · 임베딩 (연속량)

- `Δ_semantic(a, t, dim) = 1 − cos(belief_t, belief_{t−1})` — 차원별, STB/LTB 각각
- `Δ_topic(a, t, dim, c) = cos(belief_t, centroid_c) − cos(belief_{t−1}, centroid_c)`
  → belief가 클러스터 c 쪽으로 끌려간 양
- **해석 근거**: 2026-07-31에 강제 패러프레이즈 규칙이 제거되었으므로 **Δ≈0은 노이즈가 아니라 진짜 무변화**다 (`ANALYSIS_FIELD_GUIDE.md` §2-C). 이 데이터의 핵심 강점이며 REPORT에 명시한다

### 6.3 분리 규칙 (위반 금지)

- STB(당일 정보 반응)와 LTB(사후 통합)를 **절대 합치지 않는다**
- **dim_6은 STB와 LTB의 의미가 다르므로 두 층 비교 자체를 금지**한다 (STB=정보 한계, LTB=누적 자기평가)

---

## 7. S3 — 주분석: ON vs OFF paired 대조

### 7.1 짝짓기

seed·뉴스·cohort·prompt·모델이 동일하고 `community_mode`만 다르므로 `(agent_id, turn, layer, dim)`이 1:1로 대응한다. S0 게이트 5가 이를 보증한다.

### 7.2 지표

- Δ_semantic 분포 차이 (차원별, 층별)
- **무변화 비율** 차이 — 커뮤니티가 belief를 더 자주 흔드는가
- evidence 구성 변화 — 커뮤니티 인용이 뉴스 인용을 **밀어냈는가 더했는가**
- support/contradict 비율 변화

### 7.3 경로 발산의 정직한 처리 (핵심 방법론 이슈)

t=2 이후 두 arm의 belief 경로가 갈라지므로, t가 클수록 추정치는 "커뮤니티 처치효과 + 누적 경로 발산"의 혼합이다. 따라서:

1. turn 구간별(초/중/후반)로 나누어 보고한다
2. **첫 Best 수신 turn의 즉시 효과**를 가장 깨끗한 추정치로 별도 제시한다
3. day1은 전날 Best가 없어 `community_thinking`이 실행되지 않는다 — 구조적 기준선으로 활용
4. "누적 발산이 섞여 있다"를 REPORT에 명시한다. 후반 차이를 순수 처치효과로 주장하지 않는다

### 7.4 통계 원칙

- **agent 수준 클러스터 부트스트랩.** agent-turn 행을 독립 표본으로 취급하지 않는다 (`total_deviation_spec.md` §4)
- 다중 비교(6차원 × 2층)에 대한 보정 또는 사전지정 주지표를 명시한다. 주지표는 **STB dim_4의 커뮤니티 인용 비중과 Δ_semantic** 으로 사전지정한다

### 7.5 행동·외부 검증까지 연결

```
belief 이동 → decision action → 일별 signed fill value 방향
            → 실제 삼성전자 개인 순매수 방향과의 일치율 (ON / OFF 각각)
```

기존 `validation/validate_trading_direction.py`를 재사용한다(무과금).
헤드라인 후보: **"커뮤니티가 있으면 실제 개인투자자 수급 방향에 더 닮아지는가"**

한계 명시: `hold` 비활성이라 **방향 비교만 가능하고 거래 빈도 비교는 불가**하다.

---

## 8. S4 — ON 내부 메커니즘: 어떤 글이 움직였나

### 8.1 글 단위 지표

```
도달(reach)   = 그 글을 full_body로 읽은 독자 수
채택(adopt)   = 그 글의 claim이 STB/LTB 근거로 인용된 수
채택률        = adopt / reach
```

- 클러스터별 채택률 비교: 읽히기만 하는 토픽 vs belief에 박히는 토픽
- **Best(225) vs 비Best(2,925)**: Best는 D0 포함 전원에게 본문 배달되어 도달이 압도적으로 크다. 채택률이 도달에 **비례하는지 초과하는지**가 "주요 글" 효과의 실체다
- 반응과 채택의 관계: **unlike한 글도 인용되는가** (contradict 관계로 들어오면 그것도 belief 변화다)

### 8.2 준외생 변이 (보조 분석, 검정력 한계 명시)

| 변이 | 성격 | 한계 |
|---|---|---|
| Best 작성자 자기글 제외 | 같은 날 같은 자격인데 노출 4 vs 5 | 표본 = 45일 × 최대 5명 |
| depth 0/1/2 사전 고정 | D0는 선택 읽기 없이 Best만 수신 | depth는 persona와 상관될 수 있음 |

### 8.3 인과 해석 금지선

- `selected`(직접 선택 읽기)는 **처치 이후의 선택**이므로 인과 조절변수로 쓰지 않는다. mechanism 기술통계로만 쓴다 (`belief_deviation_rubric.md` 분석원칙 5)
- 게시글 수나 like 수만으로 "커뮤니티가 거래를 바꿨다"고 단정하지 않는다 (`EXPERIMENT_DESIGN.md` §10)

---

## 9. S5 — 인공물·강건성

### 9.1 승계하는 caveat (`ANALYSIS_FIELD_GUIDE.md` §2)

| 인공물 | 이 분석에 미치는 영향 |
|---|---|
| A. 양면 근거의 한쪽 편입 | dim_4에 집중. "양면성"은 최종 evidence에 남지 않음 |
| B. hold 비활성 | LTB dim_6 outcome은 추론 품질이 아니라 (강제 방향 × 시장 방향) |
| C. 패러프레이즈 강제 제거 | **Δ≈0 = 진짜 무변화** (유리한 성질, §6.2) |
| E. title_only ≠ full_body | 절대 합산 금지 |
| G. day1 구조 | 전날 Best 없음, 후보 board score 전부 0 |
| 게시율 100% | "게시 여부 판단" 설계 요소의 변별력 0 → 논문 한계 |
| shortage event 59개 | complete-news-only 민감도 별도 |

### 9.2 강건성 체크 (전부 무료·로컬)

1. 대체 임베딩 모델 1종 (`paraphrase-multilingual-MiniLM-L12-v2` 등 로컬 모델)
2. k 다중값에서 결론 불변 여부
3. cosine vs centered dot product
4. burn-in 포함/제외
5. complete-news-only (shortage 59 event 제외)
6. 부트스트랩 재표본 수 민감도

---

## 10. 실행 순서와 예상 시간

| 단계 | 내용 | 예상 |
|---|---|---|
| 환경 | analysis venv 생성 + 패키지·모델 다운로드 | 10~20분 (네트워크 의존) |
| S0 | 패널 구축 + 검증 게이트 | 10~20분 |
| S1 | 뉴스 760 + 게시글 3,150 임베딩·군집 | 5분 내외 |
| S1-라벨 | 사용자 수동 라벨링 | 사용자 시간 |
| S2 1층 | 인용 집계 | 5분 |
| S2 2층 | belief 텍스트 약 21.7만 건 임베딩 | 20~40분 (CPU) |
| S3 | arm 대조 + 부트스트랩 | 10~20분 |
| S4 | 메커니즘 | 10분 |
| S5 | 강건성 (임베딩 1회 더) | 30~60분 |

**오늘 안에 숫자가 나오는 범위**: 환경 → S0 → S1 → S2 1층.

---

## 11. 미결 사항

1. `llm_validation_errors.jsonl`에 `agent_id`/`turn`이 없다. "기사별 양면성 지수"를 이번에 하려면 `seed`로 journal의 `seed_schedule`과 조인해야 한다. → **이번 범위에서 제외하고, 필요해지면 별건으로 판단**
2. `k`의 최종값은 S1 실행 후 silhouette·안정성을 보고 사용자와 함께 정한다
3. 실제 삼성전자 개인 순매수 일별 데이터는 사용자 보유분이 필요하다 (S3 §7.5). 파일 형식과 경로를 받아야 한다
4. 클러스터 라벨은 사용자가 직접 붙인다 — 라벨링 전에는 REPORT의 토픽 이름이 비어 있다
5. `panels/`의 parquet과 임베딩 캐시는 git에 올릴지 결정이 필요하다. `.gitignore`의 `.venv/`는 하위 경로에도 적용되므로 analysis venv는 자동 제외되지만, 중간 산출물은 제외 규칙이 없다

---

## 12. 승인이 필요한 지점

- [ ] `analysis/community_belief_v10/.venv` 생성 및 패키지 설치 (디스크 약 2~3GB)
- [ ] HuggingFace 모델 가중치 최초 다운로드 (약 470MB, 무료)
- [ ] 이 스펙 문서의 git 커밋 (AGENTS.md: 커밋은 사용자 명시 승인 필요)
