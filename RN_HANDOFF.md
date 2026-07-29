# RN Community A/B — 인수인계 (봉인 완료 → canary 대기)

> 작성: 2026-07-29 · 상태: **입력 봉인 완료, `09` preflight exit 0 통과, canary 준비 완료**
> 이 문서 하나로 다른 터미널/세션이 이어받을 수 있게 정리함.
> 관련 문서: [`RN_FULL_PIPELINE.md`](RN_FULL_PIPELINE.md) · [`RN_STB_LTB_REVIEW.md`](RN_STB_LTB_REVIEW.md)

---

## 0. 한 줄 요약

45거래일(2026-02-27~05-04) × 100 agent × 2 arm(RN_COMM_OFF/ON) 실뉴스 A/B.
**입력 전체 봉인 완료 → 유료 canary 인가만 남음.** 코드 안 건드리면 지금 상태로 나중에 실행 가능.

## 1. 실행 환경 (중요)

- **반드시 venv 사용**: `.venv/bin/python` (Python 3.12). 시스템 `python3`(3.9)는 `zip(strict=)` 등에서 실패.
- 작업 루트: `/Users/sujinjung/Desktop/felab/aicp/2026_AICP_ver2.0-sujin`
- API key: `.env`에 OpenRouter 키 존재 ✅

## 2. 지금까지 완료된 것 ✅

| 영역 | 상태 |
|------|------|
| 뉴스 필터링/요약 (5 split 폴더, 9,710 유지) | ✅ |
| persona depth 매치 (100/100, D0=30/D1=55/D2=15) | ✅ 수리 완료 |
| fake-news 배제 (코드 fail-closed) | ✅ |
| STB/LTB 메모리 로직 검증 | ✅ |
| 뉴스 provenance 바인딩 + 번들 (760 슬롯, 90/90 event) | ✅ |
| 전체 sealed 입력 세트 + StudySpec | ✅ |
| **`09` preflight** (양 arm DB + RUN_RECORD 생성) | ✅ exit 0 |
| canary 준비 (`status`/`prepare-telemetry` 비유료 통과) | ✅ |

## 3. 생성한 스크립트 (봉인 파이프라인)

순서대로 실행하면 sealed 입력이 재생성됨. **모두 `.venv/bin/python`으로 실행.**

```bash
# ① 뉴스 provenance 바인딩 (crawl+split → observed_at=effective_at, fake/EOD 마커 격리)
.venv/bin/python scripts/13_bind_news_provenance.py
# → preparation/rn_ab_source_candidate_v1/provenance_bound/

# ② 90 event 슬롯 매핑 + SealedNewsRegistry 검증
.venv/bin/python scripts/14_seal_news_bundle.py
# → preparation/rn_ab_source_candidate_v1/real_news_bundle_manifest.json

# ③ cohort/calendar/stage-input/price/injection/review/StudySpec 생성
.venv/bin/python scripts/15_seal_rn_study.py
# → preparation/rn_ab_sealed_v1/  (study_spec.json 외 8개 + prompts/)
```

## 4. sealed 입력 위치

`preparation/rn_ab_sealed_v1/`:
`study_spec.json`, `cohort.json`, `calendar.json`, `stage_inputs.json`,
`prices.json`, `news.json`, `known_injection.json`, `review.json`, `prompts/`
+ persona snapshot은 `preparation/rn_ab_persona_snapshot_v1/` (기존, 이미 봉인)

## 5. preflight 재실행 (검증, 유료 아님)

```bash
S=preparation/rn_ab_sealed_v1
mkdir -p $S/run
.venv/bin/python scripts/09_run_realnews_community_ab.py --preflight \
  --run-id rn_seal_test --input-root preparation --output-root $S/run \
  --study-spec $S/study_spec.json --cohort-registry $S/cohort.json \
  --persona-snapshot-dir preparation/rn_ab_persona_snapshot_v1 --prompt-dir $S/prompts \
  --calendar-event-registry $S/calendar.json --stage-input-registry $S/stage_inputs.json \
  --event-price-registry $S/prices.json --real-news-bundle $S/news.json \
  --known-injection-registry $S/known_injection.json \
  --article-version-leakage-review-manifest $S/review.json
# 성공 시 exit 0 + run/rn_seal_test/ 아래 양 arm DB·RUN_RECORD 생성
```

## 6. Canary 실행 (다음 단계 — 유료)

preflight run_dir = `preparation/rn_ab_sealed_v1/run/rn_seal_test`

