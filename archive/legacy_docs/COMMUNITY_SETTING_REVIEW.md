# 커뮤니티 세팅 검토 보고서

> 보존 구분: 과거 코드 감사 결과. 현재 실행·정책 정본이 아니다.

> 대상: `2026_AICP_ver2.0-sujin` / RealNews Community A/B (2-arm) 실험
> 검토일: 2026-07-29 · 방법: 정적 코드 대조 + 로컬 테스트 실행 (외부 API 호출 없음)
> 검토 범위: ① 에이전트의 게시 판단·내용 ② depth별 커뮤니티 노출 규칙 구현 ③ 커뮤니티 상호작용 파이프라인

---

## 0. 검토 전제 — 커뮤니티 구현은 두 벌이고, 논문 경로는 하나뿐이다

이 저장소에는 커뮤니티 구현이 **두 개** 존재하며, 서로 코드를 공유하지 않는다.

| | 레거시 | **RN 논문 경로** |
|---|---|---|
| 코드 | `twinmarket_kr/community/*` (673줄) | `twinmarket_kr/rn_ab/community*.py` (약 7,600줄) |
| 진입점 | `scripts/05_run_simulation.py`, `06_..._smoke_test.py` | `scripts/09_..._preflight` → `scripts/12_..._operate` |
| 제어 | `config.py`의 `ENABLE_COMMUNITY*` 플래그 3개 | 봉인된 `study_spec.community_policy` |
| 프롬프트 | `prompts/*.txt` | `prompts/*.txt` (통합 production 정본, 런타임은 run-local 봉인 사본) |

**RN 경로는 레거시 커뮤니티 모듈을 단 한 줄도 import하지 않는다.** 따라서 아래 항목은 논문 실행과 무관한 죽은 코드다.

- `ARCHITECTURE.md` §7의 3-플래그 설명
- `community/badge.py`의 뱃지 3종(상위 수익자 / 자산가 / 커뮤니티 인플루언서)
- `community/thinking.py:72`의 본문 200자 절단
- `community/agent.py:67-73`의 D2 전용 작성자 포트폴리오·최근거래 열람

**이 보고서의 모든 서술은 RN 경로(`rn_ab`) 기준이다.**

### 실험 세팅 요약

| 항목 | 값 | 근거 |
|---|---|---|
| arm | `RN_COMM_OFF`, `RN_COMM_ON` (유일한 처치 차이 = community 가용성) | `spec.py:33` `TREATMENT_DIFF_ALLOWLIST=("community_mode",)` |
| 에이전트 | 100명, D0=30 / D1=55 / D2=15 | `preparation/rn_ab_persona_snapshot_v1/persona_depth_manifest.json` 실측 |
| 기간 | 45거래일 × AM·PM = 90 decision event, burn-in 3일 제외 42일 분석 | `RN_FULL_PIPELINE.md:21-22` |
| 메모리 | STB(휘발) + LTB(누적) 2계층 | `RN_FULL_PIPELINE.md:146-151` |
| 모델 | `qwen/qwen3.5-flash-02-23`, reasoning OFF, temperature 0.2 | `rn_model_pin.py`, `call_policy.py:31` |

---

## 1. 커뮤니티에 에이전트가 어떤 글을, 어떤 판단으로 쓰는가

### 1.1 게시 자격

| 조건 | 구현 | 위치 |
|---|---|---|
| depth | `cohort_depths[agent] in {1, 2}` → **D1+D2 = 70명만.** D0(30명)은 게시 0 | `community_provider.py:185-189`, `community.py:627` |
| D0 차단 | 명시적 예외 (`"Depth 0 agents may not post to RN community"`) | `community.py:459-460` |
| arm | `enabled = (condition_id == RN_COMM_ON)`. OFF는 provider 생성 자체가 불가 | `community.py:710-711`, `community_provider.py:98-99` |
| subturn | **PM만.** AM event로 `after_event()`가 불려도 조용히 no-op | `community.py:286-287`, `community_lifecycle.py:492-494` |
| 시각 | PM은 15:30 이후, 다음 AM 배달은 09:00 이전 | `community.py:119-120`, `:1795`, `:1823-1828` |
| 빈도 | 1인 1일 1회 posting decision (복수 게시 불가) | `community_provider.py:193-263` (agent당 `one()` 1회) |

**`eligibility_status` 컬럼은 판정 결과가 아니라 상수 태그다.** `community_provider.py:220`에서 `"eligible"`로 하드코딩되고, `community.py:978-979`와 `db/schema.py:458`의 CHECK가 다른 값을 거부한다. D0는 "부적격 판정"을 받는 것이 아니라 **행 자체가 생성되지 않는다.** 따라서 trace만 보고 "부적격 처리된 에이전트"를 찾을 수 없다.

