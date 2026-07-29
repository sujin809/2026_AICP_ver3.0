# Legacy results archive

이 디렉터리는 현재 통합 실행 경로에서 분리한 과거 분석·검증 산출물의
정적 보관소다. 여기에 있는 파일은 재실행·재봉인·현재 결과 해석의 입력이
아니다. 과거 실행 코드는 Git 이력으로 추적하고, 현재 실행 계약은 루트의
`RUNBOOK_AND_PREFLIGHT.md`와 `EXPERIMENT_DESIGN.md`를 따른다.

| 이전 위치 | 보관 위치 | 성격 |
| --- | --- | --- |
| `analysis/current_experiment_review/` | `analysis/current_experiment_review/` | 과거 current-run 분석 노트와 산출물 |
| `analysis/paper_0721_c00_review/` | `analysis/paper_0721_c00_review/` | 과거 C00 검토 노트와 산출물 |
| `validation/outputs/` | `validation/outputs/` | 이전 방향 검증 결과 |
| `validation/tmp/` | `validation/tmp/` | 이전 PDF/검증 임시 산출물 |

현재 canonical run은 자신의 서명된 run directory만 원본으로 사용한다.
검증 JSON·방향 검증 CSV/PDF·보고서 PDF는 canonical run 밖의 명시적
`derived/<condition>/` 경로에 생성해야 한다. 이 archive를 런타임 입력,
최신 결과, 또는 재현용 base DB로 사용하지 않는다.
