# TwinMarket Korea Architecture

이 문서는 현재 코드 기준의 핵심 구조만 정리한다. 과거 구현 계획서와 제안서는 이 문서와 `README.md`로 통합했다.

## 1. 실행 단위

시뮬레이션은 거래일마다 `am`, `pm` 두 개의 의사결정 턴을 실행한다.

```text
거래일 D
  am turn: 시가 기준 판단 및 체결
  pm turn: 종가 기준 판단 및 체결
  community phase: pm 체결 이후 게시글/읽기/반응/Best 선정
```

기본 정보 모드는 `pre_close_cutoff`이다.

- `am`: 전 거래일 15:30 이후부터 당일 08:59까지의 뉴스, 전일 시장 피처
- `pm`: 당일 08:59 이후부터 15:30까지의 뉴스, 당일 시장 피처
- 15:30 이후 뉴스는 다음 거래일 `am` 뉴스 윈도우에 들어간다.

## 2. 주요 모듈

| 모듈 | 역할 |
| --- | --- |
| `twinmarket_kr/simulation.py` | 전체 실행 루프, 날짜/턴 구성, 체결, 포트폴리오 갱신, 커뮤니티 단계 |
| `twinmarket_kr/core/daily_cycle.py` | 에이전트 1명의 context -> news interpretation -> belief -> analysis -> decision 흐름 |
| `twinmarket_kr/core/collect_context.py` | 이전 belief, 포트폴리오, 주문 이력, 시장, 뉴스, 커뮤니티 로그 수집 |
| `twinmarket_kr/agents/news_agent.py` | 뉴스 CSV 로드, 턴별 뉴스 윈도우 구성, Depth 2 검색 |
| `twinmarket_kr/agents/fundamental_agent.py` | `StockData` 로드 및 시장 피처 조회 |
| `twinmarket_kr/agents/memory_agent.py` | belief, trade log, portfolio state 저장/조회 |
| `twinmarket_kr/agents/exchange_agent.py` | 주문 검증과 공시가 기반 체결 |
| `twinmarket_kr/community/` | 게시글 작성, 게시글 읽기/반응, 뱃지, community thinking |
| `twinmarket_kr/llm/` | OpenRouter 클라이언트와 LLM 단계별 JSON 파싱 |
| `twinmarket_kr/run_logger.py` | 실행별 CSV/JSONL 로그 |

## 3. 에이전트 턴 흐름

`run_agent_turn()`의 현재 순서는 다음과 같다.

1. `collect_context()`로 이전 상태와 현재 턴 정보를 수집한다.
2. `NewsAgent`가 Depth별 뉴스 컨텍스트를 확장한다.
3. Depth 2 에이전트는 키워드 검색을 수행하고 검색 결과를 추가로 읽는다.
4. LLM이 뉴스 해석을 생성한다.
5. 커뮤니티가 켜져 있으면 community thinking을 병렬 생성한다.
6. LLM이 belief를 업데이트하고 DB에 저장한다.
7. LLM이 시장 분석을 생성한다.
8. LLM이 buy/sell 결정을 생성한다.
9. 주문 가능 수량과 보유/현금 제약을 검증한다.
10. 로그를 남기고 주문을 반환한다.

## 4. 뉴스 시스템

뉴스 런타임 입력은 원천 pkl이 아니라 CSV 두 개다.

- `outputs/processed_news.csv`: 전체 정제 뉴스와 요약
- `outputs/daily_news_selection.csv`: 날짜별 기본 노출 뉴스 목록

`scripts/02_prepare_news.py`는 원천 `data/samsung_news_raw.pkl`을 읽어 두 CSV를 만든다. 기본 선정 목표는 종목 5개, 섹터 3개, 경제 2개이며 시간대별 5:5 배분은 보장하지 않는다.

Depth별 동작:

- Depth 0: 제목 중심
- Depth 1: 제목과 요약 본문
- Depth 2: Depth 1 + LLM 키워드 기반 `processed_news.csv` 검색 결과

## 5. 주문과 체결

현재 `decision_space`는 `buy_sell_only`이다. 에이전트는 `buy` 또는 `sell` 결정을 내리며, 보유 수량이나 현금 제약상 불가능한 주문은 제출되지 않는다.

`ExchangeAgent`는 제출된 주문을 현재 턴의 공시 가격으로 체결한다.

- `am` 체결 가격: 당일 시가
- `pm` 체결 가격: 당일 종가
- 매수는 현금 한도 안에서만 가능하다.
- 매도는 보유 수량 안에서만 가능하다.
- 현재 체결 엔진은 별도 가격 발견이나 호가 경쟁을 수행하지 않는다.

## 6. 포트폴리오와 로그

체결 후 `portfolio_state`가 갱신된다.

- 매수: 현금 감소, 평균 단가 갱신
- 매도: 보유 수량 감소, 실현손익 갱신
- 매 턴 현재 가격 기준으로 미실현손익과 총자산을 기록

주요 로그는 `outputs/logs/<run_id>/`에 남는다.

- `agent_turns.jsonl`: 가장 상세한 컨텍스트와 LLM 출력
- `agent_turns.csv`: 분석에 쓰기 쉬운 요약
- `submitted_orders.csv`: 제출 주문
- `exchange_fills.csv`: 체결
- `daily_exchange_summary.csv`: 턴별 체결 요약
- `portfolio_updates.jsonl`: 포트폴리오 상태 변화
- `community_*.csv/jsonl`: 커뮤니티 단계 로그

## 7. 커뮤니티

커뮤니티는 `config.py`의 세 플래그로 제어한다.

```python
ENABLE_COMMUNITY
ENABLE_COMMUNITY_POSTING
ENABLE_COMMUNITY_READING
```

`pm` 체결 이후:

1. Depth 1 이상 에이전트가 글을 작성할 수 있다.
2. 뱃지와 Best 게시글을 계산한다.
3. 에이전트가 자기 글을 제외한 게시글을 읽고 반응한다.
4. 다음 거래일 컨텍스트에 들어갈 community log를 저장한다.

## 8. 검증과 리포트

검증의 기본 질문은 "시뮬레이션 에이전트들의 일별 순매수/순매도 방향이 실제 개인 투자자 순거래 방향과 얼마나 맞는가"이다.

- 검증 스크립트: `validation/validate_trading_direction.py`
- 실제 데이터: `validation/data_trading_value.csv`, `validation/data_trading_volume.csv`
- 기본 출력: `validation/outputs/<run_id>/`

리포트 스크립트:

- `scripts/generate_run_report_pdf.py`
- `scripts/generate_community_report_pdf.py`
- `scripts/generate_deep_analysis_report.py`

## 9. 실험 확장

가짜뉴스 주입은 `scripts/07_prepare_fake_news_injection.py`가 bearish/bullish phase-review pkl을 런타임 CSV로 변환한 뒤 실행한다. 상세 절차는 `Event_Fake_News_DB_Guide.md`를 따른다. 핵심 원칙은 다음이다.

- 기존 실제 뉴스는 제거하지 않고 가짜뉴스를 추가한다.
- `fake-news-mode=on`일 때만 가짜뉴스가 기본 노출과 Depth 2 검색 대상에 포함될 수 있다.
- 에이전트 입력에는 fake 라벨을 절대 노출하지 않는다.
- fake 관련 라벨은 사후 분석 로그에만 남긴다.