**체결(fill)은 자격 조건이 아니라 하드 요구사항이다.** `_post_context()`가 해당 agent/event의 `paper_fill_ledger` 행을 반드시 찾아야 하고, 없으면 예외로 run이 멈춘다(`community_provider.py:757-758`). 이것이 성립하는 이유는 `spec.py:431`이 `allow_hold=False`를 강제하기 때문이다 — 모든 에이전트가 매 event에 fill 행을 갖는다. **이 의존 관계에 코드 주석이나 가드가 없다.**

### 1.2 게시 판단에 실제로 들어가는 입력

프롬프트: `prompts/posting_decision.txt` (런타임은 run-local 봉인 사본)
허용 슬롯 6개 고정: `prompt_registry.py:144-151`

| 슬롯 | 실제 채워지는 값 | 위치 |
|---|---|---|
| `persona_prompt` | 봉인된 페르소나 전문 | `community_provider.py:199` |
| `ltb_dimensions` | **방금 커밋된 post-fill LTB_t의 dim_1~dim_6 원문** | `_post_context():733-736, 771-774` |
| `view_change` | 6개 dim의 `before_sha256`/`after_sha256` + `integration_evidence` ID 배열 | `memory.py:284-311` |
| `committed_pm_fill` | action, requested_qty, filled_qty, executed_price, fee, **pre/post 포트폴리오 전체** | `community_provider.py:759-767` |
| `date` | phase 날짜 | `:203` |
| `post_types_guide` | 6종 타입 한국어 설명 상수 | `:60-66` |

**들어가지 않는 것 (확인됨):** STB, `belief_summary`, 시장 가격, market analysis, decision reason, 뉴스, 다른 에이전트의 글, 자기 과거 글. 매 PM 게시판은 완전히 새로 시작한다.

### 1.3 프롬프트가 요구하는 판단 절차

`prompts/posting_decision.txt`는 3단계로 유도한다.

1. **오늘 올리고 싶은가** — "매일 올릴 필요는 전혀 없습니다. 자연스럽게 올리고 싶은 날에만 올리세요."
2. **어떤 유형인가** — 6종 중 선택. "분석적이고 정보성 글만이 좋은 글이 아닙니다. 수익 자랑, 손실 하소연, 궁금한 점 질문, 잡담, 공감 구하기 — 이 모든 것이 실제 커뮤니티에 존재합니다."
3. **제목 + 본문** — "실제 보유·체결 내역과 완전히 일치하도록 억지로 맞추지 않아도 됩니다."

즉 **게시 여부는 강제되지 않고 모델의 자율 판단**이며, **내용의 진실성도 강제되지 않는다.** 서버는 진실성·신뢰도·의미를 판정하지 않고 소유권·인용 출처·프라이버시 경계만 검사한다(`preparation/GENERATED_INPUT_CONTRACT.md:52-61`).

### 1.4 글의 형식과 검증

**post_type 6종:** `impression / question / trade_share / profit_share / analysis / column` (`community_provider.py:55-59`)

**응답 스키마는 배타적 두 형태만 허용** (`community_provider.py:940-953`):
```
{"will_post": false}                                                   ← 키 1개만
{"will_post": true, "post_type": ..., "title": ..., "content": ...}    ← 키 4개 정확히
```
- `title` ≤ 300자, `content` ≤ 8,000자 (`community.py:92-93`)
- 초과 시 **truncate가 아니라 예외** (`_text():155-156`)
- `post_type` 정규식 `^[a-z][a-z0-9_-]{0,31}$` (`community.py:43, 468`)

**재시도 정책: 없음. 완전 fail-closed.**
무효 응답은 절대 accepted되지 않고(`community_provider.py:652-659`), 실패 시 `RNCommunityProviderError` → `CommunityLifecycleError` → `phase_runner.py:263-274`가 **양쪽 arm DB 스냅샷을 복원**하고 `RNPhasePausedError`로 정지한다. HTTP 레벨 재시도도 `max_retries=1`로 금지(`stage_adapter.py:191-195`).

→ **한 에이전트의 스키마 위반 1건이 paired run 전체를 멈춘다.** 재개 시 journal의 accepted 응답을 재생하고 실패한 호출만 다시 부른다.

### 1.5 결정론

| 항목 | 구현 |
|---|---|
| seed | `sha256(study_seed\|namespace\|stage\|agent_id\|event_id)` — `community_provider.py:812-816` |
| **post_id** | 내용 해시 `"post:" + sha256({run, condition, phase, author, title, body_sha, ...})[:40]` — **응답 도착 순서와 무관** (`community.py:478-489`) |
| 게시글 정렬 | `dict(sorted(posts.items()))` = post_id 순 (`community.py:1859`) |
| 반응 집계 | 덧셈만 → 완료 순서 무관 (`community_provider.py:386-394`) |
| Best 랭킹 | `(-score, -like_count, post_id)`, 배달 시 재검증 (`community.py:820, 1212-1214`) |
| 오류 선택 | `_gather`의 `errors[0]`은 완료 순서가 아닌 **입력 위치** 기준 (`community_provider.py:713-724`) |

