# Phase Injection 자극 검토

이 문서는 phase-review 자극의 운영 기준만 남긴다. 날짜별 개별 문안과 근거는 실행 원본인 `data/fake_news_bearish_phase_review.pkl`, `data/fake_news_bullish_phase_review.pkl`에서 관리한다. Markdown에 전체 문안을 중복 보관하지 않는다.

## 현재 구성

- polarity: `bearish`, `bullish`
- 각 polarity: 30개 paired 자극
- phase: `사전 관측`, `전망 확산`, `관련 소식`, `후속 관측`, `시장 반영`
- 원본 행은 모두 `is_fake=true`이며 `leakage_safe=true` 상태로 검토된다.
- `final_approval`은 별도 인간 검토 절차의 상태다. 현재 런타임 CSV 생성은 leakage-safe phase-review 행을 기본 포함하고, `--approved-only`를 주면 승인 행만 포함한다.

## 검토 기준

1. 자극은 연결된 실제 이벤트의 사실 앵커를 벗어나지 않는다.
2. 이벤트 시점 이전에는 결과·계약 확정·실적 수치처럼 미래를 아는 표현을 쓰지 않는다.
3. 예측 불가능 이벤트에는 사전 주입을 만들지 않는다.
4. bullish/bearish 쌍은 같은 앵커와 phase에서 방향성 주장만 달라지도록 관리한다.
5. `허위`, `가짜뉴스`, `검증되지 않음`처럼 fake임을 직접 알리는 표현을 에이전트가 볼 텍스트에 넣지 않는다.
6. 원본의 `false_claim`, `correct_fact`, `why_false_or_misleading` 등 평가 메타데이터는 런타임 CSV에서 제거한다.

## 변경 절차

1. pkl 원본에서 pair·날짜·timestamp·사실 앵커·leakage 필드를 검토한다.
2. 변경 후 두 polarity의 pair 정합성과 `leakage_safe`를 재검증한다.
3. `python scripts/07_prepare_fake_news_injection.py --variant both`를 실행해 주입 CSV와 manifest를 다시 만든다.
4. 작은 실행으로 fake 노출 로그와 agent-visible 컬럼을 확인한다.

상세 데이터 구조와 실행 방법은 `Event_Fake_News_DB_Guide.md`를 참조한다.
