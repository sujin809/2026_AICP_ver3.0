# 현금·초기자본 진단 재현 메모

## 질문

완료 로그에 초기자본 1억 원과 10억 원 투자자가 함께 존재하는지, 그리고 현재 현금 잔액이 belief와 주문에 반영된 명시적 사례가 있는지 확인한다.

## 통제 자료

- persona 모집단: `outputs/sys_100.db`, `agents`
- 실제 완료 로그의 활성 cohort: `outputs/logs/*/agent_turns.jsonl`
- 행동 사례: 연속성이 유지된 `simulation_20260715_000826_158697_23249/agent_turns.jsonl`
- cohort 선택 코드: `twinmarket_kr/simulation.py`, `select_simulation_agents`

## 재현 명령

```bash
sqlite3 -header -csv outputs/sys_100.db \
  "SELECT ini_cash, COUNT(*) AS n FROM agents GROUP BY ini_cash ORDER BY ini_cash;"

for f in outputs/logs/*/agent_turns.jsonl; do
  jq -r '.agent.agent_id' "$f" | sort -u
done

jq -r 'select(.agent.agent_id=="A019" and ((.date=="2026-02-27" and .context.subturn=="am") or (.date=="2026-03-11" and (.context.subturn=="am" or .context.subturn=="pm")))) | [.date,.context.subturn,.turn,.context.portfolio_summary,.belief.belief_summary,.belief.view_change,.decision.action,.decision.quantity,.decision.reason,.decision.risk_control] | @tsv' \
  outputs/logs/simulation_20260715_000826_158697_23249/agent_turns.jsonl
```

## 결과 요약

- persona 모집단은 1억 원 90명, 10억 원 10명으로 의도대로 구성돼 있다.
- 10억 원 agent id는 A039, A043, A052, A056, A063, A071, A080, A082, A087, A097이다.
- 완료된 6개 로그의 활성 agent는 모두 A001~A030이다. 이들은 전원 초기자본 1억 원이다.
- 원인은 `select_simulation_agents(30)`이 정렬된 모집단의 앞 30명을 자르는 구현이다.
- 행동 사례 분석에는 chunk 초기화 문제가 없는 C11 로그만 사용했다.

## 지표 정의

- `초기자본`: `outputs/sys_100.db.agents.ini_cash`
- `결정 전 현금`: agent-turn의 `context.portfolio_summary`에 기록된 보유 현금
- `표현된 belief`: `belief.belief_summary`와 `belief.view_change`; 잠재적 심리 상태가 아니라 모델이 출력한 텍스트
- `행동`: `decision.action`과 `decision.quantity`

## 해석 경계

- 현금 언급과 주문이 같은 로그에 함께 있다는 사실은 명시적 메커니즘 증거지만, 현금만 무작위 배정한 반사실 실험이 아니므로 인과효과 크기는 아니다.
- C11은 buy/sell-only이므로 현금 부족 상태에서 hold 대신 1주 매수·매도가 발생할 수 있다. A019의 PM 매도는 특히 이 제약의 영향을 직접 밝힌다.
- 행동 사례는 원문 대조가 핵심이어서 표로 제시했다. cohort 누락은 설계 모집단과 실행 cohort의 1억·10억 인원 차이를 한눈에 비교하기 위해 그룹 막대로 제시했다.

## 차트 맵

- 섹션: 10억 persona는 설계에는 있지만 실제 30명 실험에는 없음
- 질문: 초기자본 집단별 인원이 설계 모집단과 실행 cohort에서 어떻게 다른가
- 형태: 그룹 막대(`bar`), 4개 관측치
- 필드: 범위, 초기자본 집단, 인원
- 주장: 10억 집단은 모집단에는 10명이지만 실제 cohort에는 0명
- 색상: 두 자본 집단만 구분하는 2색 상한, 축 라벨로도 구분
- 최종 표면: `analysis/cash_wealth_diagnostic/report.html`

## 보고서 구조 대응

- 기술 요약: 초기자본 cohort 누락과 현금 제약 사례
- 핵심 근거: cohort 감사 표와 A019/A007/A012 사례 표
- 범위·정의: 데이터 단위와 필드 정의
- 방법: SQLite/JQ 기반 직접 감사
- 한계·강건성: 비인과성, buy/sell-only, chunk 오염 제외
- 다음 단계: 30명 cohort를 27명 1억+3명 10억으로 층화할지 결정
- 추가 질문: 기존 C00을 유지할지, cohort 수정 후 다시 돌릴지