**응답 지연에 의존하는 비결정성은 발견되지 않았다.**

### 1.6 감사 로그

- **`community_post_trace`** (`db/schema.py:448-492`): 게시 판단 1건 = 1행. `posting_status(posted|skipped)`, `ltb_id`+sha, `view_change_id`+sha, `fill_id`, `prompt_template_sha256`, `prompt_values_sha256`, `logical_call_id`, `accepted_response_sha256`, `title_sha256`, `body_sha256`. `condition_id='RN_COMM_ON'` CHECK로 arm 격리.
- **`observation_events`**: `community_posts:` / `community_selected:` / `community_reader_trace:` / `community_best_schedule:` / `community_best_delivery:` / `community_checkpoint:` / `community_claims:` (`community.py:2968-2994`)
- **최종 재도출 감사** (`finalization.py:713-1010`): 봉인 페르소나 + 커밋된 LTB/fill로 `prompt_values`를 **처음부터 재구성**해 journal request와 바이트 비교하고, `will_post` ↔ 공개 게시판 대조. 위조 해시로는 통과 불가.

---

## 2. Depth별 커뮤니티 노출 규칙 — 문서 vs 코드

### 2.1 규칙 대조표

문서 기준: `RN_FULL_PIPELINE.md` §5.4

| 규칙 | 코드 구현 | 판정 |
|---|---|---|
| Best K=5 → **100명 전원** broadcast (D0 포함) | `community.py:1221` `for recipient in self.cohort_agent_ids` — depth 필터 없음 | ✅ |
| Best payload = 제목 + **본문 전체** | `spec.py:998` `best_payload=="title_plus_full_frozen_body"` 강제, `_best_metadata():2388` `full_body` | ✅ |
| 글 0이면 노출 0 | `_schedule_payload` status=`"empty"` → 배달 루프 0회 (`community.py:2101`) | ✅ |
| self-read D1=5 / D2=10 / D0=0 | `selective_read_cap():713-718` — D0는 0 하드코딩, 나머지는 spec 값 | ⚠️ §2.3 |
| D0의 read 경로 진입 불가 | 3중 차단: `community_provider.py:309-312`, `:419-423`, `community.py:1878-1879` — silent skip이 아니라 **예외** | ✅ |
| self-read에서 자기 글 제외 | `community_provider.py:316`, `community.py:1883-1884`, `:1942` | ✅ |
| depth 출처 = DB `news_depth` | `community.py:690` ← resolver cohort ← 봉인 registry | ✅ |
| 노출 시점 = **다음 거래일 AM** | selected는 `visible_from_event_id=next_am` (`:2363`), best는 next-AM event_id로 삽입 (`:1227`) | ✅ |
| OFF arm 전면 차단 | `run_pm_phase():776-800` — draft/read/trace가 하나라도 있으면 예외, exposure 0행, no-op checkpoint만 | ✅ |

### 2.2 상한 강제는 이중이며 fail-closed

1. **모델 응답 경계** — `community_provider.py:964-965`: `len(ids) > limit` → 예외
2. **원장 커밋 경계** — `community.py:1889-1891`: 리더별 카운트가 cap 초과 시 예외
3. **사후 검증기** — `community_artifacts.py:1338-1343, 1382-1386`이 동일 cap으로 재검증

truncate 경로는 없다.

### 2.3 ⚠️ D1=5 / D2=10을 코드가 보장하지 않는다

`spec.py:994-997`이 강제하는 것은 `depth2_cap >= depth1_cap`과 음수 금지뿐이다. `depth1=1, depth2=1`인 study_spec도 통과한다. K만 `best_k <= 5` 상한이 코드에 있다(`community.py:638`).

**5/10/5는 저장소에 존재하지 않는 외부 `study_spec.json` 값이다.** (`--study-spec` 인자, `scripts/09_...:42`. 저장소 내 `community_policy` 포함 JSON 검색 결과 0건.) 설계 문서 `FUSE_MEMORY_DESIGN.md:3088-3092`에만 숫자가 있다.

→ **실행 직전 study_spec의 `best_k`, `depth1_selective_read_cap`, `depth2_selective_read_cap` 값을 육안 확인해야 한다.**

### 2.4 depth 불일치 방지는 견고하다

persona prompt는 `news_depth`에서 렌더링되는 파생물이다(`persona_snapshot.py:214-215`, docstring `:189-195`가 "레거시 `persona_prompt` 값을 의도적으로 읽지 않는다"고 명시). 4중 방어:

1. round-trip 바이트 동일성 검사 — `persona_snapshot.py:411-417`
2. 14개 필드 대조 (`news_depth` 포함) — `:702-720`
3. 파서의 canonical depth 정책 검증 — `:264-268`
4. depth manifest 해시 ↔ StudySpec 핀 대조 — `:852-889`, `run_context.py:188-189`

