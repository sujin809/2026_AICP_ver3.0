# C00 파일럿 실험 진단과 논문 재설계 제안

기준 버전: `samsung-baseline-0720` @ `8604f9aec041c9929e327a90cc9025b650e9fab6`.

이 문서는 실제 결과가 존재하는 조건을 **C00 하나**로 한정한다. 나머지 다섯 조건은 향후 실행할 2×3 요인설계 후보이지, 관측된 비교 결과가 아니다.

## 결론

C00은 수급 재현을 입증한 본실험이 아니라, 재설계를 정당화하는 단일 파일럿이다.

- 58일 validation에서 방향 일치율은 62.1% (36/58)다.
- 항상 매수 기준선은 58.6%, 전일 시장수익률 방향 기준선은 56.9%다.
- C00의 balanced accuracy는 57.8%: 매수일 recall 82.4%, 매도일 recall 33.3%다.
- 즉, 매수 우세 표본에서 항상 매수보다 2일 더 맞춘 결과다. “개인 수급을 안정적으로 재현했다”는 결론은 불가능하다.

더 큰 문제는 실행 연속성이다. 로그 행 수는 30명 × 63일 × AM/PM = 3,780으로 완전하지만, global turn은 1–126이 아니라 1–10만 존재한다. 13개 chunk의 첫 AM마다 30명 전원이 `1억 원 현금·무보유`로 되돌아가고, 모두 매수 주문을 냈다. C00은 63일 연속 시장이 아니라 **13개의 짧은 episode를 이어붙인 결과**다.

따라서 C00에서 아래 주장은 쓰지 않는다.

| 주장 | 판정 | 이유 |
| --- | --- | --- |
| 단기 agent-turn 로그의 작동 감사 | 제한적으로 가능 | context→belief→decision→fill→portfolio 기록이 있음 |
| 63일 장기 행동, long memory, PnL path | 불가 | 매 chunk에서 portfolio와 turn이 reset됨 |
| 초기 자본 이질성 | 불가 | 활성 30명 모두 1억 원 |
| fake/community 효과 | 미관측 | C00은 fake off·community off |
| persona 효과 | 기술통계만 가능 | depth 2는 4명, 40대는 3명 등 cell이 작음 |
| belief–action 정합성 | 부분 감사만 가능 | belief summary 20.2% blank, buy/sell 강제와 fallback 주문 존재 |

## C00 수치를 어떻게 읽을 것인가

validation의 58일은 시작 5거래일을 제외한 창이다. 실제 개인 수급은 매수일 34일, 매도일 24일이다.

| 방법 | 방향 일치 | 균형 정확도 | 매수일 recall | 매도일 recall |
| --- | ---: | ---: | ---: | ---: |
| C00 agent flow | 62.1% | 57.8% | 82.4% | 33.3% |
| 항상 매수 | 58.6% | 50.0% | 100.0% | 0.0% |
| 전일 시장수익률 방향 | 56.9% | 56.5% | 58.8% | 54.2% |

해석은 다음과 같이 제한한다.

1. C00의 aggregate direction에는 약한 정보가 있을 수 있다.
2. 그러나 전일 수익률 기준선보다 balanced accuracy가 약 1.3%p 높은 한 번의 시간창은 신뢰할 만한 성능 증거가 아니다.
3. 매도 국면을 제대로 재현하지 못한다. 일치율만 최적화하면 매수 편향을 보상하게 된다.
4. 12개 validation chunk의 방향 일치율은 33.3%–80.0%로 넓게 흔들린다. 각 chunk가 3–5일뿐이므로 성능 비교가 아니라 “전체 평균을 과대해석하지 말라”는 진단이다.

추가로 C00 trace에는 invalid LLM output 뒤 minimal fallback 주문이 50개 있고, 이 중 48개가 sell이다. 1주 주문 125개에는 `one_share_reason`이 기록되지 않았다. 주문 방향을 belief 방향과 동치로 놓으면 안 되는 직접적인 이유다.