```bash
RD=preparation/rn_ab_sealed_v1/run/rn_seal_test

# (비유료) 게이트 점검 / telemetry 계획
.venv/bin/python scripts/12_operate_realnews_community_ab.py status --run-dir $RD
.venv/bin/python scripts/12_operate_realnews_community_ab.py prepare-telemetry --run-dir $RD
#   → 800 호출 예상, REASONING_OFF_TELEMETRY_PLAN.json 생성 (이미 확인됨)

# (유료 ①) reasoning-off 실측 telemetry — 첫 유료 호출
.venv/bin/python scripts/12_operate_realnews_community_ab.py telemetry \
  --run-dir $RD --authorize-paid-api-calls --confirm-run-id rn_seal_test

# (유료 ②) P1 canary (첫 2거래일, ~$2)
.venv/bin/python scripts/12_operate_realnews_community_ab.py run-p1 \
  --run-dir $RD --authorize-paid-api-calls --confirm-run-id rn_seal_test

# (유료 ③) 본실행 — P1 통과 후 (~$33-38)
.venv/bin/python scripts/12_operate_realnews_community_ab.py run \
  --run-dir $RD --p1-run-dir $RD --authorize-paid-api-calls --confirm-run-id rn_seal_test

# (비유료) 마감/검증
.venv/bin/python scripts/12_operate_realnews_community_ab.py finalize --run-dir $RD --p1-run-dir $RD
.venv/bin/python scripts/12_operate_realnews_community_ab.py validate-final --run-dir $RD
```

canary에서 반드시 확인: returned model/provider, `reasoning_tokens=0`, empty reasoning fields.

## 7. 확정된 방법론 결정 (재봉인 시 유지)

- **study window**: 45거래일 2026-02-27~05-04 (90 event, burn-in 3일, 주분석 42일). 확장 안 함.
- **노출시각**: `effective_at = max(published_at, modified_at)`.
- **observed_at = effective_at** (7월 크롤 시각 아님; validate_delivery의 observed<=cutoff 통과 위함).
- **뉴스 윈도우**: AM=전날 15:30~당일 08:59 / PM=당일 08:59~15:30. event_id=`{date}/AM|PM`.
- **버킷 쿼터**: 종목5/섹터3/경제2(합10), 부족하면 부족한 채 진행(shortage_accepted).
- **필터링**: 5폴더 모두 N=유지. **fake 없음** (fake_news_per_event=0, 코드 강제).
- **모델**: `qwen/qwen3.5-flash-02-23`, reasoning `{effort:none, exclude:true}`, 수수료 0.

## 8. ⚠️ 실행 전 남은 결정/caveat

| # | 항목 | 성격 | 조치 |
|---|------|------|------|
| 1 | **파일럿 vs 논문급** | 목적 결정 | 파일럿이면 지금 상태로 canary. 논문급이면 아래 2,3 먼저 |
| 2 | StudySpec 정책 해시 5개 placeholder<br>(regime/retry/runtime/evaluation/market-feature) | 논문급만 | `scripts/15`의 `_digest(...)` → 실제 검토 정책 문서 해시로 교체 |
| 3 | leakage review candidate 2026-04-27 1건만 | 논문급만 | 나머지 flagged 후보 검토 추가 |
| 4 | **canary 유료 인가** | 사용자 결정 | `--authorize-paid-api-calls` (제3자 대행 불가) |

**추천 순서**: 파일럿 canary(~$2)로 reasoning-off·누출·비용 실측 → 통과하면 그때 논문급 정책 문서 채움(헛일 방지).

## 9. 재봉인 필요 조건 (중요)

StudySpec의 `baseline_commit`에 **현재 git HEAD**가 박혀 있음. preflight/run이 체크아웃 commit과 대조.
→ **canary 전에 코드를 커밋/변경하면** 불일치로 거부. 그땐 `.venv/bin/python scripts/15_seal_rn_study.py` 재실행(commit 갱신) 후 preflight 다시.
코드 안 건드리면 지금 상태 그대로 언제든 실행 가능.

## 10. 확인된 사실 (참고)

- preflight 산출: `preparation/rn_ab_sealed_v1/run/rn_seal_test/` (resolved_manifest, evaluator_contract, 양 arm `paper_run.sqlite` ~760KB, RUN_RECORD.json/.md, source_hashes.json)
- RUN_RECORD 확인: `reasoning: {"effort": "none"}`, `fake count is 0`
- 뉴스 번들: 760 기사/슬롯, 90/90 event 커버(31 완비 + 59 shortage)