resolver cohort ↔ persona snapshot 교차검증은 `evidence_provider.py:197-200, 235`에서 이루어진다. 다만 이 검증은 evidence provider 생성 시점에만 걸리고, 커뮤니티 서비스는 cohort 쪽 depth를 쓴다(`community.py:690`).

### 2.5 D2 전용 특권은 커뮤니티에 없다

- 작성자 프로필 열람은 **D2 전용이 아니라 D1/D2 공통**이다(`community_provider.py:336-338`, `:373-375` — depth 분기 없음).
- Best broadcast에는 프로필이 아예 없다(`community.py:2396`, `:2486-2487`이 Best 채널 프로필 유출 금지).
- RN에서 D2의 실제 추가 특권은 **뉴스 쪽 1개**뿐: sealed recent-search 레지스트리(`evidence_provider.py:125`, `stages.py:201`이 depth≠2인데 결과가 있으면 fail-close).

`ORIGINAL_EXPERIMENT_DESIGN.md:211`의 "D2는 작성자 최근 거래·포트폴리오를 본다"는 서술은 **RN에서 폐기되었고**, `PublicAuthorProfile`(`community.py:386-424`)이 badges/direction/reliability 3필드로 제한하며 포트폴리오·현금·체결·belief는 필드명 자체가 금지 목록이다(`preflight_inputs.py:59-78`).

---

## 3. 커뮤니티 상호작용 파이프라인

### 3.1 전체 흐름

```
거래일 D — PM event
  runner._event_phase.workflow()                                    runner.py:354-389
   ├ mature_outcomes_for_event()
   ├ community_lifecycle.prepare_event()   ← 전날 보드 배달 (아래 D+1 참조)
   ├ ═══ 순차 barrier ═══
   │   STB → ANALYSIS → DECISION → FILL → POST_FILL_LTB   (100명 × 2arm)
   └ community_lifecycle.after_event()     ← ★ 게시판 생성          :371-378

  after_event() 내부                                       community_lifecycle.py:480-583
   1. board_provider.post_drafts()
        70명(D1/D2) 병렬 → posting_decision LLM 1회씩
        입력: persona + LTB_t + view_change + committed_pm_fill
        will_post=false면 skip trace만 남김
   2. service.preview_candidate_posts()
        본문·프로필 없는 후보 메타만 생성, 서버가 post_id 확정 (내용 해시)
   3. board_provider.selective_reads()
        70명 병렬, 각자
          (a) select : 자기 글 제외 후보에서 최대 5(D1)/10(D2)개 선택
          (b) react  : 선택한 글 전문을 읽고 like / unlike / none
        ※ 후보 보드는 이 단계 시작 전에 동결 → 같은 PM 안에서 남의 반응을 볼 수 없음
   4. finalized_post_traces() / finalized_reader_traces()
   5. finalized_post_drafts()   score = likes − unlikes 적용
   6. service.run_pm_phase()    ★ 단일 SQLite 트랜잭션 커밋
        - community_selected exposure (status=delivered, visible_from=다음 AM)
        - Best = sorted(−score, −like, post_id)[:5] → best_schedule observation
        - 마지막 날이면 best를 right_censored exposure로 기록

거래일 D+1 — AM event
  community_lifecycle.prepare_event()                              :379-478
   6. service.deliver_scheduled_best()
        Best 5를 100명 전원에게 delivered exposure로 삽입 (본문 포함, 프로필 없음)
   7. service.interpretation_payloads()
        에이전트별 노출 조립:
          best_only_body / selected_body / selected_and_best_overlap
          + title_only 후보 (§4-①)
   8. interpretation_provider.interpret()
        community_thinking LLM 1회 → 구조화 JSON
          observed_sentiment
          claims[claim_text, claim_stance, source_exposure_ids, supporting_quote]
          agreement_disagreement, uncertainty
        서버가 supporting_quote의 원문 부분문자열 일치와 노출 소유권을 검증
        (의미·진실성·신뢰도는 판정하지 않음)
   9. service.record_interpretation_claims()
        claim_id 부여 + community_claim_sources junction 생성
  ═══ STB ═══
   community_claims = [{claim_id, claim_text, stance, source_exposure_ids}]   ← 본문 없음
   (stages.py:465-482) → 주로 dim_4 시장심리 / dim_5 사건 해석
  → ANALYSIS → DECISION → FILL → LTB_t   (여기서만 지속)
```

### 3.2 인과 경계

- PM의 게시·읽기·반응은 **그날 PM 주문의 원인이 될 수 없다.** 모두 post-fill LTB 커밋 이후에 일어난다.
- 커뮤니티의 효과는 **다음 거래일 AM STB**를 통해서만 들어간다.
- STB로 넘어가는 것은 원문이 아니라 claim ID·텍스트·stance이고, LTB에는 sanitized registry(evidence_id / kind / lineage_sha256)만 남는다(`stages.py:485-505`).
- interpretation 출력은 제3의 메모리가 아니다. 지속되는 것은 거래 뒤 `LTB_t`가 STB를 재귀 통합한 부분뿐이다.