## C00 로그를 모두 읽는 경로

root 로그가 해석 원천이고, `chunks/`는 restart를 확인하는 중복 복사본이다. 전체 파일 목록은 `outputs/c00_log_inventory.csv`에 있다.

| 순서 | 파일 | 읽는 내용 | 해석 시 주의 |
| --- | --- | --- | --- |
| 1 | `run_metadata.json` | 기간, 30명, fake/community off, chunk 설정 | 실제 실행 스펙 확인 |
| 2 | `agent_turns.jsonl` | news/context, interpretation, belief, decision, quantity, reason | belief와 실행 주문을 분리 |
| 3 | `submitted_orders.csv` | 요청된 buy/sell 주문 | intention이 아니라 제출 주문 |
| 4 | `exchange_fills.csv` | 체결 가격·수량 | 주문과 체결 차이를 확인 |
| 5 | `portfolio_updates.jsonl` | 현금·보유·자산·PnL | chunk 경계 reset 여부 확인 |
| 6 | `daily_exchange_summary.csv` | 날짜×AM/PM aggregate | 일별 C00 flow 구성 |
| 7 | `validation/.../daily_comparison_value.csv` | C00 대 실제 개인 수급의 날짜별 부호 | skip window를 고정 |
| 8 | `validation/.../summary_metrics.json` | 공식 요약과 기준선 | primary metric만 인용 |
| 9 | `chunks/*/agent_turns.jsonl` | 각 chunk 첫 AM | turn=1·initial portfolio 반복 진단 |

해석 연결은 아래 하나다.

`time-aligned input/context → news interpretation·belief → desired intention(현재는 미기록) → submitted order → fill → portfolio update → daily aggregate flow → actual individual flow 비교`

현재 로그에는 desired intention/hold가 빠져 있으므로, `belief → order`를 곧바로 인과적으로 해석할 수 없다.

## persona, embedding, rubric의 위치

persona는 버릴 것이 아니라, 다음 실험에서 사전등록된 moderator로 다뤄야 한다. 현재 C00 활성 cohort는 20대 9명, 30대 18명, 40대 3명이며 전원이 1억 원이다. 전략·news depth별 매수 비율은 기술통계로만 보고한다.

현재의 rubric 설계는 좋은 논문 기여 후보다. 특히 다음을 분해하는 점이 좋다.

- claim reception
- investment stance
- belief/source confidence
- position conviction과 risk restraint
- evidence grounding
- belief–action consistency

단, fake exposure가 없는 C00에서는 reception effect를 평가할 수 없다. C00에서는 R2–R8의 샘플 감사만 하고, 본 분석은 fake와 correction이 있는 clean rerun에서 blind human coding으로 한다. 조건·persona·가설을 가리고 2명 이상이 코딩하며 κ 또는 α를 보고한다. LLM-as-a-judge는 인간 코딩과의 calibration 후 보조 지표로만 쓴다.

embedding은 raw UMAP/cluster 발견을 주결과로 쓰지 않는다. factual↔misinformation claim axis를 미리 만들고, exposure 전·직후·다음 turn의 이동과 correction 후 persistence를 paired trajectory로 측정한다. boilerplate 제거, 다른 embedding model, event/agent/seed bootstrap, subgroup별 belief 결측률이 필수다.

## 기존 6조건은 논문이 될 수 있는가

그렇다. 기존 matrix는 명확한 2×3이다.

| community | 정보 |
| --- | --- |
| off/on | 없음 / bearish fake / bullish fake |

이 설계가 답할 수 있는 질문은 다음이다.

> community exposure가 bullish/bearish financial misinformation의 claim-level belief deviation, action translation, correction persistence를 증폭하는가?

