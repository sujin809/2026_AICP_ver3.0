# 가짜뉴스 주입 실험 설계

## 목적

동일한 시장·실제 뉴스·에이전트 구성에서 가짜뉴스 노출이 거래 판단과 성과에 미치는 영향을 비교한다. 이 문서는 구현 가능한 최소 설계를 기록하며, 자극 데이터의 세부 검토는 별도 review 문서에 둔다.

## 조건

| 조건 | 뉴스 입력 |
| --- | --- |
| Baseline | `processed_news.csv`, `daily_news_selection.csv`의 실제 뉴스만 사용 |
| Injection | 같은 baseline 뉴스에 bearish 또는 bullish fake 행을 추가하고 노출 |
| CSV control | 같은 주입 CSV를 읽되 fake 행을 숨김 |

Injection은 실제 뉴스를 치환하지 않는다. 따라서 fake가 있는 날의 뉴스 수가 늘어날 수 있으며, 정보량 효과를 분리하려면 실제 뉴스 한 건을 추가한 placebo 조건을 별도로 설계해야 한다.

## 자극과 주입

- 자극은 `data/fake_news_<variant>_phase_review.pkl`에서 관리한다.
- `scripts/07_prepare_fake_news_injection.py`가 agent-visible 필드를 가진 polarity별 런타임 CSV를 만든다.
- predictable 이벤트는 `D-2…D+2`, unpredictable 이벤트는 `D…D+2`에만 배치한다.
- 같은 날짜에는 fake 행을 하나만 배치한다.
- fake 라벨과 이벤트·검토 메타데이터는 프롬프트, 기본 뉴스, Depth 2 검색, 커뮤니티 입력에 노출하지 않는다.

## 실행 원칙

두 조건은 아래 요소를 동일하게 유지한다.

- 기간, seed, 에이전트 수·순서, `news_depth` 구성
- 시장 데이터, baseline 실제 뉴스, 초기 포트폴리오와 belief
- 정보 컷오프, community 설정, 모델 설정, 주문 공간

조건별로 별도 `--sim-db`를 쓰거나 초기 상태를 동일하게 재구성해 상태가 섞이지 않도록 한다. 실행별 정확한 옵션은 `run_metadata.json`으로 기록·검증한다.

예시:

```bash
python scripts/07_prepare_fake_news_injection.py --variant both

python scripts/05_run_simulation.py \
  --max-agents 30 --seed 2 \
  --start-date 2026-02-27 --end-date 2026-06-01 \
  --community-mode off --fake-news-mode off

python scripts/05_run_simulation.py \
  --max-agents 30 --seed 2 \
  --start-date 2026-02-27 --end-date 2026-06-01 \
  --community-mode off \
  --use-fake-news-injection --fake-news-variant bearish \
  --fake-news-mode on
```

## 평가

- 성과: 누적·일별 PnL, 수익률, 변동성, 최대 낙폭
- 거래 행태: 주문 수, 매수/매도 방향, 체결 수량, 포지션 변화
- 노출: 기본 노출·본문 읽기·Depth 2 검색·선택 뉴스에서의 fake 노출 여부
- 비교: 전체 기간뿐 아니라 실제 주입 날짜와 비주입 날짜를 나누어 본다.

가짜뉴스 영향 보고서는 `scripts/generate_fake_news_report_pdf.py`로 만들며, baseline run을 함께 주면 공통 에이전트·날짜 기준 비교를 추가한다. 실제 개인 투자자의 방향과의 비교는 별도 검증이며 `validation/README.md`를 따른다.

## 해석 제한

이 실험은 뉴스 입력에 대한 시뮬레이션 에이전트의 민감도를 측정한다. 실제 시장 참여자나 실제 기업 성과에 가짜뉴스가 동일한 영향을 준다는 인과 주장으로 확장하지 않는다.