### 3.3 look-ahead 누출 점검 — 문제 없음 (6중 방어)

1. **실행 순서**: 보드는 그 event의 모든 belief/decision이 끝난 뒤 생성 (`runner.py:354-380`)
2. **PM 전용**: source가 PM이 아니면 예외 (`community_lifecycle.py:312`)
3. **다음 AM은 봉인 calendar가 결정**: 런타임 caller가 대상 event를 고를 수 없음 (`community_lifecycle.py:314-323`, `community.py:1830-1837`)
4. **claim 게이트**: `subturn != "am"`이면 빈 튜플 (`community_lifecycle.py:596-597`, `community.py:2856-2862`)
5. **exposure 쿼리 게이트**: PM에 만들어진 `community_selected` 행은 `event_id=PM`이지만 `visible_from_event_id`가 다음 AM이라 PM STB 조회에 걸리지 않음 (`community.py:1287-1295`)
6. **시각 창**: `_EARLIEST_PM_COMMIT=15:30`, `_LATEST_NEXT_AM_DELIVERY=09:00` (`community.py:119-120`)

### 3.4 exposure 상태 — 전이는 존재하지 않는다

스키마는 3상태를 허용하지만(`db/schema.py:405`), 유일한 writer인 `_insert_exposure`가 **`scheduled`를 명시적으로 거부**한다(`community.py:2259-2260`).

| 시점 | channel | status | delivered_at | event_id |
|---|---|---|---|---|
| PM `run_pm_phase` (`:881-893`) | `community_selected` | `delivered` | `phase.observed_at` | PM event |
| PM 최종일 (`:902-915`) | `community_best` | `right_censored` | NULL | PM event |
| 다음 AM `deliver_scheduled_best` (`:1224-1234`) | `community_best` | `delivered` | AM 배달 시각 | **다음 AM event** |

UPDATE 문이 없다. append-only이고 `status`가 `exposure_id` 해시에 포함되므로(`community.py:2285`) 상태 변경 = 다른 행이 되어 물리적으로 전이가 불가능하다. "예약"은 `observation_events`의 `rn-community-best-schedule-v1` payload에, "노출"은 `agent_exposures`에 기록되는 **2계층 구조**다.

---

## 4. 발견사항

### 🔴 심각 — 실행 전 결정 필요

**① 문서에 없는 4번째 노출 채널: `title_only_candidate`**

D1/D2 리더는 다음 AM 해석에서 **자기 글을 제외한 그날 게시글 전부의 제목·유형·score**를 `content_level="title_only"`로 받고, claim의 `source_exposure_ids`로 인용할 수 있다(`community.py:2825-2844`, `interpretation_payloads():1360`).

그런데 이 노출은 `observation_events`의 reader trace payload에만 저장되고 **`agent_exposures` 테이블에는 행이 없다.**

→ "한 에이전트의 커뮤니티 노출량 = Best 5 + self-read 5~10"이라는 이해가 **틀렸다.** 하루 게시글이 40개면 D1 에이전트의 실제 노출은 5+5가 아니라 5+5+39(title-only)다. `agent_exposures`만으로 노출 통계를 산출하면 리더당 phase당 최대 99건이 누락된다.
**조치**: 노출 카운팅 스크립트가 reader trace observation까지 읽는지 확인. 논문의 노출량 정의를 재작성.

**② `max_tokens=1024` vs 본문 8,000자 캡 — 실질 모순**

`COMMUNITY_CALL_MAX_TOKENS["community_posting"] = 1024` (`community_provider.py:69-76`)인데 검증기는 `content` 8,000자를 허용한다(`:952`). 한국어 8,000자는 대략 4,000~8,000 토큰이다.

모델이 `column`("긴 호흡의 의견") 타입을 골라 길게 쓰면 → 응답 절단 → `finish_reason != "stop"` → `RNStageAdapterError`(`stage_adapter.py:237-238`) → **재시도 없이 전체 paired run 정지.**

45일 × 70명 = 최대 3,150회 호출에서 이 경로가 밟힐 확률은 낮지 않다. 동일 우려가 `community_interpretation` 2048 토큰 vs `claim_text` 2000자 × 다중 claim에도 있다.
**조치**: 유료 run 전 `max_tokens` 상향 또는 프롬프트의 길이 지침 명시. 1순위.

**③ Best broadcast가 자기 글을 걸러내지 않는다**

`community.py:1221`의 배달 루프에 `post.author_agent_id == recipient` 필터가 없다. self-read 경로는 자기 글을 엄격히 배제하는데(`community_provider.py:316`, `community.py:1883`) Best만 비대칭이다.

