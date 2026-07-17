# TwinMarket Korea

삼성전자(`005930`)를 대상으로 LLM 기반 투자자 에이전트가 뉴스·시장 정보·포트폴리오·커뮤니티 정보를 바탕으로 매수 또는 매도 주문을 내는 시장 시뮬레이션입니다.

```text
시장/뉴스 데이터 준비 → 100명 페르소나 구성 → 초기 상태 생성
→ 거래일별 am·pm 의사결정 및 체결 → 로그·리포트·실제 거래 방향 검증
```

## 문서

| 문서 | 용도 |
| --- | --- |
| `ARCHITECTURE.md` | 현재 실행 구조와 데이터 흐름 |
| `Code_Status.md` | 유지해야 할 핵심 구현 결정 |
| `Event_Fake_News_DB_Guide.md` | 이벤트·가짜뉴스 데이터와 주입 CSV 생성 |
| `fake_news_injection_experiment.md` | 가짜뉴스 비교 실험 설계 |
| `fake_news_phase_stimulus_review.md` | phase-review 자극의 검토 기준과 현황 |
| `validation/README.md` | 실제 개인 투자자 거래 방향 검증 |
| `News_Scraper/README.md` | 뉴스 수집 보조 도구 |

`twinmarket_micro_behavior_research_plan.md`은 논문 작업용 별도 문서이며 이 운영 안내의 범위에 포함하지 않습니다.

## 설치와 설정

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

데이터 수집과 PDF 리포트에는 `yfinance`, `matplotlib`, `reportlab` 등 추가 패키지가 필요할 수 있습니다. LLM 실행 전에는 프로젝트 루트의 `.env`에 사용할 OpenRouter 설정을 명시합니다.

```dotenv
OPENROUTER_API_KEY=...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=...
OPENROUTER_COMMUNITY_MODEL=...
```

모델은 실험 중 임의로 바꾸지 않습니다. 자세한 기준은 `API_MODEL_USAGE_NOTICE.md`를 따릅니다.

## 기본 데이터 준비

아래 순서는 새 환경에서 기본 상태를 만드는 순서입니다.

```bash
python scripts/00_fetch_market_data.py
python scripts/01_build_persona.py
python scripts/02_prepare_news.py --seed 2
python scripts/03_load_stock_data.py
python scripts/02_init_memory.py
python scripts/04_generate_initial_beliefs.py --offline
python scripts/99_validate.py
```

LLM으로 초기 belief를 만들려면 마지막 전 단계에서 `--offline`을 뺍니다. 주요 입출력은 다음과 같습니다.

| 단계 | 입력 | 출력 |
| --- | --- | --- |
| 시장 데이터 | 외부 시세 | `data/stock_data.csv`, `data/macro_data.csv` |
| 페르소나 | `data/sys_1000.csv` | `outputs/sys_100.db` |
| 뉴스 전처리 | `data/samsung_news_raw.pkl` | `outputs/processed_news.csv`, `outputs/daily_news_selection.csv` |
| 시장 DB 적재·초기화 | 위 산출물 | `outputs/sim.db` |

`02_init_memory.py`는 `sim.db`의 초기 포트폴리오 상태를 만듭니다. 같은 DB로 새 실험을 시작할 때에는 기존 런타임 상태에 유의해야 합니다.

## 시뮬레이션

```bash
python scripts/05_run_simulation.py \
  --max-agents 30 \
  --seed 2 \
  --start-date 2026-02-27 \
  --end-date 2026-06-01 \
  --community-mode off
```

주요 옵션:

| 옵션 | 설명 |
| --- | --- |
| `--max-agents`, `--max-days` | 에이전트 수와 거래일 수 제한 |
| `--start-date`, `--end-date` | 실행 구간 |
| `--seed` | 재현용 표본 seed |
| `--information-mode` | `pre_close_cutoff`(기본), `prior_close`, `same_day` |
| `--community-mode` | `on` 또는 `off` |
| `--processed-news-csv`, `--daily-news-csv` | 런타임 뉴스 입력을 명시적으로 교체 |
| `--sim-db` | 실험별 SQLite DB 경로 지정 |
| `--no-logs` | 상세 실행 로그 비활성화 |

기본 `pre_close_cutoff`에서 am은 전 거래일 15:30 이후부터 당일 08:59까지, pm은 당일 08:59 이후부터 15:30까지의 뉴스를 사용합니다. 주문은 `buy_sell_only`이며 am은 시가, pm은 종가로 체결됩니다.

짧은 커뮤니티 점검은 다음과 같이 실행합니다.

```bash
python scripts/06_run_community_smoke_test.py --max-agents 3 --max-days 2
```

## 가짜뉴스 주입 실험

먼저 두 polarity의 주입용 CSV를 생성합니다.

```bash
python scripts/07_prepare_fake_news_injection.py --variant both
```

이 명령은 `outputs/*_injection_bearish.csv`와 `outputs/*_injection_bullish.csv`를 만듭니다. phase-review 행도 기본으로 포함하며, `--approved-only`는 `final_approval=true` 행만 사용합니다.

```bash
# bearish 자극 노출
python scripts/05_run_simulation.py \
  --use-fake-news-injection --fake-news-variant bearish \
  --fake-news-mode on --community-mode off

# 동일한 주입 CSV를 읽되 fake 행은 에이전트에게 숨김
python scripts/05_run_simulation.py \
  --use-fake-news-injection --fake-news-variant bearish \
  --fake-news-mode off --community-mode off
```

`fake-news-mode=on`일 때만 `is_fake=true` 행이 기본 뉴스, 본문 읽기, Depth 2 검색 후보에 들어갑니다. 에이전트가 읽는 입력에서는 fake 관련 메타데이터를 제거합니다.

## 로그·리포트·검증

실행 로그는 `outputs/logs/<run_id>/`에 저장됩니다. 핵심 파일은 `run_metadata.json`, `run_complete.json`, `agent_turns.csv/jsonl`, `submitted_orders.csv`, `exchange_fills.csv`, `daily_exchange_summary.csv`, `portfolio_updates.jsonl`, `community_*.csv/jsonl`, `errors.jsonl`입니다.

```bash
python scripts/generate_run_report_pdf.py \
  --run-dir outputs/logs/<run_id> \
  --output outputs/reports/<run_id>_report.pdf

python scripts/generate_fake_news_report_pdf.py \
  --run-dir outputs/logs/<fake_run_id> \
  --baseline-run-dir outputs/logs/<baseline_run_id> \
  --output outputs/reports/<fake_run_id>_fake_news.pdf

python validation/validate_trading_direction.py \
  --run-dir outputs/logs/<run_id>
```

검증 결과는 `validation/outputs/<run_id>/`에 생성됩니다. 실행 완료 여부와 실제 조건은 특정 과거 표가 아니라 각 run의 `run_metadata.json` 및 `run_complete.json`을 기준으로 확인합니다.