이 방향은 지금 코드와 조건을 가장 많이 살린다. 다만 C00 하나로는 효과를 전혀 검증하지 못했고, legacy C00은 post-7/20 state-continuity/restart-safety 코드 이전 산출물이라 새 실험과 병합하면 안 된다.

## 권장 논문 방향: memory/reflection resilience

더 강한 방향은 다음이다.

> Source-aware short/long memory와 structured reflection이 misinformation-induced belief–action distortion을 줄이는가?

여기서 memory는 수급 일치율을 올리는 단순 feature가 아니라, 출처·시점·정정 상태를 보존하는 통제 가능한 실험 처치다.

### 추천 architecture

1. **Belief / desired stance**: bullish·bearish·neutral, 확률, 근거 claim ID, 출처 신뢰도.
2. **Intention**: trade/hold, 목표 노출도, confidence, 위험 예산.
3. **Feasibility allocator**: 현금·보유·최소단위·가격을 적용하고 constraint flag를 기록.
4. **Execution**: 주문·체결·미체결·fallback을 별도 기록.

short memory에는 직전 1–5 turn의 timestamped market/news/own-position/prediction만 둔다. long memory에는 factual anchor, claim, source, credibility, 이후 확인/반박, 교훈을 저장한다. reflection은 PM 이후 또는 다음 AM 전의 구조화된 `prediction vs observation`, `근거/반증`, `보존/폐기할 memory` 형식이어야 한다.

각 memory에는 observed time, provenance, factual/claim 구분, correction status, expiry를 붙이고, t 시점에 t+1 가격·뉴스·실제 수급을 읽지 못하도록 time gate를 강제한다. 그렇지 않으면 memory가 성능 개선이 아니라 leakage 또는 misinformation persistence가 될 수 있다.

## 재실험 순서

1. **Phase 0 — 실행 신뢰성**: 최신 `scripts/08_run_six_conditions.py` 경로, clean base, `chunk-days=1`, run_complete, global turn 1–126, portfolio continuity, fake delivery, fallback/constraint logging을 gate로 둔다.
2. **Phase 1 — 메인 인과 실험**: community off, memory {baseline, short, short+long+reflection} × information {factual anchor, matched fake, fake+correction}; 최소 3–5 seed와 균형 cohort.
3. **Phase 2 — 외적 타당성**: Phase 1에서 효과가 확인된 architecture만 community on/off × bullish/bearish valence의 기존 2×3에 넣는다.
4. **평가**: rubric deviation·correction persistence를 primary outcome, intention direction의 balanced accuracy를 secondary outcome으로 둔다. direction match 외에 class recall, calibration/Brier, hold rate, turnover, constraint/fallback rate, belief–action consistency, source grounding을 보고한다.

관련 출발점: [InvestorBench](https://aclanthology.org/2025.acl-long.126/), [FinMem](https://ojs.aaai.org/index.php/AAAI-SS/article/view/31290/33450), [Generative Agents](https://arxiv.org/abs/2304.03442), [Reflexion](https://arxiv.org/abs/2303.11366), [financial misinformation study](https://papers.ssrn.com/sol3/Delivery.cfm/5187289.pdf?abstractid=5187289), [FUSE-EVAL](https://aclanthology.org/2025.emnlp-main.1330/).

## 산출물

- `analyze_current_runs.py`: root C00 로그의 무결성·기준선·cohort·chunk 감사.
- `current_experiment_audit.ipynb`: 실행 확인된 재현 노트북.
- `outputs/`: 로그 인벤토리, C00 chunk boundary, validation by chunk, persona 기술통계 CSV.
- `artifact.json`: 기술 보고서의 검증된 canonical manifest.

포터블 HTML 보고서는 artifact packaging까지 통과했지만, 현재 portable report builder의 desktop `100vw` sticky header가 이 Chromium 환경에서 약 15px 가로 overflow를 만들며 최종 QA를 실패시킨다. 이 문서와 실행 노트북은 그 UI 문제와 무관하게 검증된 분석 근거다.