익명이라 작성자는 자기 글인 줄 모른 채 "타인 의견"으로 해석해 claim을 만들 수 있다. 테스트가 이 동작을 고정하고 있어(`tests/test_rn_ab_community.py:592`) 버그가 아니라 **미결정 설계**로 보인다.
**조치**: 배제 로직 추가 여부를 결정하거나, `self_echo` 플래그로 사후 통제.

### 🟠 중요 — 해석·비용에 영향

**④ 뱃지·평판 채널이 사실상 무력화되어 있다**

모든 작성자의 public profile이 동일 상수다: `{badges:["registered-community-member"], direction:"neutral", reliability:50}` (`preflight_inputs.py:52-58`, 정책명 `uniform-neutral-no-private-runtime-state-v1`). 다르면 preflight가 실패한다(`:525-526`).

그런데 `prompts/community_reading.txt:29`는 선택 기준으로 **"작성자의 배지나 평판이 내 판단에 참고될 수 있는 글"**을 그대로 안내한다. 존재하지 않는 신호를 참조하는 프롬프트 문구이며, 매 호출마다 정보량 0인 토큰을 싣는다.

**⑤ select 단계에는 인기 신호가 전혀 없다**

`community_provider.py:330-341`이 넘기는 후보 필드의 `score`는 이 시점 draft 값이라 **항상 0**이다(반응은 이후 단계에서 적용). 독자는 **제목과 글 유형만 보고** 선택한다.

C-07(동결 스냅샷)의 의도된 결과이지만, "좋아요가 많은 글로 쏠린다" 류의 인기 캐스케이드 해석은 이 설계에서 나올 수 없다. 논문에서 명시 필요.

**⑥ Best 동점 처리가 작성 순서에 유리하다**

`community.py:820`의 tie-break가 `post_id` 오름차순인데 post_id는 내용 해시다. 반응이 0인 날은 사실상 임의 순서로 Best가 정해진다. 설계 문서가 C-06으로 인지하고 있으므로 `zero_engagement_best_count`를 실제로 집계·보고하는지 확인 필요.

**⑦ `view_change` 슬롯이 모델에게 해독 불가능한 값이다**

프롬프트의 `[관점 변화]`에 들어가는 값은 6개 dim의 **SHA-256 hex 문자열 쌍 + evidence ID 배열**이다(`memory.py:284-311`). 자연어 변화 서술이 아니다.

설계 문서 §1012의 "post-writing private context에서만 view_change 허용"을 형식적으로는 준수하지만, 실제 모델 입력으로는 노이즈 + 토큰 비용이다. "관점 변화를 보고 글쓸지 판단한다"는 서술과 실제 정보량이 다르다.

**⑧ fill 정보의 간접 우회 경로**

게시 프롬프트가 당일 체결가·전후 포트폴리오를 포함한다(`community_provider.py:759-767`). 순서상 정당하지만, 모델이 그 숫자를 본문에 쓰면 다음 AM에 100명의 STB로 들어간다.

§5.3의 "`fill_t`는 STB/analysis/decision에 역유입 불가"는 **자기 자신에 대해서만** 코드로 보장된다. 이를 막는 필터는 존재하지 않는다(필터 부재는 확인된 사실).

**⑨ `scheduled` 상태와 `channel='news'`는 dead enum이다**

`agent_exposures`의 `scheduled`는 절대 쓰이지 않고(§3.4), `channel='news'`도 RN 경로에서 한 번도 삽입되지 않는다(뉴스는 `stage_input_registry`로 프롬프트에 직접 투입). 문서의 상태 전이 서술을 수정해야 한다.

### 🟡 확인·주의

**⑩ 실효 커뮤니티 노출일은 45일이 아니라 44일이다.** Day 1 AM은 전날 보드가 없고, 마지막 PM의 Best는 다음 AM이 없어 `right_censored`로만 기록된다(`community.py:902-915`, `:1192-1193`).

**⑪ 길이 cap이 봉인되지 않았다.** 설계 C-11은 manifest freeze를 요구하는데 실제로는 `community.py:92-93`의 하드코딩 8,000/300자다.

**⑫ `supporting_quote`는 정확 부분문자열 매칭이다.** 모델이 한국어 인용에서 조사·띄어쓰기를 바꾸면 실패 → 재시도 없이 run 정지(`community_provider.py:1033-1040`, `community.py:1419-1421`).

**⑬ 무료 로컬 드라이런으로 커뮤니티 경로를 end-to-end로 밟을 수 없다.** `production_ready = isinstance(model, StrictOpenRouterStageModel)`(`community_provider.py:125`)이고, 아니면 `_begin_provider_attempt`가 False를 반환해(`community_lifecycle.py:636-637`) `post_drafts`의 `_require_attempt()`가 예외를 낸다. 유일한 실검증은 유료 P1 canary다.

