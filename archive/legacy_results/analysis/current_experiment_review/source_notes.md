# C00 파일럿 진단 보고서: 근거·구성 메모

## 보고서 작업 정의

- 질문: 현재 저장된 C00만을 기준으로 무엇을 해석할 수 있으며, 어떤 논문 방향과 재실험이 필요한가?
- 독자: 실험 설계·측정·코드 구조까지 검토할 기술 연구팀.
- 범위: `samsung-baseline-0720`의 `8604f9aec041c9929e327a90cc9025b650e9fab6` 기준. 실제 관측 결과는 C00 하나뿐이다.
- 비교 기준: validation `summary_metrics.json`이 기록한 항상 매수 및 전일 시장수익률 방향 기준선.
- 성공 기준: C00를 과대해석하지 않고, 논문으로 전환 가능한 가설·측정·재실행 순서를 명확히 한다.

## 필수 구조 매핑

| 기술 보고서 요구 항목 | 보고서 섹션 |
| --- | --- |
| Title | C00 파일럿 실험 진단과 논문 재설계 제안 |
| Technical summary | 결론: 방향 일치율은 약한 신호이고, C00는 연속 실험 증거가 아님 |
| Key findings with visual evidence | 기준선 대비·chunk별 변동성·상태 재시작 차트 |
| Scope, data, metric definitions | C00 범위와 58일 검증 창 정의 |
| Methodology | 로그 읽기 경로·집계 절차·페르소나/텍스트 평가 가능 범위 |
| Limitations, uncertainty, robustness checks | 1억 동질 코호트·reset·강제 매매·결측 belief·단일 실행 |
| Recommended next steps | memory/reflection 중심의 재설계와 재실험 순서 |
| Further questions | 사전등록된 인과 비교와 평가자 신뢰도 |

## 차트 맵

| 섹션 | 질문 | 시각화 | 데이터·필드 | 지원하는 주장 | 표현 규칙 |
| --- | --- | --- | --- | --- | --- |
| C00 방향성 | 62.1%가 기준선보다 의미 있게 높은가? | grouped bar | `method`, `direction_match_rate`, `balanced_accuracy` | 항상 매수보다 3.4%p, 전일 시장수익률 방향보다 5.2%p 높지만 단일 58일 창의 약한 기술적 차이 | 두 지표를 색이 아닌 계열로 구분, 0부터 100% 축 |
| chunk 변동성 | 전체 평균이 안정적인가? | bar | `chunk`, `direction_match_rate`, `validation_days`, 실제/예측 buy·sell 일수 | 3~5일 chunk에서 33.3~80.0%로 변동하며, 각 chunk는 reset됨 | 중립 단색, 0부터 100% 축, chunk별 일수 툴팁 |
| 연속성 | 상태가 이어졌는가? | bar | `chunk`, `first_am_initial_portfolio_rows`, `first_am_buy_orders` | 모든 13개 chunk의 첫 AM에 30명 모두 초기 현금·무보유 상태에서 매수 | 두 계열 비교, 0부터 30 축 |

## 의도적으로 제외한 분석

- 기존 C00 외 다섯 조건은 “실제 실험 결과”로 포함하지 않는다. 향후 요인설계 후보일 뿐이다.
- 초기 자본·고자산 취약성 효과는 추정하지 않는다. C00의 30명은 모두 1억 원이다.
- embedding 군집, misinformation reception, community effect는 C00에서 실행/식별되지 않았다. 결과가 아니라 향후 평가 설계로만 다룬다.
- cumulative PnL·장기 행동 지속성은 chunk마다 재시작되므로 C00의 63일 연속 결과로 해석하지 않는다.
- 58개 날짜를 독립 표본으로 놓는 유의확률은 보고하지 않는다. 반복 시드가 없고 시간 의존성이 있다.

## 재현 절차

1. `python3 analysis/current_experiment_review/analyze_current_runs.py`
2. `python3 analysis/current_experiment_review/generate_notebook.py`
3. `python3 analysis/current_experiment_review/execute_notebook.py` (Jupyter kernel 실행 권한이 있는 환경)
4. `python3 analysis/current_experiment_review/generate_report_artifact.py`
5. 플러그인 루트에서 portable report delivery 명령 실행.
