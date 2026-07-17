# Code Status

이 파일은 현재 코드에서 유지해야 할 핵심 구현 결정을 짧게 기록한다. 오래된 단계별 구현 로그는 `README.md`와 `ARCHITECTURE.md`로 통합했다.

## 현재 기준

- 대상 종목은 삼성전자 `005930`이다.
- LLM 백엔드는 OpenRouter 호환 API를 사용한다.
- 주요 상태 저장소는 `outputs/sim.db` SQLite DB이다.
- 선발된 100명 에이전트는 `outputs/sys_100.db`에 저장된다.
- 실행별 상세 로그는 `outputs/logs/<run_id>/`에 저장된다.

## 데이터와 페르소나

- 페르소나 후보 입력은 `data/sys_1000.csv`를 기준으로 한다.
- `data/fixed_slots.csv`가 없으면 코드가 고정 슬롯을 생성할 수 있다.
- 기본 random seed는 `config.RANDOM_SEED = 2`이다.
- `agents` 테이블은 `news_depth`를 가진다.
- 기본 에이전트 선택은 `sys_100.db`의 앞에서부터 `max_agents`명을 자르는 방식이다. Depth 균형 샘플링 옵션은 사용하지 않는다.

## 뉴스

- 런타임 뉴스 입력은 `outputs/processed_news.csv`와 `outputs/daily_news_selection.csv`이다.
- `data/samsung_news_raw.pkl`은 전처리 입력이다.
- 일별 기본 선정 목표는 종목 5개, 섹터 3개, 경제 2개이다.
- 시간대별 오전/오후 5:5 배분은 보장하지 않는다.
- Depth 2 검색은 키워드 기반이며 `processed_news.csv`를 검색한다.

## 정보 컷오프

- 기본 실행 모드는 `pre_close_cutoff`이다.
- `am` 턴은 전 거래일 15:30 이후부터 당일 08:59까지의 뉴스를 본다.
- `pm` 턴은 당일 08:59 이후부터 15:30까지의 뉴스를 본다.
- `prior_close`, `same_day` 모드는 비교 실험용으로 유지한다.

## 주문과 체결

- 현재 decision space는 `buy_sell_only`이다.
- 주문은 현금과 보유 수량 제약을 통과해야 제출된다.
- `am` 주문은 당일 시가, `pm` 주문은 당일 종가 기준으로 체결된다.
- 현재 체결 엔진은 별도 호가 경쟁이나 가격 발견을 하지 않는다.

## 커뮤니티

- 커뮤니티 기능은 `config.py`의 `ENABLE_COMMUNITY`, `ENABLE_COMMUNITY_POSTING`, `ENABLE_COMMUNITY_READING`으로 제어한다.
- 커뮤니티 모델은 `OPENROUTER_COMMUNITY_MODEL`을 사용할 수 있다.
- 자기 글은 읽기 후보에서 제외한다.
- 읽기 기능을 끈 경우에도 Best-only 로그를 저장할 수 있다.

## 검증

- 기본 검증은 실제 개인 투자자(`Individuals`) 순거래 방향과 LLM 에이전트 순거래 방향의 일치 여부를 본다.
- 검증은 value와 volume 기준을 모두 산출한다.
- 기본적으로 초기 5거래일은 검증 지표에서 제외한다.

## 실험 메모

- 가짜뉴스 주입 실험 설계는 `fake_news_injection_experiment.md`를 따른다.
- fake 라벨은 에이전트 입력에 노출하면 안 된다.
- fake 라벨은 분석용 로그와 산출물에만 보존한다.