**⑭ arm 간 시드는 동일하다.** `stage_adapter.py:959-963`과 `community_provider.py:812-816` 모두 `condition_id`를 시드에 포함하지 않는다. paired 비교를 위한 의도적 설계로 보인다. 다만 `temperature=0.2`(0이 아님)이고 커뮤니티 호출은 arm semaphore 밖 별도 세마포어로 돌아 ON arm의 순간 동시성이 더 크다. "유일한 차이"는 **결정론적 재현**이 아니라 **입력 대칭** 수준의 보장이다.

**⑮ `allow_hold=False`에 대한 암묵적 결합.** §1.1 참조. 스펙이 바뀌면 hold 에이전트에서 커뮤니티가 크래시한다.

### 🟢 확인 결과 문제 없음

- **누출**: 독자 payload에 `author_agent_id` 없음(`community.py:2505-2525`), 사금지 필드 블랙리스트(`:46-67`), 자기 글 self-read 차단, OFF arm 0행 강제(`finalization.py:483-486`), STB에 raw 본문 미전달
- **정렬 비결정성**: 순서 민감 지점이 전부 정렬 또는 교환법칙 연산
- **look-ahead**: §3.3의 6중 방어
- **depth 불일치**: §2.4의 4중 방어

### 테스트

- 커뮤니티 4개 스위트 **53건 전부 통과**
- `rn_ab` 전체 283건 중 **1건 실패** — 커뮤니티와 무관: `outputs/daily_news_selection.csv` 해시 드리프트(`source_candidates.py:497`)

---

## 5. 기존 설계 서술(legacy vs RN_COMM_ON 대조표) 검토

팀 내부에서 공유된 "기존 커뮤니티 vs RN_COMM_ON" 대조 서술을 코드와 대조한 결과다.

### 5.1 확인된 부분 — legacy 서술과 결함 진단은 모두 정확하다

legacy 결함 5건 전부 코드로 재현된다.

| 지적된 결함 | 코드 확인 |
|---|---|
| Best5에 본문 없이 ID·제목·유형·점수만 | `community/agent.py:118-129` — `SELECT post_id, title, post_type, score` |
| Depth 0은 Best조차 못 봄 | `core/collect_context.py:79` — `config.ENABLE_COMMUNITY and news_depth >= 1` 게이트 |
| 선택 열람 본문도 다음 날 200자 절단 | `community/thinking.py:72` — `str(post.get("content",""))[:200]` |
| 게시 입력이 6차원이 아니라 belief_summary·view_change | `community/posting.py:47-48` |
| D2에게 작성자 포트폴리오·최근거래 노출 | `simulation.py:866-870` + `community/agent.py:67-73` |

RN 쪽 서술(OFF no-op, D1·2만 능동, Best 본문 전체 100명 배달, LTB 6차원 기반 작성, exposure ID·hash·전달시각 기록, 원문→interpretation→claim→STB 분리, 강제 게시 없음)도 모두 코드와 일치한다.

### 5.2 보정이 필요한 5곳

**① legacy의 "좋아요 수를 보고 선택"은 실질적으로 작동하지 않았다.**
`community/reading.py:196-205`가 like/unlike를 표시하지만, `post_list_snapshot`이 반응 적용 **전에** 동결되고(`simulation.py:823`, 반응은 `:913-920`에서 일괄 적용) 당일 새 글의 `like_count`는 전부 0이다. legacy에서 실제로 작동한 선택 신호는 **제목·유형·익명코드·뱃지**였다. 이 점은 RN도 동일하다(`score`가 항상 0, §4-⑤).

**② 뱃지는 "프라이버시 제한"이 아니라 "신호 제거"다.**
legacy 뱃지 3종은 수익률·자산·누적 좋아요로 실제 계산되어 변별력이 있었다(`community/badge.py:34-51`). RN의 public profile은 70명 전원 동일 상수이고 다르면 preflight가 실패한다(`preflight_inputs.py:52-58`, `:525-526`).
→ "allowlist된 public profile만 노출"은 축소 서술이다. 정확히는 **평판 채널 자체가 제거되었다.**

**③ RN에는 익명 코드가 없다.**
legacy는 `황소-1234` 형태의 안정적 익명 코드로 여러 날에 걸쳐 같은 작성자를 인식할 수 있었다(`community/agent.py:20-24`). RN에는 `anonymous` 관련 코드가 한 건도 없다(rn_ab 전체 검색 0건).
→ 의도적 설계("no stable author identity")지만 대조표에 없는 처치 차이이며, **반복 작성자에 대한 신뢰 형성이 RN에서는 원천적으로 불가능하다.**

**④ "LTB 연동 view_change"는 자연어가 아니다.**
실체는 6개 dim의 SHA-256 hex 쌍 + evidence ID 배열이다(`memory.py:284-311`). legacy의 `view_change`는 자연어였으므로 이 항목은 개선이 아니라 **모델 입장에서는 정보 손실**이다. (§4-⑦)

