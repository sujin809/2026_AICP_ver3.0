# News Scraper

뉴스 원천 데이터를 만들기 위한 보조 수집 도구 모음이다. 이 폴더는 현재
simulation runtime의 입력 경로가 아니다. 새 run은
`preparation/rn_ab_sealed_v1/news.json`의 봉인된 실제뉴스 bundle만 읽으며,
5/3/2 뉴스 노출 정책과 accepted shortage를 다시 보충하거나 재선발하지 않는다.

## 주요 파일

| 파일 | 역할 |
| --- | --- |
| `scrape_mk.py` | 매일경제 검색 결과와 기사 본문 수집 |
| `scrape_mk_stock_playwright.py` | Playwright 기반 종목 뉴스 수집 |
| `scrape_mk_sector_playwright.py` | Playwright 기반 섹터 뉴스 수집 |
| `scrape_hankyung.py` | 한국경제 뉴스 수집 |
| `collect_all.py` | 여러 수집기를 묶어 실행 |
| `summarize.py` | 수집 뉴스 요약 |
| `_test_resummary.py` | 재요약 테스트 보조 |

## 설치

```bash
cd News_Scraper
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Playwright 기반 스크립트를 쓰는 경우 브라우저 설치가 추가로 필요할 수 있다.

```bash
.venv/bin/python -m playwright install chromium
```

## 매일경제 검색 수집 예시

```bash
.venv/bin/python scrape_mk.py \
  --word 삼성전자 \
  --start-date 2026-03-11 \
  --end-date 2026-03-12 \
  --sort accuracy \
  --search-field title \
  --json mk_articles.json
```

매경 검색은 같은 날짜를 `startDate`와 `endDate`에 동시에 넣으면 0건이 나올 수 있다. 특정일 뉴스는 검색 URL에서 `startDate=전날`, `endDate=해당일` 형태로 조회하는 쪽이 안정적이다.

## 현재 파이프라인과의 경계

이 도구로 수집한 원천은 별도 검토·provenance 절차를 거쳐 새로운 versioned
candidate에만 사용할 수 있다. 현재의 `02_prepare_news.py`는 legacy pkl/CSV
split sampler가 아니라 명시한 sealed profile의 `news.json`을 검증하는 단계다.
공식 bundle 또는 이미 봉인된 뉴스의 기사·slot·hash를 이 폴더의 수집 결과로
바꾸지 않는다. 현재 실행·재봉인·검증 명령은 루트의
`RUNBOOK_AND_PREFLIGHT.md`를 따른다.
