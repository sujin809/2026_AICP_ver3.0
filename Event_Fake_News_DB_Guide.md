# Event / Fake News DB Guide

이 문서는 현재 가짜뉴스 주입 데이터와 런타임 CSV 생성 절차를 설명한다. 자극의 문안별 검토는 `fake_news_phase_stimulus_review.md`, 실험 비교 원칙은 `fake_news_injection_experiment.md`를 따른다.

## 데이터와 역할

| 파일 | 역할 |
| --- | --- |
| `data/event.pkl` | 현재 승인된 9개 source pair의 30개 날짜별 주입 일정·근거 뉴스 |
| `data/fake_news_bearish_phase_review.pkl` | bearish phase-review 자극 |
| `data/fake_news_bullish_phase_review.pkl` | bullish phase-review 자극 |
| `outputs/processed_news.csv` | baseline 전체 뉴스 풀 |
| `outputs/daily_news_selection.csv` | baseline 일별 기본 노출 뉴스 |

`event.pkl`은 현재 가짜뉴스 자극의 일정·근거 DB이고, 런타임에서 직접 읽는 파일은 아닙니다. 런타임에는 아래 스크립트가 만든 polarity별 CSV를 사용합니다.

```bash
python scripts/07_prepare_fake_news_injection.py --variant both
```

생성물:

- `outputs/processed_news_injection_bearish.csv`
- `outputs/daily_news_selection_injection_bearish.csv`
- `outputs/processed_news_injection_bullish.csv`
- `outputs/daily_news_selection_injection_bullish.csv`
- `outputs/fake_news_injection_manifest_<variant>.json`

## 주입 규칙

- baseline 실제 뉴스는 제거하지 않고 fake 행을 추가한다.
- 같은 날짜에는 최대 한 개의 fake 행만 추가한다.
- predictable 이벤트는 `D-2`부터 `D+2`, unpredictable 이벤트는 `D`부터 `D+2`만 허용한다.
- 이벤트 결과가 아직 알려지지 않은 timestamp에는 결과·확정 수치·타결 여부를 자극에 쓰지 않는다.
- target timestamp는 배치 기준일 뿐, 기준 실제 뉴스를 치환하지 않는다.

따라서 fake가 있는 날짜는 baseline보다 뉴스 수가 하나 많을 수 있다. 정보량 증가 효과를 분리하려면 별도 placebo 조건을 둔다.

## 안전한 런타임 변환

`07_prepare_fake_news_injection.py`가 다음을 수행한다.

1. leakage-safe 행만 취급한다.
2. 기본값에서는 phase-review 행도 포함한다. `--approved-only`는 최종 승인 행으로 한정한다.
3. baseline 뉴스 CSV에 fake 행을 append한다.
4. `is_fake`, 연결 이벤트, 허위 주장, 검토·leakage 정보 등 에이전트 비노출 필드를 CSV에서 제거한다.
5. manifest에 사후 분석용 날짜별 주입 정보를 남긴다.

직접 pkl을 병합하지 말고 이 스크립트를 사용한다. 공개 입력과 분석용 원본을 섞으면 라벨 leakage를 만들기 쉽다.

## 실행

```bash
# 주입 CSV 준비
python scripts/07_prepare_fake_news_injection.py --variant bearish

# 자극을 에이전트에게 노출
python scripts/05_run_simulation.py \
  --use-fake-news-injection --fake-news-variant bearish \
  --fake-news-mode on

# 동일 CSV의 fake 행을 숨긴 대조 실행
python scripts/05_run_simulation.py \
  --use-fake-news-injection --fake-news-variant bearish \
  --fake-news-mode off
```

`--use-fake-news-injection`을 주면 선택한 variant의 주입 CSV가 기본 경로로 사용됩니다. `fake-news-mode=off`에서는 fake 행이 기본 노출, 본문 읽기, Depth 2 검색 후보에서 모두 제외됩니다.

## 원본 스키마에서 중요한 필드

| 범주 | 예시 필드 | 용도 |
| --- | --- | --- |
| 식별·배치 | `synthetic_id`, `date`, `timestamp`, `time_slot`, `feed_slot` | 자극 식별과 노출 시점 |
| 문안 | `title`, `content`, `summary`, `category`, `source` | 에이전트에 보이는 뉴스 내용 |
| 이벤트·정책 | `event_id`, `linked_event_id`, `injection_phase`, `injection_offset`, `injection_window_policy` | 연구·검토 추적 |
| 안전성 | `leakage_safe`, `uses_future_event_details`, `can_use_event_outcome`, `final_approval` | 사전 검토와 필터링 |
| 평가 | `claim_polarity`, `misinformation_type`, `false_claim`, `correct_fact` | 사후 분석 |

마지막 세 범주의 메타데이터는 에이전트 입력으로 전달하지 않는다.