**⑤ "다음 AM에 Best 원문 전달"이 노출의 전부가 아니다.**
D1/D2는 `title_only` 채널로 그날 자기 글 제외 **모든 글의 제목**을 추가로 받고 claim으로 인용할 수 있다(`community.py:2825-2844`).
→ legacy 비판의 핵심인 "제목만 봤다"가 사라진 게 아니라 `full_body` / `title_only` 두 채널로 **명시 분리**된 것이다. 분리 자체는 개선이지만, title_only가 `agent_exposures`에 기록되지 않아 노출 통계에서 빠진다(§4-①).

### 5.3 대조표에 빠진 항목

| 항목 | 내용 |
|---|---|
| Best 자기 글 | RN은 자기 글이 Best에 오르면 작성자 본인에게도 배달한다(`community.py:1221`). self-read는 자기 글을 배제하는데 Best만 비대칭 (§4-③) |
| 실효 노출일 | Day 1 AM은 전날 보드 없음, 마지막 PM Best는 `right_censored` → **45일이 아니라 44일** (§4-⑩) |
| D0 정보 비대칭 | D0는 뉴스는 제목만 보는데 커뮤니티 Best는 본문 전체를 받는다 |
| 실패 처리 | legacy는 스키마 위반 시 **4회 재시도**(`community/posting.py:55-96`). RN은 **재시도 0회, 즉시 양쪽 arm 롤백 후 run 정지**(`phase_runner.py:263-274`). 견고성은 후퇴했고 `max_tokens=1024` vs 본문 8,000자 캡과 결합하면 실제 위험 (§4-②) |
| fill 의존 | legacy는 `execution_summary`가 비어도 동작(`community/posting.py:37-44`). RN은 fill 행이 없으면 예외 (§1.1) |

### 5.4 결론

"기존 구조의 가장 큰 문제는 Best5가 인기 글 처치를 표방하면서 실제로는 제목·점수만 전달했다"는 진단은 정확하고, RN이 그것을 고친 것도 맞다.

다만 **RN이 동시에 두 개의 사회적 신호를 제거했다는 점**이 대조표에 반영되어 있지 않다 — 뱃지(평판)와 익명코드(작성자 연속성). 논문에서 커뮤니티 처치를 "실제 종목토론방과 유사한 사회적 정보 환경"으로 서술한다면, 현재 RN 세팅은 **작성자 정체성과 평판이 완전히 제거된 익명 게시판**이라는 점을 명시해야 방어된다.

---

## 6. 실행 전 확인 목록

- [ ] `study_spec.json`의 `best_k=5`, `depth1_selective_read_cap=5`, `depth2_selective_read_cap=10` 육안 확인 (§2.3)
- [ ] `community_posting` / `community_interpretation`의 `max_tokens` 상향 또는 프롬프트 길이 지침 추가 (④②)
- [ ] Best broadcast의 자기 글 배제 여부 결정 (④③)
- [ ] 노출 통계 스크립트가 `title_only_candidate`를 포함하는지 확인 (④①)
- [ ] `community_reading.txt`의 "배지·평판" 문구 처리 결정 (④④)
- [ ] `zero_engagement_best_count` 집계 경로 확인 (④⑥)
- [ ] 논문에서 커뮤니티 처치를 "평판·작성자 연속성이 제거된 익명 게시판"으로 명시 (§5.2 ②③)
- [ ] `outputs/daily_news_selection.csv` 해시 드리프트 해소

---

## 부록. 코드 경로 색인

| 관심사 | 위치 |
|---|---|
| event stage 순서 | `rn_ab/runner.py:354-389` |
| 커뮤니티 lifecycle | `rn_ab/community_lifecycle.py:379`(prepare) / `:480`(after) |
| 게시 LLM 호출 | `rn_ab/community_provider.py:180-286` |
| 읽기/반응 LLM 호출 | `rn_ab/community_provider.py:288-428` |
| 해석 LLM 호출 | `rn_ab/community_provider.py:490+` |
| 게시 컨텍스트 조립 | `rn_ab/community_provider.py:726-782` |
| 보드 동결·Best 산정 | `rn_ab/community.py:753-930` |
| Best 배달 | `rn_ab/community.py:1175-1269` |
| 노출 payload 조립 | `rn_ab/community.py:1271-1360` |
| claim 검증 | `rn_ab/community.py:1362-1541`, `stages.py:465-482` |
| depth cap | `rn_ab/community.py:713-718` |
| 정책 파싱 | `rn_ab/spec.py:982-1013` |
| public profile 생성 | `rn_ab/preflight_inputs.py:377-395` |
| 프롬프트 슬롯 정의 | `rn_ab/prompt_registry.py:144-165` |
| 프롬프트 원본 | `prompts/posting_decision.txt`, `prompts/community_reading.txt`, `prompts/community_thinking.txt` |
| 스키마 | `db/schema.py:395-408`(exposures), `:448-492`(post_trace) |
