# Trading Direction Validation

삼성전자 실제 투자자별 순거래 데이터와 TwinMarket Korea 시뮬레이션의 일별 순매수/순매도 방향을 비교한다.

## 입력

| 파일 | 설명 |
| --- | --- |
| `validation/data_trading_value.csv` | 실제 투자자별 순거래대금 |
| `validation/data_trading_volume.csv` | 실제 투자자별 순거래량 |
| `outputs/logs/<run_id>/exchange_fills.csv` | 시뮬레이션 체결 내역 |
| `outputs/logs/<run_id>/daily_exchange_summary.csv` | 일별 가격/거래 요약 |
| `outputs/logs/<run_id>/run_metadata.json` | 실행 메타데이터 |

`--run-dir`를 명시해 검증할 실행 로그를 선택한다. 인자를 생략하면 스크립트는 `outputs/logs/current`을 기본값으로 사용하므로, 해당 경로가 없으면 반드시 명시해야 한다.

## 실행

```bash
python validation/validate_trading_direction.py \
  --run-dir outputs/logs/<run_id>
```

특정 실행 로그 검증:

```bash
python validation/validate_trading_direction.py \
  --run-dir outputs/logs/simulation_YYYYMMDD_HHMMSS
```

초기 거래일 제외 없이 검증:

```bash
python validation/validate_trading_direction.py \
  --run-dir outputs/logs/simulation_YYYYMMDD_HHMMSS \
  --skip-initial-days 0
```

## 산출물

`validation/outputs/<run_id>/`에 생성된다.

| 파일 | 설명 |
| --- | --- |
| `daily_comparison_value.csv` | 거래대금 기준 일별 비교 |
| `daily_comparison_volume.csv` | 거래량 기준 일별 비교 |
| `normalized_comparison_value.csv` | 거래대금 정규화 비교 |
| `normalized_comparison_volume.csv` | 거래량 정규화 비교 |
| `summary_metrics.json` | 방향 일치율, balanced accuracy, 상관계수, baseline 비교 |
| `validation_report.pdf` | PDF 보고서 |

## 기준

- 시뮬레이션 체결에서 `buy`는 양수, `sell`은 음수로 환산한다.
- 일별 LLM 순거래 방향과 실제 `Individuals` 순거래 방향을 1차로 비교한다.
- 실제 데이터와 시뮬레이션 로그가 겹치는 거래일만 사용한다.
- 기본 설정은 초기 5거래일을 제외한다.
- 상관계수와 코사인 유사도는 보조 지표이며, 방향 지표가 1차 해석 기준이다.
