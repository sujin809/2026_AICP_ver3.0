# 100-agent 실제 뉴스 Community ON/OFF 실험과 FUSE-inspired Short-term→Long-term Belief 설계

> 보존 구분: 과거 상세 설계 provenance. 현재 실행·정책 정본이 아니다.

> 상태: **설계·감사 문서 / 현재 manifest 예시 45거래일×AM·PM=90 decision turns, 최초 3거래일 burn-in·42일 주 분석 / 본 실험 실행 전 NO-GO**  
> 기준 브랜치: `samsung-baseline-0720`  
> 기준 commit: `8604f9aec041c9929e327a90cc9025b650e9fab6`  
> 기준 확인일: 2026-07-22  
> 대상 저장소: [ujlee1661/2026_AICP_ver2.0](https://github.com/ujlee1661/2026_AICP_ver2.0/tree/samsung-baseline-0720)  
> 원 실험 명세: [ORIGINAL_EXPERIMENT_DESIGN.md](ORIGINAL_EXPERIMENT_DESIGN.md)  
> 핵심 참고 논문: [Liu et al., *The Stepwise Deception: Simulating the Evolution from True News to Fake News with LLM Agents*, EMNLP 2025](https://aclanthology.org/2025.emnlp-main.1330/); 금융·성찰 관련 비교연구는 §5.5와 부록 C 참조
> 구현 상태 주석 (2026-07-23): 이 문서 본문은 설계·인수인계 계약이다. RN four-stage core, prompt bundle, schema v9, local adversarial tests는 구현·검증되었으나, 실제 유료 100-agent run은 승인 artifact와 live canary가 없어 계속 NO-GO다. 최신 구현/검증 상태는 [적대적 검증 보고서](RED_TEAM_VALIDATION_REPORT.md)를 기준으로 한다.
> 현재 정본: [README](../../README.md) ·
> [실행 전 체크리스트](../../RUNBOOK_AND_PREFLIGHT.md) ·
> [통합 아키텍처](../../ARCHITECTURE.md)

---

## 0. 이 문서의 결론

이번 첫 실험은 다음 두 조건만 실행한다. **STB/LTB를 도입하되 기존 `dim_1~dim_6`의 이름·의미·전망 horizon과 belief→market-analysis→decision 골격은 바꾸지 않는다.** 새 memory용 의미 차원이나 별도 투자 taxonomy를 만들지 않는다. `belief_summary`와 `view_change` field는 로그 호환을 위해 저장한다. `belief_summary`는 항상 사람용 로그이고 어떤 agent-visible 입력에도 다시 넣지 않는다. `view_change`는 `LTB_(t-1)→LTB_t`의 six-dimension 차이와 integration evidence에서 서버가 결정론적으로 만드는 사람용 trace이며, **post-writing 단계에서만** `LTB_t`와 함께 private input으로 허용한다. STB·LTB updater·market analysis·decision·community interpretation에는 다시 넣지 않는다. 우리 구현에서는 agent-visible memory를 세 층으로 늘리지 않고 **Short-term Belief(STB)와 Long-term Belief(LTB) 두 상태**로 둔다.

- **STB_t**: frozen persona를 해석 렌즈로 삼아, 현재 AM/PM에 새로 허용된 뉴스·실제 노출된 community를 해석한 이번 턴의 `dim_1~dim_6` 단기 belief다. 이전 STB·이전 LTB·시장/portfolio execution state·과거 거래 성찰을 prior로 carry하지 않는다.
- **LTB_t**: `LTB_(t-1) + STB_t + decision/fill episode_t + 이번 turn에 새로 관찰 가능한 과거 가격 성찰`을 **재귀적으로 다시 해석**해 거래 뒤에 새로 작성하는 누적 `dim_1~dim_6` 장기 belief다. `decision/fill episode_t`는 이번 turn의 LTB/STB 통합 판단, 실제 committed `fill_t`, pre/post portfolio를 뜻하며 단순 append log가 아니다. FUSE의 `previous LTM + current STM → new LTM`에 대응한다. 같은 frozen persona는 통합의 일관성 제약으로 사용하지만 새로운 evidence로 세거나 수정하지 않는다.
- 이번 turn 거래의 memory input은 **`LTB_(t-1)`와 `STB_t`의 분리된 두 block**이다. `LTB_t`는 다음 decision event부터 쓰므로, 같은 현재 뉴스·community를 LTB와 STB에 중복해 넣지 않는다. current market facts·portfolio·order/constraint는 기존처럼 직접 입력으로 유지하되 raw news/community는 STB를 거쳐서만 거래 단계에 전달한다.
- 기존 `belief_history.dim_1~dim_6`는 LTB canonical state로 유지하고, 새 `short_term_belief_history.dim_1~dim_6`만 추가한다. 기존 downstream 수정 범위를 줄인다.
- 이번 turn의 actual `fill_t`·post-portfolio는 raw ledger에 즉시 저장하고, **같은 turn post-fill LTB updater**에 `decision/fill episode_t`로 정확히 한 번 전달한다. 단, 이는 “무엇을 실제로 체결했는가”라는 거래 사실과 판단 맥락을 재귀적으로 해석하는 단계이지 가격 성과를 미리 판정하는 append가 아니다. 가격 관찰은 `next-turn`, `H1(다음 거래일 동일 subturn)`, `H5(5거래일 뒤 동일 subturn)` 순으로 성숙하며, STB가 아니라 그 시점 LTB `dim_6`의 장기 거래 성찰 근거가 된다. `fill_t`는 생성 전인 STB·analysis·decision에 역유입되면 실패지만, post-fill `LTB_t`와 PM post-writing에는 허용된다.
- 핵심 횟수 불변식은 고정 숫자 90이 아니라 **agent별 committed logical STB update 수 = committed logical LTB update 수 = manifest가 정의한 decision-turn 수 `U`**다. 현재 45거래일에 AM·PM 두 decision subturn을 쓰므로 해석값이 우연히 `U=90`인 것뿐이며, 기간을 늘리거나 줄이면 validator가 manifest에서 `U`를 다시 계산한다. transport/schema retry의 physical attempt는 이 logical count에 중복 산입하지 않는다. **각 LTB update는 여섯 차원을 새로 작성해야 하며 이전 text의 byte-identical copy를 허용하지 않는다.** 다만 새 근거가 기존 장기 판단을 뒤집지 않으면, 기존의 material assertion을 보존한 채 이번 STB와 거래 성찰에 비추어 왜 지속되는지 재서술한다.
- 현재 승인 study는 exact 100명으로 실행하되, core scheduler·DB key·validator·report는 `resolved_manifest.agent_count=N`을 사용한다. 즉 N을 바꾼 후속 study에서 모든 expected count와 cohort validation이 함께 재계산되어야 한다.
- 시뮬레이션과 데이터 완전성 검사는 현재 승인 예시인 45거래일 전체를 대상으로 하되, 방향 정합성의 **사전 고정 주 분석은 최초 3거래일을 burn-in으로 제외한 42거래일**이다. 45일 전체 결과는 초기 강제 매수 영향을 보여 주는 필수 보조표다. 기간을 2026-06-01 등으로 확장할 때에는 날짜 목록만 calendar-event registry에서 바꾸고, `D`(거래일), `U`(decision event), burn-in 이후 평가일 수를 resolver가 재계산한다. 45·90·42는 코드 상수가 아니다.

### 0.0 2026-07-23 확정 운영 정책

- **Reasoning은 실제로 OFF여야 한다.** 모든 physical HTTP attempt에 최종 `reasoning: {"effort": "none"}` request object를 중앙 client가 강제한다. `exclude=true` 또는 `include_reasoning=false`만으로는 통과하지 않는다. model/provider를 pin하고 fallback을 끈 뒤, live canary와 run audit에서 returned model/provider, empty reasoning fields, `reasoning_tokens=0`을 모두 확인하지 못하면 run을 즉시 중단한다.
- **거래 성찰 horizon은 확정한다.** 각 earlier `fill_t`는 `next-turn`(다음 decision event), `H1`(다음 거래일 동일 subturn), `H5`(5거래일 뒤 동일 subturn)의 가격 관찰을 하나의 `fill episode`로 순차 기록한다. 관찰값은 도래한 event 뒤의 LTB `dim_6` 성찰에만 쓰며, STB·현재 decision·`dim_1~dim_5`에는 넣지 않는다. 마지막 구간의 미도래 관찰은 `right_censored`다.
- **수수료는 0원으로 강제한다.** 이번 baseline은 `commission_rate=0.0`, `sell_tax_rate=0.0`이며 매수·매도 모두 `fee_amount=0`이어야 한다. config·exchange·portfolio·PnL·`paper_fill_ledger`·canonical CSV export·manifest가 이 값을 정확히 공유하지 않으면 fail-closed한다. 따라서 `dim_6` 가격 성찰은 수수료 조정 없이 공시 체결가격 기준 gross timing markout으로 일관되게 계산한다. 개인수급 방향의 1차 비교값도 일별 gross signed fill value다. 수수료를 도입하는 후속 study는 별도 amendment와 양 arm 재실행이 필요하다.
- **실제 뉴스 slot의 목표는 10개다.** 기존 보유 기사 중 cutoff-time 원문 version·출처·시각 provenance와 semantic leakage review를 통과한 실제 기사 pool에서 event별 ordered 10개를 먼저 재선정해 frozen manifest에 봉인한다. safe pool으로도 목표를 채울 수 없으면 unsafe 기사·합성 기사·동일 기사 중복으로 빈 slot을 채우지 않고, 가능한 safe·unique 실제 기사만 사용해 **run은 계속한다**. 이 경우 `target_real_count=10`, `selected_safe_count`, `serialized_count`, `delivered_real_count`, `actual_real_count(=delivered_real_count)`, `missing_real_count(=target_real_count-selected_safe_count)`, candidate/review 사유, ordered ID/hash를 `news_shortage_exception_manifest.jsonl`에 남기며 양 arm은 그 동일 bundle을 쓴다. `serialized_count`/`delivered_real_count` 또는 payload hash가 selected map과 다른 것은 shortage가 아니라 runtime delivery failure이므로 retry/실패 처리한다. 후속 fake-news 실험은 같은 frozen real bundle을 보존한 채 schedule상 별도 1개 fake slot만 추가한다.
- **shortage가 agent prompt를 왜곡해서는 안 된다.** prompt에는 “10개 뉴스”라는 고정 수를 쓰지 않고 실제 frozen payload 목록만 직렬화한다. shortage quality flag·후보 수·review 사유는 evaluator/report용 metadata이며 agent에게 study-quality label로 주지 않는다. prompt snapshot hash와 actual delivered article count는 `article_delivery_trace`에서 검증한다.
- **Best 5는 상위 최대 5개다.** `K=5`는 매일 강제 생산량이 아니라 ranking 상한이다. 게시글이 0개면 exposure 0, 1~4개면 실제 글 전부, 5개 이상이면 상위 5개만 다음 AM에 노출한다. 글 부족을 채우기 위한 강제 posting·합성 post·중복 post는 금지하고, `available_post_count`, `best_count`, actual exposure count를 기록한다.
- **기존 로그와 출력 convention은 보존·확장한다.** 기존 per-run `agent_turns`, `submitted_orders`, `exchange_fills`, `portfolio_updates`, `community_posts`, `community_interactions`, `community_best_posts`, `community_logs` CSV/JSONL은 삭제·대체하지 않는다. 새 memory/outcome/API/community-exposure trace는 sidecar로 추가하고, 한 `RUN_RECORD.md`와 manifest가 기존·신규 artifact의 경로와 hash를 색인한다. 과거 run PDF는 archival-only이며 새 report는 전역 DB가 아니라 해당 run의 frozen snapshots만 읽는다.

| 조건 ID | 실제 뉴스 | 가짜뉴스 | Community | 계층 메모리 | Reasoning mode |
|---|---:|---:|---:|---:|---:|
| `RN_COMM_OFF` | 동일 | 없음 | OFF | per-turn STB → `previous LTB + STB` Decision-Making → `fill_t` 체결 → recursive LTB | OFF |
| `RN_COMM_ON` | 동일 | 없음 | ON | per-turn STB → `previous LTB + STB` Decision-Making → `fill_t` 체결 → recursive LTB | OFF |

두 조건 사이에서 바뀌는 것은 **community availability 하나뿐**이다. 100명, persona, 초기 LTB와 portfolio, 실제 가격, 실제 뉴스, 정보 cutoff, 모델, prompt, seed, per-turn STB/LTB 갱신 주기, reasoning-off 정책, 동시성, 재시도 정책은 모두 동일해야 한다.

이 설계의 1차 목적은 **주가 자체를 예측하는 것**이 아니라, 실제 뉴스 환경에서 agent 집단이 보이는 belief·매매 반응이 삼성전자 실제 개인투자자의 일별 순매수/순매도 반응을 얼마나 잘 따라가는지 검증하는 것이다. 이 baseline이 단순 기준보다 충분히 타당해야 구조를 freeze하고 후속 가짜뉴스의 bullish/bearish 반응 차이를 해석한다.

이 설계가 식별하는 community contrast는 다음이다.

> 실제 뉴스만 존재하는 외생 삼성전자 시장에서, per-turn STB와 previous LTB를 분리해 거래하고 거래 뒤 LTB를 재귀 갱신하는 100명 LLM 투자자에게 공통 종목토론방의 사용 가능성이 belief와 주문에 만드는 총효과. 이 총효과에는 community 정보가 다음-AM STB를 거쳐 거래 뒤 LTB에 통합되어 이후 판단에 지속되는 경로도 포함된다.

이 실험만으로는 다음을 주장할 수 없다.

- STB/LTB 계층 자체의 인과효과
- 이번 base run 자체에서의 가짜뉴스 효과 또는 bullish/bearish 차이
- community와 가짜뉴스의 상호작용
- 실제 인간 투자자에 대한 인과효과
- agent 주문으로 형성된 시장가격 또는 시장 전체의 재현
- 실제 소셜 네트워크 확산, homophily, repost cascade

### 0.1 Short-term/Long-term Belief가 거래에 참여하는 정확한 시점

FUSE에서 STM은 “오늘 들은 의견의 짧은 summary”, LTM은 “이전 LTM과 오늘 STM을 합친 recursive summary”이며, 공개 코드는 새 LTM으로 opinion을 갱신한다. 우리에게는 재귀 갱신 자체는 유지하되, 단기 정보가 이번 거래에도 독립적으로 작용하게 **`LTB_(t-1) + STB_t → decision_t → committed fill_t → LTB_t`**로 옮긴다. `decision_t`는 BUY/SELL·수량 제안이고, `fill_t`는 제약 검증 뒤 실제로 체결된 거래 사실이다.

- **Short-term Belief(STB_t)**: 현재 AM/PM 정보만으로 만든 six-dimension turn belief. 기억의 수명이 짧다는 뜻이지 `dim_1`의 전망 horizon을 바꾼다는 뜻이 아니다.
- **Long-term Belief(LTB_t)**: previous LTB·current STB·**이번 turn의 committed fill_t 거래 사실**·새로 확인된 과거 거래 성찰을 통합한 six-dimension cumulative belief. 이번 turn 뒤 생성되어 다음 decision event의 long-term block이 된다. current fill에는 아직 성과 판단을 붙이지 않고, actual side/quantity/price/fee=0/pre-post cash·holdings와 `outcome_pending` 상태만 쓴다.
- **증빙 원장(evidence ledger)**: `paper_fill_ledger`의 실제 `fill_t`, `trade_outcomes`의 사후 가격 관찰, news/community exposure의 immutable trace를 남기는 서버 기록이다. 두 belief가 무엇을 봤는지 검증할 뿐 agent의 세 번째 memory가 아니다.

각 global turn의 핵심 순서는 다음과 같다.

1. 이전 turn fill의 next-turn mark를 계산하고, due H1/H5 outcome을 mature해 **이번 LTB update용 earlier price-outcome packet**을 준비한다.
2. frozen persona를 해석 렌즈로 두고, 현재 뉴스·허용 community만으로 `STB_t.dim_1~dim_6`을 정확히 한 번 만든다. previous STB/LTB·시장/portfolio execution state·과거 거래 성찰·summary/change는 넣지 않는다.
3. market analysis와 decision에는 `LTB_(t-1).dim_1~dim_6`와 `STB_t.dim_1~dim_6`을 역할이 구분된 두 block으로 전달하고, current market·portfolio·order/constraint와 함께 BUY/SELL·수량을 만든다.
4. actual fill·post-trade portfolio를 deterministic phase staging에 계산하고, actual side/quantity/price/fee=0/pre-post cash·holdings를 가진 **committed `fill_t` transaction-fact packet**을 만든다. 이 packet은 같은 turn의 STB·analysis·decision에는 역유입되지 않지만, post-fill `LTB_t`에는 정확히 한 번 들어간다.
5. 같은 frozen persona를 read-only 일관성 제약으로 두고, `LTB_(t-1).dim_1~dim_6 + STB_t.dim_1~dim_6 + current committed fill_t transaction-fact packet + eligible earlier trade-outcome reflection packet`으로 `LTB_t`를 정확히 한 번 새로 작성한다. persona는 evidence가 아니며 동일 root evidence는 한 번만 센다.
6. 100명의 모든 STB·의사결정·체결·LTB·portfolio가 검증되면 한 transaction으로 commit한다. `LTB_t`는 다음 decision event부터 visible하다.
7. post-PM community는 PM scientific phase commit 뒤 생성되므로 다음 거래일 AM STB에 처음 들어간다.

뉴스·community 해석은 필요한 기존 차원에 반영될 수 있다. 반면 **actual fill·가격 markout 기반 거래 성찰만** LTB `dim_6`으로 제한한다.

| 채널 | STB에 들어가는 시점 | LTB 통합 시점 | 영향 가능 차원 |
|---|---|---|---|
| 이번 turn 거래 사실 `fill_t` | **STB에는 never**; decision/fill 뒤 current transaction-fact packet | **같은 turn post-fill `LTB_t`** | **LTB `dim_6`만**; actual side·qty·price·pre/post portfolio·`outcome_pending` |
| next-turn mark·성숙 markout | **STB에는 never**; first-visible/maturity event의 LTB outcome-reflection packet | 그 event의 **거래 뒤 LTB update** | **LTB `dim_6`만**; fill episode·horizon·관찰 event ID 필수 |
| 실제 뉴스 | 실제 노출된 현재 event의 STB | 같은 event 거래 뒤 `LTB_t` | STB `dim_1`, `dim_2`, `dim_3`, `dim_5` 중심 |
| Community | 전날 PM 노출분을 다음 AM STB | 같은 event 거래 뒤 `LTB_t` | STB `dim_4`, `dim_5`, 필요 시 다른 차원; source dependency 기록 |

본 연구의 LTB는 “최근 5일만 기억”하는 저장소가 아니다. FUSE의 직전 LTM 재귀갱신을 옮겨, AM/PM 판단 주기에 맞춰 **매 턴** 새 버전을 작성한다. 5거래일은 거래 outcome의 평가 horizon일 뿐 LTB 보존기간이 아니다.

### 0.2 현재 즉시 실행하면 안 되는 이유

현재 commit은 원 실험의 복구 안전성을 상당 부분 갖췄지만, 이번 실험을 바로 실행할 수 있는 상태는 아니다.

- 6조건 launcher가 agent 수 30을 여러 곳에 하드코딩한다.
- 6조건 launcher는 조건을 생략하면 가짜뉴스 4개 arm까지 전부 실행하고, checkpoint runner 자체도 `fake_news_mode=on`을 기본값으로 둔다. 따라서 현재 진입점은 실뉴스-only에 대해 fail-closed가 아니다.
- 현재 baseline CSV에는 알려진 bearish/bullish 주입 row가 없지만, fake-off preflight는 승인된 실뉴스 bundle의 exact identity가 아니라 `is_fake`/`synthetic_id` 표식만 검사한다. 표식을 지운 합성 row나 내용이 바뀐 CSV가 통과할 수 있다.
- 실제 `sys_100.db`의 뉴스 depth와 persona prompt가 60/100명에서 서로 모순된다.
- 코드·검증 보고서가 의도하는 depth 분포와 실제 DB 분포도 다르다.
- 현재 memory는 직전 belief summary와 최근 주문 정도만 제공하며, DB에 저장한 여섯 차원 전체를 다음 turn에 복원하지도 않는다.
- analysis·decision·posting이 `belief_summary`/`view_change`를 실제 causal input으로 사용해 “사람용” 계약과 충돌한다.
- 오전 timestamp 기사 summary에 같은 날 마감값·최종 고가·최종 투자자 수급이 들어 있는 실제 반례가 있어 현재 뉴스 feed는 as-of 안전하지 않다.
- 현재 API 요청에는 reasoning-off 설정이 없다. **재시도까지 포함한 모든 물리적 요청에서 off 증거가 없으면 즉시 중단한다.**
- 현재 turn-0 belief는 여섯 차원 중 다섯 차원과 summary가 100명 모두 동일하다.
- 100명 중 한 명의 LLM 실패가 AM/PM phase 전체를 rollback하여 성공 응답까지 재호출하게 된다.
- `COMMISSION_RATE=0.0005` 설정과 실제 fee `0.0` 체결 경로가 불일치한다. 이번 baseline에서는 config·manifest도 `0.0`으로 봉인해 이 불일치를 제거해야 한다.
- memory lineage, 시점 누출, reasoning token 0, 100명 완전성에 대한 자동 검증이 없다.
- 현재 direction validator는 기본 5일 제외, malformed→0, actual∩simulation 교집합 평가, non-buy→sell 처리이며 fill status·가격·manifest-derived `N×|Q_d|` 행/일 완전성을 검증하지 않는다. 사용자 확정 primary인 first-3 burn-in 42일과도 다르다.
- AM exchange/audit row가 당일 미래 종가를 미리 보유한다. 현재 `pre_close_cutoff` prompt의 직접 누출은 확인되지 않았지만 accidental reuse를 막도록 AM payload와 evaluator-only EOD fact를 분리해야 한다.
- 현재 workspace의 기본 `outputs/experiment_base_sim.db`는 0 byte라 즉시 실행하면 clean-base validation에서 실패한다. 본 실행 시 자동 재생성하지 말고 별도 승인·봉인 단계에서 새 base를 만들어야 한다.

따라서 이 문서의 P0 gate를 모두 통과하기 전에는 유료 본 실행을 시작하지 않는다.

---

## 1. 연구 목적과 estimand

### 1.1 주 연구질문

원 설계의 RQ1과 RQ2를 유지한다.

- **RQ1 baseline 반응 정합성(핵심)**: 실제 가격과 실제 뉴스만 주어진 agent 집단의 삼성전자 일별 순거래 방향이 실제 삼성전자 개인투자자의 순매수/순매도 반응 방향과 얼마나 일치하는가? 매수일뿐 아니라 매도일도 따라가는지와 단순 price-only 기준 대비 증분 정합성을 함께 본다.
- **RQ1-N opening nowcast 진단**: 당일 실제 개인 수급 label이 결합되기 전, **당일 시가를 관찰한 직후** AM에 기록한 100명의 제약 전 `directional_stance`가 같은 날 삼성전자 개인투자자 방향과 얼마나 맞는가? 현재 최소수정 코드 기준 이는 08:59 사전예측이 아니라 09:00 opening nowcast이며, 기억이 만든 방향을 강제 체결과 분리해 보는 보조 검증이다. 진짜 08:59 예측을 택하면 당일 시가와 시가 파생값을 모두 제거하는 별도 forecast-origin amendment가 필요하다.
- **RQ2 community 총효과**: 같은 실제 정보환경에서 community availability가 belief, 주문 방향·강도, 집단 수렴, 포트폴리오 결과를 어떻게 바꾸는가?

다음은 이번 2-arm 안에서만 제한적으로 다룬다.

- **RQ4 일부**: community 효과가 사전에 정의한 하락장과 상승장에서 달라지는가?
- **RQ5 일부**: community 효과가 뉴스 depth, 초기자본, 전략 등 사전 특성에 따라 달라지는가?

RQ3과 가짜뉴스 관련 RQ4는 이번 **base run**의 범위에서는 제외한다. 다만 이 base run이 아래 validation gate를 통과하면, 동일 cohort·기간·가격·뉴스 cutoff·STB/LTB 정책을 고정한 채 `fake=none/bearish/bullish` 조건을 후속 단계에 추가한다.

후속 가짜뉴스 단계의 목적은 새 구조가 정상 작동하는지 다시 확인하는 것이 아니라, 검증된 base 위에서 bullish/bearish injection의 방향·지속·community 상호작용을 추정하는 것이다. 따라서 base 결과를 보고 prompt, memory 규칙, depth, 기간을 다시 바꾼 뒤 가짜뉴스 효과를 시험해서는 안 된다.

현재 target CSV는 날짜별 최종 `Individuals` 순거래대금/순거래량만 있고 시간대별 개인 수급 label은 없다. 따라서 AM 신호를 “오전 개인 수급”과 비교할 수 없으며, 그날 장 마감 뒤 확정된 **일별 전체 방향**과 비교한다. AM+PM 체결은 하루 전체 행동 재현, AM stance는 더 이른 시점의 opening nowcast 진단이다. 또한 news-OFF 대조군이 없으므로 이번 2-arm만으로 “뉴스가 반응을 일으켰다”는 인과효과를 식별하지 않고, **실제 뉴스 환경에서의 반응 정합성**이라고 표현한다.

### 1.2 주요 estimand

주 estimand는 같은 agent·date·subturn에 대한 paired contrast다.

```text
Community total effect
= outcome(RN_COMM_ON) - outcome(RN_COMM_OFF)
```

이 차이에는 다음 경로가 함께 포함된다.

```text
Community availability
→ 게시판 노출·읽기·반응
→ 다음 AM STB의 community 해석
→ current STB와 previous LTB를 분리한 거래 판단
→ 거래 뒤 다음 event용 LTB의 재귀 갱신
→ market analysis·decision·order
```

이번 실험에서는 memory가 양 arm에 동일하므로 memory 효과를 따로 식별하지 않는다. Community OFF에서는 community-derived evidence가 명시적 empty이고, Community ON에서는 실제 노출된 community event만 같은 STB/LTB policy로 처리한다.

### 1.3 주장 범위

- 가격은 모든 조건에 동일한 외생 실제 경로다.
- agent order는 가격을 움직이지 않는다.
- 모든 정상 주문은 공시가격에 전량 체결된다.
- 결과는 이 modeled population, 이 모델, 이 prompt, 이 seed/run에 대한 simulation 결과다.
- 실제 개인 순매수는 evaluation target이며 agent input, STB, LTB, prompt에 절대 넣지 않는다.
- RQ1의 원 primary는 기존 validation의 **AM+PM actual-fill 일별 합산 방식**을 유지한 net trading direction이다. 다만 분석 창은 현재 코드 기본 5일 제외가 아니라 사용자 확정 first-3 burn-in 42일로 바꾼다. RQ1-N은 시가 관찰 직후 기록한 `directional_stance`를 사용하며 강제 매매·현금 제약 때문에 생긴 BUY/SELL과 분리한다.
- RQ1의 input coverage는 45거래일 전체여야 하지만, 사용자 확정에 따라 primary metric은 최초 3거래일을 동일하게 제외한 42거래일에서 계산한다. skip 0/1/5일은 결과를 바꾸어 고르는 선택지가 아니라 사전 명명된 민감도 분석이다.
- 현재 evaluation target은 코스피 전체가 아니라 `005930` 삼성전자 투자자별 실제 순거래 CSV의 `Individuals` 열이다. 코스피 전체 개인 수급으로 바꾸려면 별도 design amendment와 target dataset이 필요하다.
- 한 seed만 실행하면 confirmatory population inference가 아니라 한 쌍의 simulated worlds에 대한 사례 결과다.

### 1.4 이 45일은 development replay다

2026-02-27~2026-05-04의 **legacy C00** 결과와 실제 개인 방향을 이미 검토한 뒤 STB/LTB 구조를 설계했다. 따라서 같은 45일로 새 구조를 실행한 결과는 memory architecture와 community pipeline의 **development/replay validation**이며, 완전히 보지 않은 기간에 대한 confirmatory out-of-sample prediction으로 주장할 수 없다.

기존 30-agent **legacy C00** 진단에서는 45일 raw direction match 60.0%로 always-buy 62.2%보다 낮았고, 같은 decision-time 가격만 사용한 AM opening-gap contrarian의 balanced accuracy는 82.8%, PM current-return contrarian는 92.9%였다. PM baseline은 contemporaneous reconstruction이고 AM baseline은 시가를 이미 본 opening-nowcast comparator다. 이 수치는 새 100-agent 결과의 보장값이 아니라, 복잡한 memory pipeline이 단순하고 강한 price-only relation을 오히려 희석할 수 있다는 사전 경고다.

새로 고정한 첫 3거래일 burn-in을 같은 legacy data에 적용하면 42일 AM opening-gap contrarian는 accuracy 78.6%·BA 81.1%, PM current-return contrarian는 accuracy 90.5%·BA 92.0%다. PM 수치는 종가까지 본 reconstruction이므로 예측 gate가 아니라 같은 정보시점의 강한 비교선이다. 새 결과는 반드시 이 42일 정의로 다시 계산하고, 위 수치를 코드 상수나 목표값으로 하드코딩하지 않는다.

- base replay에서는 always-buy/sell, previous-day, AM opening-gap contrarian, PM current-return contrarian를 같은 evaluator로 반드시 보고한다.
- “STB/LTB로 정합성이 좋아졌다”는 주장은 이번 2-arm에서 식별되지 않으며, 후속 same-cohort memory ablation과 강한 baseline 대비로만 판단한다.
- 엄밀한 confirmatory 예측 주장은 prompt·memory·분석코드를 freeze한 뒤 아직 설계에 사용하지 않은 별도 기간에서 검증한다.
- 같은 45일의 fake-news/community paired extension은 simulated-world treatment effect를 탐색할 수 있지만, out-of-sample 인간 행동 예측 검증을 대신하지 않는다.

---

## 2. 확정할 2-arm 설계

### 2.1 조건 정의

#### `RN_COMM_OFF`

- 실제 뉴스만 사용한다.
- fake mode를 코드와 manifest에서 강제로 `off`로 둔다.
- community posting, reading, reaction, Best 5를 모두 비활성화한다.
- community phase는 생략하지 않고 **no-op checkpoint**를 기록한다.
- 내부 audit state에는 `community_available=false`와 empty snapshot을 남기되, agent-visible prompt에는 arm label이나 `not_available` 문구를 노출하지 않고 기존처럼 `null`/absence로 처리한다.

#### `RN_COMM_ON`

- `RN_COMM_OFF`와 byte-identical한 실제 뉴스 파일을 사용한다.
- fake mode를 강제로 `off`로 둔다.
- Depth 0은 글을 쓰거나 일반 게시물을 선택 열람·반응하지 않지만, 전날 non-empty Best **최대 5개**는 연구기간 내 다음 AM에 수동적으로 본다.
- Depth 1·2는 PM 체결 후 게시 여부를 결정하고, 게시물이 있으면 한도 내에서 선택 열람·반응할 수 있으며, 전날 Best 5도 다음 AM에 본다.
- non-empty Best **최대 5개**의 제목과 본문 원문은 100명 모두에게, agent가 추가로 실제 읽은 일반 글은 해당 Depth 1·2 agent에게만 다음 거래일 AM부터 노출 가능하다. empty/final-day Best schedule의 next-AM Best broadcast/exposure는 0이다.
- 개인화 추천, 친구·팔로우 graph는 추가하지 않는다.

2026-07-22 사용자 확인에 따라 이 participation rule을 원 설계의 확정 의도로 사용한다. 즉 100명 전원은 두 arm의 뉴스·belief·AM/PM 거래·portfolio·memory 경로와 `RN_COMM_ON`의 Best 5 audience에 포함된다. `RN_COMM_ON`에서 능동적으로 글을 쓰고 일반 게시물을 선택·반응하는 agent만 Depth 1·2의 70명이다. Depth 0의 30명은 **Best5-only passive audience**다.

### 2.2 조건 사이 불변 항목

다음 값은 run signature에서 hash 또는 exact value가 같지 않으면 paired experiment를 시작하거나 재개할 수 없다.

- ordered calendar-event registry hash와 resolver가 산출한 date list·burn-in/evaluation date list
- 100개 agent ID의 순서와 persona DB hash
- fixed-slot gender/age/age-group/initial-cash projection, rich ID list와 각 agent의 depth·persona prompt hash
- clean base DB hash와 turn-0 `LTB_0` hash
- 실제 가격과 실제 뉴스 파일 hash
- fake row 수 0, synthetic ID 수 0
- model과 returned provider
- reasoning-off request object
- prompt tree와 code tree hash
- dependency lock와 Python/SQLite/OpenAI SDK version
- seed derivation policy
- STB/LTB schema·per-turn cadence·fusion·LTB-update policy version
- worker/global concurrency, RPM/TPM limiter, retry policy
- cutoff와 trading constraints

### 2.3 manifest-driven parameterization 계약

이번 본 실험의 **100명은 확정된 연구 조건**이다. 이를 가변 기본값으로 낮추지 않는다. 다만 `100`을 launcher·runner·validator·분석기에 각각 복사하지 않고, typed `StudySpec`의 `required_agent_count=100`과 immutable ordered cohort registry 한 곳에서만 선언한다. resolver는 registry의 exact agent ID 수가 100인지 검사한 뒤 `N`을 계산한다. 하나라도 빠지거나 다른 ID로 교체되면 호출 전 실패한다.

구현은 `StudySpec`(사람이 승인하는 입력)과 `resolved_study_manifest.json`(resolver만 만드는 파생 결과)을 분리한다. launcher·scheduler·DB key·validator·예산·완료 기준·분석기는 모두 resolved manifest의 같은 ordered key set을 소비하며 각자 45·90·9,000 같은 값을 다시 산술하지 않는다.

| manifest 입력 변수 | 파생/검증 대상 |
|---|---|
| ordered condition IDs와 treatment map | arm 수, child command, 허용된 arm 간 차이 |
| `required_agent_count=100`과 ordered cohort registry(100 exact IDs, agent별 depth·persona·초기자산) | registry cardinality assertion, `N`, cohort hash, depth/권한 집계, expected agent-turn key set |
| ordered calendar/event registry | `D`, 시작·종료일, cutoff windows, exact decision-event map |
| 각 날짜의 ordered decision-event spec | `U`, global turn map, STB/LTB/analysis/decision/fill expected key set |
| exact burn-in date IDs 또는 단일 burn-in rule 중 하나 | excluded/evaluation date set과 hash; 두 표현을 동시에 입력하지 않음 |
| community phase policy | 날짜별 post-decision phase와 next-turn exposure schedule |
| news condition/bundle | 동일 real-news root 또는 승인된 후속 treatment bundle |
| memory policy | 각 decision turn의 `STB → Decision-Making → decision/fill → post-fill LTB → next-event visibility` stage DAG |
| model/reasoning/concurrency/retry | physical execution policy와 비용 projection |

현재 study에서 사람이 고정하는 것과 resolver가 계산하는 것을 다음처럼 구분한다.

| 구분 | 현재 study의 canonical 입력/파생값 |
|---|---|
| authored 연구 조건 | required agent 수 100, exact cohort/depth/자산 map, RN_COMM_OFF/RN_COMM_ON treatment map, 승인된 거래일·decision-event registry, 최초 3개 exact burn-in date ID, real-news-only bundle, AM/PM cutoff·시가/종가 execution policy, BUY/SELL-only, community 권한, Best `K=5`, read/search/lookback policy, memory/retry/reasoning policy |
| resolved cardinality | `N`, `A`, `D`, `U`, evaluation-date 수, depth별/active/Best-audience 수 |
| resolved exact key set | decision events, news slots, `(condition,agent,event)` STB/LTB/analysis/decision/fill keys, community schedule/exposure keys, outcome maturity/right-censor keys |
| resolved 예상량 | DB row, logical API call, committed STB/LTB, fill, checkpoint, 비용·token·저장공간 예상량 |

`StudySpec`에는 `trading_days`, `start_date/end_date`, `turns`, `primary_evaluation_days`, `active_community_agents`, `best5_audience`, STB/LTB update 수, expected row/call 수처럼 canonical registry에서 계산 가능한 중복 field를 넣지 않는다. 그런 derived key가 authored spec에 들어오면 schema 단계에서 거부한다. 현재 해석값 45일·90 decision events·42 평가일·active 70명·Best audience 100명·agent별 STB/LTB 90회는 resolved manifest에 감사용으로 기록할 수 있지만 실행의 입력으로 사용하지 않는다.

모든 arm은 같은 scheduler와 같은 memory state machine을 사용한다. 현재 2-arm에서 treatment diff allowlist는 `community_mode` 하나뿐이다. `RN_COMM_OFF`도 community stage를 삭제하지 않고 no-op checkpoint를 만들며, community evidence가 비어도 AM/PM decision turn이면 STB와 LTB를 각각 한 번 logical update한다. 반대로 manifest에 존재하지 않는 날짜/subturn에는 두 update를 모두 만들지 않는다. 어떤 조건 분기에서도 STB만 만들거나 LTB만 건너뛸 수 없다.

확장·축소 원칙은 다음과 같다.

- 기간을 바꾸면 ordered date manifest와 hash만 새 study version으로 승인하고 `D/U`·row count·비용·완료 기준을 다시 계산한다.
- AM/PM 구조를 바꾸면 ordered decision-subturn manifest를 새 version으로 승인하며, scheduler는 hardcoded `2d-1/2d` 대신 ordered turn map을 사용한다.
- 현재 본 실험은 exact 100명에서만 유효하다. agent 수·agent ID·depth map을 바꾸는 것은 별도 study amendment이고 같은 결과의 축소 실행으로 취급하지 않는다. 다만 core code는 새 registry를 해석할 수 있어야 하며 숫자 교체를 위해 수정하지 않는다.
- 후속 bearish/bullish arm도 같은 `STB → Decision-Making → actual fill → post-fill LTB → next-event visibility` 횟수·순서를 유지하고 news treatment만 허용된 차이로 추가한다.
- manifest resolver는 실행 전에 모든 derived 값과 expected composite-key set을 `resolved_study_manifest.json`에 기록한다. paper entrypoint는 이 파일을 생성·봉인한 뒤 실행기와 validator에 같은 hash를 전달한다. 코드에 별도 literal 45·90·9,000 또는 독자적인 date intersection/count 산식이 있으면 실행을 거부한다.

### 2.4 현재 기간과 turn 해석값

사용자가 지정한 시작일 2026-02-27부터 2026-05-04까지를 적용한다. 현재 `StockData` 거래일 캘린더에서는 정확히 **45거래일**이다. “약 40일”은 근사 표현으로만 남기지 않고, manifest에는 45일 전체 날짜 목록을 고정한다. 시뮬레이션은 45일 전부 실행하며, 최초 3거래일 `2026-02-27, 2026-03-03, 2026-03-04`를 burn-in으로 제외한 **42거래일**이 주 분석 표본이다.

| 항목 | 값 |
|---|---|
| 종목 | 삼성전자 `005930` |
| 기간 | 2026-02-27~2026-05-04 |
| 거래일 | 45 |
| 주 분석 거래일 | 최초 3거래일 제외 42 |
| burn-in | 2026-02-27, 2026-03-03, 2026-03-04 |
| subturn | AM·PM |
| global turn | manifest-derived `1..U`; 현재 date/subturn map에서 `U=90` |
| 하락장 | 2026-02-27~2026-03-31 |
| 상승장 | 2026-04-01~2026-05-04 |
| AM 뉴스 cutoff | 직전 거래일 15:30 초과~당일 08:59 이하 |
| PM 뉴스 cutoff | 당일 08:59 초과~15:30 이하 |

이 기간에는 2026-04-01 이전 22거래일과 이후 23거래일이 들어간다. 원 문서의 63거래일·126턴에서 줄인 것이므로 “원 기간 유지”라고 쓰지 않고, 새 preregistration version과 date-list hash를 부여한다. 시작일 또는 종료일이 바뀌면 정확한 거래일 목록과 regime 균형을 다시 산출해 manifest를 갱신해야 한다.

---

## 3. 원 실험 설계 부합성 매트릭스

| 원 설계 요소 | 이번 설계 | 판정 | 조치/해석 |
|---|---|---|---|
| 실제 삼성전자 가격을 외생 경로로 사용 | 유지 | 부합 | 가격 생성 연구라고 주장하지 않음 |
| 63거래일·AM/PM 126 turn | 현재 manifest 해석값 45거래일·90 turn | 사전등록 amendment | core code는 ordered date/subturn manifest에서 동적 계산; 현재 study에는 2026-02-27~2026-05-04 date list와 hash 고정 |
| 초기구간 제외 분석 | 최초 3거래일 burn-in, 42일 primary | 사용자 확정 분석 amendment | 실행 완전성은 45일; excluded-date hash를 고정하고 0/1/5일은 sensitivity |
| AM/PM 정보 cutoff | 유지 | 부합 | memory에도 같은 as-of rule 강제 |
| 실제 뉴스 10개 | **각 decision event 목표 10개의 provenance-safe 실제 기사** | 사용자 확정 amendment | 기존 보유 기사 pool에서 leakage-safe 원문 version으로 우선 재선정·봉인; 부족 시 safe·unique actual count와 shortage 사유를 기록한 동일 arm bundle로 계속 실행, 중복·합성·unsafe backfill 금지 |
| Community OFF/ON | 두 arm만 선택 | 정당한 부분집합 | RQ1·RQ2 중심; full factorial 완료로 표기 금지 |
| fake 없음/bearish/bullish | base에서는 fake 없음만 실행 | 단계적 범위 축소 | base validation 통과 뒤 같은 frozen design에서 bearish/bullish 확장; base만으로 RQ3 주장 금지 |
| 동일 cohort | 모든 arm 동일 100명 | 원 설계 수정 | 기존 문서의 30명 실행과 다른 estimand; 새 base에서 두 arm 모두 재실행 |
| persona 원천 100명 | 전원 사용 | 모집단 의도 부합 | 90명 1억·10명 10억 포함 |
| Depth 0/1/2의 입력·community 권한 | Depth 0 Best5-only, Depth 1·2 active | 조건부 부합 | 현재 DB·prompt 오염과 source spec/code의 Depth 0 Best 5 누락을 복구 |
| 단일 공개 게시판·개인화 없음 | 유지 | 부합 | FUSE의 social graph를 복사하지 않음 |
| PM 뒤 community, 다음 AM 반영 | 유지·강화 | 부합 | `visible_from_turn` DB constraint와 test 추가 |
| belief 여섯 차원 | STB/LTB 모두 같은 의미로 유지 | 부합 | 기존 `belief_history` text를 canonical LTB로 두고 current-only STB table만 추가 |
| buy/sell only, hold 없음 | 유지 | 부합하나 해석 주의 | trade count를 자연 참여율로 해석하지 않음 |
| 1회 매수 현금 50%, 공매도 없음 | 유지 | 부합 | portfolio constraint hash 고정 |
| Qwen model과 seed 2 | 유지 | 부합 | provider까지 pin·검증 |
| worker 30, global API 16 | 100명을 worker pool 30으로 처리 | 부합 | agent_count와 concurrency를 구분 |
| 논리적 연속 실행과 checkpoint | 유지·강화 | 부합 | validated-response journal 추가 |
| 계층 belief | frozen-persona-conditioned current-information 6D STB + cumulative 6D LTB | 사전등록 수정 | 두 arm에서 승인된 각 decision event마다 STB 1회·LTB 1회. agent·arm당 두 count는 모두 manifest-derived `U`; 현재 45일×AM/PM manifest를 해석할 때만 각각 90이다. persona는 read-only이며 evidence가 아님 |
| reasoning off | 새로 추가 | 사전등록 수정 | 모든 LLM stage의 고정 invariant |

### 3.1 논문/보고서 표기

권장 표기:

> We ran a preregistered 45-trading-day clean-news two-arm slice of the original 2×3 information-environment design, comparing community off versus on. We excluded the prespecified first three trading days as initialization burn-in, leaving 42 primary evaluation days. The cohort was amended from the earlier 30-agent operational subset to all 100 frozen personas, and both arms used the same evidence-linked per-subturn short-term-belief-to-long-term-belief update and explicitly disabled provider reasoning mode.

금지 표기:

- “6조건 실험을 완료했다.”
- “계층 메모리의 효과를 검증했다.”
- “가짜뉴스에 강건하다.”
- “100명의 독립 표본을 확보했다.”

---

## 4. 기준 코드 감사 결과와 P0 문제

### 4.1 기준 commit

- 로컬 branch: `samsung-baseline-0720`
- 로컬 HEAD: `8604f9aec041c9929e327a90cc9025b650e9fab6`
- 원격 GitHub의 같은 branch commit도 동일하게 확인했다.
- 이 문서는 기존 runtime DB·WAL·SHM과 분석 산출물을 수정하지 않는다.

### 4.2 launcher의 30명 하드코딩

현재 `scripts/08_run_six_conditions.py`는 다음을 30으로 고정한다.

- study invariant의 `max_agents`와 agent list: `scripts/08_run_six_conditions.py:126-136`
- child command의 `--max-agents 30`: `scripts/08_run_six_conditions.py:233-239`
- launch manifest의 `agent_count: 30`: `scripts/08_run_six_conditions.py:410-414`

checkpoint runner의 기본값도 30이지만 명시적으로 100을 받을 수 있다: `scripts/05_run_simulation_checkpointed.py:649-664`.

내부 `select_simulation_agents(100)`은 100명을 반환할 수 있으므로, 문제는 core selector보다 launcher·manifest·검증 경로다.

**P0 조치**

- 6조건 launcher를 억지로 재사용하지 말고 2조건 전용 launcher를 만든다.
- 이번 study의 `required_agent_count=100`을 typed `StudySpec` 한 곳에서 읽고 exact cohort registry의 cardinality와 비교한다. 전역 `AGENT_COUNT` 상수를 또 만들지 않는다.
- command, manifest, invariant, acceptance count가 모두 이 값에서 파생되게 한다.
- child process가 보고한 agent IDs와 parent manifest의 100 IDs를 실행 전후 비교한다.

### 4.3 persona depth 오염

현재 코드·보고서가 주장하는 값:

- `config.py:49-50`: Depth 2 비율 30%, Depth 0 인원 15명
- `twinmarket_kr/persona/select.py:116-125`: 위 규칙에 따른 할당
- 이 코드에서 파생되는 분포: Depth 0/1/2 = 15/55/30
- `outputs/persona_validation_report.json:20-25`도 15/55/30과 pass를 주장

실제 `outputs/sys_100.db` 감사 결과와 이번 실험의 확정 설정:

- Depth 0/1/2 = **30/55/15**
- initial cash = 1억 90명, 10억 10명
- DB `news_depth`와 `persona_prompt`의 자기설명이 **60/100명 불일치**
- 첫 30명에서도 16/30명 불일치

불일치는 분포만 비교해서 추정한 것이 아니다. `agents.news_depth`와 `persona_prompt` 안의 three depth-permission 문장을 agent ID별로 parse하여 교차표를 만들었다.

| DB의 실제 `news_depth` | prompt가 D0이라고 말함 | prompt가 D1이라고 말함 | prompt가 D2라고 말함 |
|---:|---:|---:|---:|
| D0 (30명) | 11 | 18 | 1 |
| D1 (55명) | 16 | 27 | 12 |
| D2 (15명) | 3 | 10 | 2 |

따라서 prompt 쪽의 전체 30/55/15 분포가 우연히 같아도 **같은 사람에게 같은 depth가 배정된 것이 아니다**. 일치 행은 40명뿐이다. mismatch가 언제/누가 만들었는지는 현재 artifact만으로 단정하지 않지만, 현재 재생성 코드가 같은 문제를 다시 만들 수 있는 근거는 명확하다.

2026-07-22 사용자 확인에 따라 **Depth 2가 더 적은 현재 DB 분포 30/55/15가 올바른 실험 설정**이다. 이번 study에서는 DB의 agent별 `news_depth`를 canonical assignment로 freeze한다. 실제 runtime은 [`collect_context.py:41-51`](../../twinmarket_kr/core/collect_context.py)의 `agent["news_depth"]`로 뉴스 접근을 정하고, Depth 2 search 및 community gate도 같은 DB field를 쓴다. 반면 persona prompt는 news interpretation/analysis/decision LLM input에 별도로 주입된다. 따라서 `config.py`의 30% Depth 2 상수와 `persona_validation_report.json`은 새 paper run의 authority가 될 수 없는 stale artifact다.

이 상태에서는 런타임이 DB depth에 따라 뉴스·community 권한을 주면서 모델은 persona prompt에서 다른 접근 성향을 읽는다. Community treatment eligibility 자체가 오염되므로 본 실행을 금지한다.

**권장 복구안 — 새 depth를 배정하지 않는다**

1. 현재 100명의 `agent_id`, `source_user_id`, demographic, strategy, behavioral traits, initial cash와 **agent별 DB `news_depth`를 그대로 freeze**한다.
2. pre-repair DB hash와 exact 100개 `(agent_id, news_depth)`를 `persona_depth_manifest.json`에 기록하고 hash-pin한다.
3. **`scripts/01_build_persona.py`를 실행하지 않는다.** 이 script는 `match_agents()`로 source cohort와 depth를 새로 선택하고 `init_agents_db()`로 agents table을 DROP 후 재생성한다. `config.py` 숫자를 30/55/15로 고쳐도 agent별 map을 보존한다는 보장은 없다.
4. 현재 DB의 각 structured row를 `generate_persona_prompt(row)`에 넣어, `persona_prompt`만 canonical `render_persona_v1` serialization으로 100개 전부 재생성한 **run-local repaired persona snapshot**을 만든다. `news_depth`와 다른 structured field는 한 행도 수정하지 않는다. 이 방식은 실제 renderer가 DB row의 depth를 사용함을 독립 fixture로 확인한다.
5. `scripts/02_init_memory.py`, initial-belief generation, paper launcher, runtime, validator, report가 모두 전역 `outputs/sys_100.db`가 아닌 이 sealed `--persona-snapshot`을 받게 한다. 그래야 turn-0 `LTB_0`부터 repaired prompt를 쓴다.
6. 모든 agent에 대해 DB depth, parsed prompt depth/permission, manifest depth가 100/100 일치하는지 검사하고, two arm의 `(agent_id, depth, prompt_sha256)` map이 byte-identical인지 확인한다.
7. `persona_repair_manifest.json`에는 repair 전/후 DB hash, pre/post mismatch 수(60→0), depth 변경 agent 수(반드시 0), non-prompt structured field 변경 agent 수(반드시 0), old/new prompt hash, renderer/template hash를 남긴다.
8. 새 validator는 depth count뿐 아니라 exact `agent_id → depth → permission text` equality를 검사한다. 현 `distribution_pass`처럼 distribution만 읽는 보고서는 paper gate로 쓰지 않는다.

**영구 불변식:** paper run에서는 `persona_depth_manifest`의 `agent_id → news_depth`가 유일한 assignment source다. persona prompt는 그 immutable structured row의 deterministic projection일 뿐 독립 편집 가능한 source가 아니다. depth를 바꾸려면 새 assignment manifest/version을 만들고 prompt·turn-0 belief·두 arm을 모두 새 run으로 생성한다. 기존 run의 DB/prompt를 부분 patch하는 것은 금지한다.

확정 분포에서는 active contributor/reader가 Depth 1·2 합계 **70명**이고, Best 5 audience는 **100명 전원**이다. 본 문서의 이후 예상 수치와 완료 기준도 30/55/15를 사용한다.

### 4.4 Depth 0 Best 5 passive exposure 누락

사용자가 확인한 원 설계 의도는 “Depth 0은 글을 쓰거나 일반 글을 선택·반응하지 않지만 Best 게시물은 본다”이다. 그러나 현재 source spec과 구현은 이 의도를 완전히 표현하지 못한다.

- `ORIGINAL_EXPERIMENT_DESIGN.md:207-211,292-298`은 Depth 0을 community 게시·열람 미참여로 적어 Best 5 passive exposure를 누락한다.
- `twinmarket_kr/simulation.py:705-709,807-808`이 posting·일반 reading을 Depth 1·2로 제한하는 것은 맞다.
- `twinmarket_kr/simulation.py:922-930`은 community log를 active agent에게만 저장하므로 Depth 0용 Best 5 log가 없다.
- `twinmarket_kr/core/collect_context.py:78-85`는 `news_depth >= 1`일 때만 전날 community log를 불러온다.
- `twinmarket_kr/core/daily_cycle.py:133-140`도 `depth >= 1`일 때만 community thinking을 수행한다.
- `twinmarket_kr/community/agent.py:118-136`의 `mark_best_posts()`는 `post_id`, `title`, `post_type`, `score`만 반환한다. 이 함수 안에서 본문을 조회하지 않는다.
- `twinmarket_kr/community/thinking.py:57-63`의 `_format_best_posts()`도 위 네 필드 중 제목·유형·점수만 prompt에 넣는다. 반환된 `post_id`로 `get_post_content()`를 다시 호출하는 경로가 없다.
- 본문 조회는 Best 5 경로가 아니라, Depth 1·2가 같은 날 candidate metadata를 보고 일반 게시물을 선택한 경우에만 `simulation.py:850-860`의 `get_post_content(post_id)`를 통해 일어난다. 따라서 어떤 agent가 Best 글의 본문을 알고 있다면 그 글을 전날 별도로 선택해 읽은 우연한 경우일 뿐, Best 5를 보고 원문을 찾아간 것이 아니다.

즉 현재 구현은 **Depth 0은 Best 제목조차 못 보고, Depth 1·2도 Best 5 경로에서는 제목만 본다.** 이는 사용자가 확인한 원 설계와 다르므로 P0 결함이다.

**P0 조치**

1. 원 설계 문서의 Depth 0 표현을 “posting/selective reading/reaction 없음, Best 5 passive viewing 있음”으로 바로잡는다.
2. posting과 일반 게시물 reading/reaction 대상은 기존처럼 Depth 1·2의 70명으로 유지한다.
3. Best 5 확정 시 각 `post_id`의 **제목과 원문 본문 전체**를 서버가 deterministic query로 결합한다. agent가 제목을 보고 다시 클릭할지 LLM으로 결정하게 하지 않는다.
4. **Best가 1건 이상이고 연구기간 내 다음 거래일 AM이 존재할 때만** 결합된 payload를 100명 모두에게 broadcast 예약하고, 다음 AM community-interpretation request에 실제 삽입된 때 Best exposure로 기록한다. STB request에는 실제 노출·출처가 연결된 interpretation과 lineage만 넣는다. 빈 board/Best면 `empty` schedule envelope만 남기고 Best broadcast call·exposure는 0이다. 마지막 거래일은 연구기간 안의 **next-AM Best broadcast/exposure**가 0이며, non-empty Best가 있을 때만 schedule을 `right_censored`로 남긴다. 같은 PM에 D1/D2가 선택해 읽은 본문 exposure까지 0이라는 뜻은 아니다.
5. Depth 0 log는 non-empty Best일 때 `posts_read=[]`, `best_posts=[{post_id,title,post_type,score,content,...}]`, `participation_mode="best_only"`여야 한다. empty Best일 때 `best_posts=[]`이고 exposure는 0이다.
6. 연구기간 내 다음 AM에 실제 non-empty payload가 있을 때만 Depth 0도 Best 원문을 대상으로 §10.3의 community interpretation을 수행한다.
7. Depth 1·2도 Best 5에 대해서는 동일한 원문을 받으며, 전날의 별도 선택 열람 기록은 추가 private context로만 둔다.
8. Depth 0이 쓴 글·선택 열람·reaction 행은 0인지 별도 integrity check한다.
9. 45번째 거래일 PM의 Best 5는 연구기간 안에 다음 AM이 없으므로 `scheduled_but_right_censored`로 남기고 실제 exposure로 세지 않는다.

### 4.5 turn-0 belief의 제한된 다양성

현재 `outputs/sim.db`의 turn-0 belief 100행을 감사하면 다음과 같다.

- `dim_1`, `dim_3`~`dim_6`, `belief_summary`: 각 1종
- `dim_2`: strategy에 따른 2종

이는 `twinmarket_kr/llm/belief.py:52-67`의 offline neutral template와 일치한다. Persona별 초기 belief 이질성 분석은 약해지지만, 두 arm이 동일한 prior에서 시작하는 community contrast 자체를 무효화하지는 않는다.

**권장 결정**

- 원 설계 변경을 최소화하기 위해 현재 deterministic neutral turn-0 prior를 유지한다.
- 다양성 수나 unique-string threshold를 P0 gate로 두지 않는다. Threshold를 맞추려고 prompt를 반복 수정하면 인위적인 이질성을 만들 수 있다.
- P0에서는 100개 agent ID, 여섯 field schema, 동일 base hash, 두 arm byte identity만 검증한다.
- Persona-conditioned initial prior가 꼭 필요하면 별도 study amendment로 정의하고, reasoning-off·stable seed로 100개를 한 번 생성한 뒤 두 arm에 복사한다.
- 기존 30명 결과나 현재 dirty `sim.db`를 새 runtime으로 직접 사용하지 않는다.

### 4.6 현재 memory의 한계

`twinmarket_kr/core/collect_context.py:35-40`은 다음만 가져온다.

- 직전 belief
- 직전 portfolio
- 최근 주문 5건
- 마지막 행동 이유
- 최근 system message

`twinmarket_kr/agents/memory_agent.py:49-60`은 직전 belief의 여섯 차원이 아니라 `belief_summary`만 조회한다. Community는 원 설계대로 전날 로그를 AM에만 제공하지만, 장기 뉴스·장기 community·증거 lineage·거래 성찰은 없다.

따라서 현재 상태를 `no-memory`라고 부르면 안 된다. 정확한 이름은 **shallow-state baseline**이다.

### 4.7 reasoning off 미구현

`twinmarket_kr/llm/client.py:79-99`는 model, messages, temperature, seed와 provider options만 보내며 reasoning parameter를 전혀 보내지 않는다. 기존 로그를 reasoning-off 실행이라고 소급 주장할 수 없다.

### 4.8 기타 실행 차단/주의점

- `MemoryAgent.save_belief()`가 `INSERT OR REPLACE`를 사용해 충돌을 조용히 덮어쓴다.
- belief validation은 필드가 비어 있지 않은지만 확인하며 `BELIEF_LIMITS`를 실제로 강제하지 않는다.
- `analysis.py`의 일부 JSON validator는 unknown extra key를 허용하고 retry 횟수를 causal dict에 넣는다.
- `run_integrity.py`의 phase digest는 canonical six dimensions보다 summary/change 위주라 실제 belief 변화를 놓칠 수 있다.
- `processed_news.csv`의 오전 기사에 같은 날 “마감”·최종 고가·최종 수급이 포함된 반례가 있고, `prepare_news()`는 URL·last-modified·scraped-at·raw version을 보존하지 않는다.
- 뉴스 summary 전처리의 별도 Claude CLI 경로는 reasoning-off·시점 provenance가 감사되지 않는다.
- `config.COMMISSION_RATE=0.0005`가 정의돼 있지만 exchange transaction과 portfolio fill fee는 `0.0`이다. 이번 baseline에서는 config도 `0.0`으로 고정해 fee-free policy와 일치시켜야 한다.
- stage별 `max_tokens`가 없어 출력 폭주 상한이 없다.
- provider fallback 기본값이 `true`다.
- transport retry에는 jitter가 없어 100명에서 thundering herd가 생길 수 있다.
- 파일 기반 global semaphore는 동시 요청 수만 제한하며 RPM/TPM과 arm 간 공정성을 보장하지 않는다.
- `.env.example`은 아직 `openai/gpt-4o`를 가리키고 reasoning·community·concurrency·retry 설정이 빠져 있다.
- 현재 확인한 기본 `python3` 환경에는 `openai` package가 설치돼 있지 않고 project `.env`도 없다. 본 실행용으로 승인된 별도 runtime과 secret 주입 경로를 준비해야 한다.
- `prompts/update_belief.txt`는 AM과 PM 모두 호출되는데 “매일 아침”이라고만 설명한다.
- 현재 repository는 약 4.2GB, `outputs`는 약 3.6GB, `outputs/logs`는 약 2.6GB다.
- runtime DB/WAL/SHM과 분석 디렉터리에 기존 변경이 있으므로 새 실험 root를 별도로 사용해야 한다.

기존 `tests/test_experiment_safety.py`의 17개 테스트는 이 commit에서 통과했다. 다만 이 테스트군에는 100명 전체 실행, 계층 메모리 시점 누출, reasoning-off live canary, provider pin, validated-response replay, kill/resume 동일성 검사가 아직 없다. 기존 17개 통과는 이번 설계의 Go 판정이 아니다.

### 4.9 demographic·persona·초기자산 무결성 감사

`data/fixed_slots.csv`, `twinmarket_kr/persona/slots.py`, `outputs/sys_100.db`를 100개 ID별로 대조했다. **agent ID·성별·나이·연령대·초기자산은 100/100 exact match**하고 null·중복 source user는 0이다. 따라서 기존 persona 모집단을 다시 뽑거나 자산 구성을 바꾸지 않는다.

| 축 | 현재 `sys_100.db` 분포 | 판정 |
|---|---|---|
| 성별 | 여성 57, 남성 43 | fixed slots와 일치 |
| 연령대 | 20대 9, 30대 18, 40대 23, 50대 26, 60대 17, 70대 6, 80대 이상 1 | fixed slots와 일치; 21~82세 |
| 전략 | technical 58, value 42 | behavioral source match 결과 |
| user type | ordinary 91, small influencer 8, big influencer 1 | behavioral source match 결과 |
| 초기자산 | 1억 90, 10억 10 | 원 설계 90%/10%와 일치 |
| news depth | Depth 0=30, Depth 1=55, Depth 2=15 | **확정 의도와 일치; Depth 2가 가장 적은 설정이 맞음** |

DB field와 `persona_prompt`를 직접 비교하면 gender·age·location·strategy·user type·initial cash와 네 behavioral category의 **의미상 field mismatch는 모두 0**이다. 의미상 예외는 이미 §4.3에서 확인한 **news depth 문장 60/100 mismatch**다. 예를 들어 DB Depth 0인 A039 prompt가 “10개 요약본 모두”, DB Depth 1인 A100 prompt가 “최근 7일 추가 검색”이라고 말한다. 이는 demographic 오류가 아니라 P0 persona-depth prompt 오염이며, DB depth를 바꾸지 않고 frozen DB 값으로 prompt만 재생성한다.

별도의 형식 결함도 하나 있다. 현재 100개 중 **A001만 prompt 안에 줄바꿈이 0개이고 문장 두 개가 붙어 있으며**, 나머지 99개와 직렬화 형식이 다르다. A001의 DB 대비 persona 의미값은 맞지만, 이 작은 형식 차이가 agent 1명만 받는 비의도 treatment가 될 수 있다. 어차피 depth 문장을 복구해야 하므로 100개 prompt **전체 serialization**을 frozen structured field에서 `render_persona_v1`로 다시 생성한다. renderer는 Unicode NFC, LF-only, exactly one trailing LF, 고정 section 순서·section separator·scalar escaping을 규정한다. gate는 자연어 “붙은 문장” 탐지가 아니라 `(a)` canonical parse/round-trip 100/100, `(b)` DB↔parsed prompt의 모든 의미 field mismatch 0, `(c)` depth 외 structured value의 before/after equality 100/100, `(d)` ordered `(agent_id, prompt_sha256)` map hash 고정, `(e)` RN_COMM_OFF/RN_COMM_ON의 agent별 prompt byte identity다. 단순 raw prompt hash는 depth·형식 복구 때문에 바뀌므로, 기존 의미 field 변화 0과 새 ordered map hash를 repair manifest에 함께 기록한다.

자산은 인원 비율만 보면 90/10이지만 총액 기준으로는 다르다.

```text
1억 group: 90명 × 1억 = 90억, 전체 초기자본의 47.37%
10억 group: 10명 × 10억 = 100억, 전체 초기자본의 52.63%
총 초기자본: 190억
```

즉 10억 agent 10명이 raw signed-value primary를 지배할 가능성은 **버그가 아니라 원 자산 설계가 만드는 가중치**다. 10억 agent의 depth는 0/1/2가 3/6/1로 전체 30/55/15와 크게 어긋나지 않지만, 연령은 모두 45~76세이고 20·30대 10억 agent가 0명이다. 따라서 초기자산 효과와 연령 효과를 이 100명 관찰자료만으로 분리하거나 실제 인간 demographic 효과로 일반화할 수 없다.

원 설계를 유지하면서 다음을 필수 robustness로 둔다.

- primary raw signed value는 그대로 유지하되 `1억 group only (=10억 10명 제외/rich-excluded; 같은 90명 alias)`, `10억 group only`, `10억 agent leave-one-out 10회` 방향을 함께 보고. alias 두 결과는 byte-identical이어야 함
- agent별 signed notional을 각자의 **고정 initial capital**로 나눈 뒤 100명을 equal-weight 평균한 방향을 필수 sensitivity로 보고
- raw primary와 initial-capital-normalized 방향이 다른 날짜, 10억 group이 전체 부호를 뒤집은 날짜, group별 AM/PM 기여도를 공개
- RN_COMM_OFF/RN_COMM_ON에는 같은 100명·같은 자산을 byte-identical하게 사용하므로 arm 간 community contrast의 cohort balance는 유지. 다만 demographic·자산 subgroup 결과는 descriptive mechanism으로만 표기
- 외부 실제 삼성전자 투자자 demographic/capital weighting으로 calibration하지 않은 이상 “한국 개인투자자 모집단 대표 표본”이라고 쓰지 않음

`wealth_sensitivity_v1`은 사전 고정한다. 각 날짜마다 raw 100명 합, 1억 90명 합, 10억 10명 합, agent별 AM+PM signed notional을 fixed initial capital로 나눈 100명 equal-weight 평균, 그리고 10억 agent를 한 명씩 뺀 열 개 합의 방향을 계산한다. `core_p3b_pass`는 §22 P3-B의 coverage/leakage·buy/sell recall·BA·constant-baseline accuracy·MCC라는 **wealth 조건을 제외한 순수 성능 기준**의 conjunction이다. 어느 한 leave-one-rich-out에서 `(a)` `core_p3b_pass`가 full-100 결과와 달라지거나 `(b)` 42일 RQ2 mean community-effect 부호가 바뀌거나 0으로/0에서 이동하면 `wealth_fragile=true`다. 최종 `robust_p3b_pass = core_p3b_pass AND NOT wealth_fragile`로 계산해 자기참조를 금지한다. 이는 DB/실행 무결성 실패는 아니지만 “100-agent population-wide robust baseline/community effect” 주장과 후속 fake-news extension gate는 통과시키지 않는다. 날짜별 sign-flip 수와 rich contribution share는 결과와 무관하게 공개한다.

또한 persona의 behavioral source matching은 `rng.choices()` 가중추출이고 최소 `match_score` gate가 없다. 현재 score 범위는 3~16이다. 이는 identity row 오류는 아니지만, 낮은 score persona까지 포함된 constructed population이라는 한계다. 원 설계 그대로 freeze하되 score와 source hash를 manifest에 남기고, age·gender·wealth별 행동 차이는 prompt enactment로만 해석한다.

### 4.10 실제 뉴스-only 실행 준비도 감사

#### 최종 판정

현재 파일에는 **알려진 bearish/bullish 주입 뉴스가 섞여 있지 않다.** 그러나 현재 구현을 “실수해도 승인된 실제 뉴스만 쓰는 100-agent 논문 실행”이라고 부를 수는 없다. 판정은 다음처럼 나뉜다.

| 판정 대상 | 결과 | 의미 |
|---|---|---|
| 현재 baseline에 기존 synthetic stimulus가 들어 있는가 | **구조상 PASS** | 알려진 bearish 30개·bullish 30개의 ID·제목과 baseline 교집합 0; baseline에 fake/synthetic metadata column도 없음 |
| 현재 CSV의 row·slot 완전성 | **진단용 PASS / 10개 목표 보충 필요** | 기존 45거래일·90 decision event 중 1 event(2026-03-23 PM)가 9개이고 나머지 89 event는 10개다. 즉 현재는 899 article rows이다. 새 paper bundle은 기존 provenance-safe 기사 pool에서 **90 event × 목표 10개 = 목표 900 slot**을 먼저 재선정·봉인한다. 보충이 불가능하면 899라는 actual count·shortage 사유·ordered ID/hash를 봉인한 exception bundle로 run은 계속하며, 두 arm·report가 같은 exception을 사용한다. |
| 현재 기사가 의사결정 cutoff 당시 관찰 가능했던 version인가 | **FAIL** | 최소 1개의 확정적 backdated/end-of-day leakage와 article-version provenance 부재 |
| 기존 runner를 명시적 fake-off로 수동 실행할 수 있는가 | **조건부 가능** | baseline 경로를 정확히 지정하면 marker 기반 preflight와 fake-visible=0 검사는 작동 |
| 계획한 100명 RN_COMM_OFF/RN_COMM_ON 실뉴스-only 본 실행이 fail-closed인가 | **NO-GO** | unsafe default, 임의 CSV override, canonical clean-feed equality·cross-arm hash gate·전용 launcher가 아직 없음 |

따라서 “현재 baseline은 알려진 주입셋과 분리돼 있다”는 말은 가능하지만, “현재 코드가 진짜 뉴스-only 본 실험 준비 완료”라는 결론은 불가능하다.

#### 현재 입력의 계보와 구조 감사

2026-07-22 현재 tracked input을 읽기 전용으로 감사한 결과는 다음과 같다. 아래 hash는 **현재 상태 진단값**이지, leakage review 뒤 승인할 최종 paper bundle hash가 아니다.

| 파일 | row | 현재 SHA-256 | 감사 결과 |
|---|---:|---|---|
| `data/samsung_news_raw.pkl` | 19,560 | `b736d8bc9818cb1a969e35e7cd9b0c123940fa9be23330d29ee70f1f10799362` | source 19,560/19,560, HTTPS URL 19,560/19,560; URL 중복 0 |
| `outputs/processed_news.csv` | 19,542 | `975862fbca471b0ac42352d8024a64a243b6d8ef0aef26692bb0adeb8fbbcca9` | ID 중복 0, summary null 0, time null 29; runtime schema에는 source·URL·body 없음 |
| `outputs/daily_news_selection.csv` | 6,900 | `e1380951737eaff1419dfa5f48911af0fc2c31ea7d0208361e281568638c0b2a` | ID 중복·orphan·public-field mismatch·time null 모두 0 |

원본 source는 `mk` 17,077건과 `hankyung` 2,483건이고, URL은 모두 HTTPS이며 서로 다르다. processed 19,542건은 원본의 unique `(date,title)`에 전부 복원된다. raw 19,560건과의 18건 차이는 17개 `(date,title)` 중복 group의 excess 18행을 제거한 결과다. processed summary도 19,542/19,542건 모두 현재 `prepare_news()`의 whitespace 정리와 220자 truncation 규칙으로 raw summary에서 결정론적으로 재현된다. 즉 이 단계에서 새 LLM이 summary를 다시 창작하는 것은 아니다. 다만 upstream raw summary 생성 경로에는 Claude CLI와 fallback이 있고 model/prompt/request/output hash가 보존되지 않으므로, **실제 기사 URL이 존재한다는 사실이 summary 문장까지 검증된 사실임을 뜻하지는 않는다.** 최종 승인 시 raw body 대비 summary entailment/수치 검사를 별도로 해야 한다.

연구기간 45거래일의 실제 feed는 unique 899건이며 source는 매경 817건·한경 82건, 원본 URL도 899/899 복원된다. 그러나 runtime CSV가 URL·source·원문 body를 버리므로 실행 로그만으로는 이 lineage를 입증할 수 없다. 또한 agent가 받는 것은 **원문 전체가 아니라 제목과 raw summary를 220자 지점에서 자르고 필요 시 `...`를 붙인 파생본**이다. 논문에는 “실제 뉴스 원문을 읽었다”가 아니라 “실제 언론기사에서 파생한 제목·요약 feed를 받았다”고 써야 한다. 원문 전체 사용으로 바꾸려면 context·prompt-injection·token budget이 함께 달라지므로 별도 amendment가 필요하다.

알려진 injection 파일은 polarity별로 baseline에 30개를 추가한 구조이고, 두 polarity의 synthetic ID·제목과 baseline processed/daily의 exact 교집합은 모두 0이다. 이는 현재 baseline에 **알려진 주입 row가 없음**을 지지한다. 다만 표식이 없는 새 합성 문장을 의미적으로 탐지하는 증거는 아니다.

#### 확정적 as-of 누출 반례

현재 structural preflight는 통과하지만 기사 version의 시점 안전성은 통과하지 못한다. 확정 반례는 다음과 같다.

- ID: `news_20260427_섹터_0032`
- runtime timestamp: 2026-04-27 09:11, 따라서 2026-04-27 PM feed에 선택
- 제목: “7000피 시대 눈앞…코스피, 사상 첫 6600선 돌파”
- processed summary: 같은 날 코스피 종가 6615.03, 장중 고가 6657.22, 외국인·기관 최종 순매수와 개인 2조5243억원 최종 순매도를 서술
- raw summary/body: 삼성전자가 2.28% 오른 224,500원에 **마감**했다고 서술
- `data/stock_data.csv`의 2026-04-27 삼성전자 실제 종가: 224,500원

09:11에 존재했다고 기록된 payload가 15:30 뒤에만 확정되는 당일 종가와 최종 투자자 수급을 정확히 포함한다. 이는 단순 의심이 아니라 **확정적인 article-version/backdating leakage**다. 현재 preflight는 이 파일에 `status=pass`, 45일·90 decision event·899 article rows, fake 0을 반환하므로 `fake=0` 검증만으로 시점 안전성을 대신할 수 없다.

보조 semantic scan으로 같은 target date의 선택기사 제목+processed summary에서 `마감/종가/최고가/순매수/순매도/장중`을 찾으면 99/899건, 36거래일이 review 후보가 된다(AM 10, PM 89). 이 99건이 모두 누출이라는 뜻은 아니다. 전일 장, 해외시장, 이미 관찰 가능한 장중 수치를 말할 수 있으므로 원 URL의 publication/update version과 언급 대상·기준일을 사람이 판정해야 한다. 다만 위 2026-04-27 반례 하나만으로도 현재 feed의 본 실행 판정은 NO-GO다.

원본 PKL에는 `date,time,title,category,summary,body,source,url`만 있고 `observed_at`, `scraped_at`, `last_modified_at`, raw version/content hash가 없다. 기사 목록에서 얻은 시각과 나중에 mutable live page에서 가져온 본문이 같은 version이라는 증거도 없다. 최종 실뉴스 bundle은 cutoff 당시의 immutable snapshot 또는 이에 준하는 archived version을 가져야 한다.

#### 현재 fake-off 경로의 우회 가능성

좋은 부분도 있다.

- `scripts/08_run_six_conditions.py`의 legacy `c00_commoff_fakeoff`/`c10_common_fakeoff` mapping은 모두 `fake_news_mode=off`다.
- checkpoint runner에서 off를 명시하면 baseline processed/daily 경로를 고른다.
- `validate_news_inputs()`는 현재 중복 ID, daily→processed orphan, slot 9/10, 명시적 `is_fake` 또는 `synthetic_id` row만 검사한다. 새 paper preflight는 이를 대체해 **각 event의 target/actual real count, shortage exception, approved article-version/provenance registry**를 기록·검증한다. shortage 자체는 run 중단 사유가 아니지만, hidden shortage·pair 불일치·unsafe/duplicate/synthetic backfill은 실패다.
- 완료 검사도 off run에서 기록된 fake-visible row를 거부한다.
- 현재 application-level persistent response cache는 없어 기존 fake response가 자동으로 RN_COMM_OFF/RN_COMM_ON에 재사용되는 경로는 없다.

그러나 다음 우회가 실제로 재현됐다.

1. baseline CSV 한 건의 제목·summary를 승인 원본과 다른 합성 문장으로 바꾸되 fake marker를 넣지 않으면, 현재 `validate_news_inputs(..., fake_news_mode="off")`는 `pass`를 반환한다. baseline equality 비교가 fake-on 분기 안에만 있기 때문이다.
2. `synthetic_id`만 있고 `is_fake`가 없는 row를 `NewsAgent(include_fake_news=False)`에 주면 그 row가 그대로 노출된다. `NewsAgent`의 filter·audit는 `is_fake`만 보기 때문이다.
3. plain runner에서 `--fake-news-mode off --use-fake-news-injection`을 함께 주면 injection CSV를 선택하면서 visibility만 off로 둔다. 이 runner는 `validate_news_inputs()`도 호출하지 않는다.
4. checkpoint runner의 기본값은 agent 30명, fake on, bearish다. `08` launcher도 `--conditions`를 생략하면 가짜뉴스 포함 여섯 조건을 전부 실행한다.
5. checkpoint runner는 임의 processed/daily CSV override를 허용한다. 개별 child signature에는 파일 hash가 있지만 `08`의 arm 간 invariant에는 news hash가 없어, 먼저 실행한 legacy arm 뒤 파일이 바뀐 다음 arm도 같은 study처럼 묶일 수 있다.
6. `scripts/run_full_restart.sh`와 수동 bullish resume script처럼 fake-on을 명시한 legacy 진입점이 같은 repository에 남아 있다.

따라서 실뉴스-only 정책은 “fake row를 발견하면 필터한 뒤 계속”이 아니라 **승인된 clean bundle과 다르면 즉시 종료**여야 한다.

#### 새 전용 launcher의 강제 계약

새 `scripts/09_run_realnews_community_ab.py`만 이 study의 paper entrypoint로 인정한다.

- condition enum은 `RN_COMM_OFF`, `RN_COMM_ON` 정확히 두 개만 허용하고 child argv 차이는 `community_mode` 하나뿐이다. legacy `c00_commoff_fakeoff`, `c10_common_fakeoff`, `rn_c00_commoff_hmem`, `rn_c10_common_hmem`은 새 baseline artifact·report·run ID에 사용하지 않는다. legacy ID를 새 ID로 alias·rename·symlink하는 것도 금지한다.
- 이번 spec의 required agent 100을 exact cohort registry와 대조하고, `fake_news_mode=off`, `information_mode=pre_close_cutoff`, exact calendar/event registry를 typed study spec에서 resolve한다.
- paper CLI에서 fake variant, injection flag, processed/daily path override, arbitrary sim DB, base rebuild, offline stub를 제거한다. 알려지지 않은 argument는 reject한다.
- 승인 단계가 `real_news_bundle_manifest.json`을 만들고 raw snapshot/provenance sidecar, processed, daily, exact allowed schema, ordered clean news-ID registry, row hashes, exact slot map, selection seed·algorithm version을 봉인한다.
- RN_COMM_OFF/RN_COMM_ON은 복사본 두 개가 아니라 가능하면 동일한 read-only bundle object를 참조한다. parent가 두 child의 resolved path·file hash·canonical public-row hash·ID registry hash를 API 전에 비교한다.
- preflight 뒤와 매 phase API 직전에 bundle hash를 다시 비교해 TOCTOU 교체를 거부한다.
- 모든 visible/read/search/influential news ID, STB evidence root, LTB lineage, journal request의 external-news ID가 clean registry의 부분집합인지 closure 검사한다.
- baseline exact schema 밖의 `is_fake`, `synthetic_id`, `fake_news_id`, `related_event*`, `misinformation_*`, `false_claim`, `injection_*`, 생성 prompt/variant metadata가 한 field라도 있으면 필터하지 않고 실패한다.
- 기존 bearish/bullish injection registry의 ID·title·row-hash와 overlap 0을 defense-in-depth로 기록한다.
- output root에는 두 condition directory 외 legacy/fake directory가 없어야 하고, `outputs/logs/current`·glob·hardcoded old run은 새 evaluator 입력으로 금지한다.

#### “실뉴스-only”라는 표현의 범위

이 study가 보장하려는 것은 **외생 뉴스 입력에 synthetic fake-news injection이 0**이라는 뜻이다. `RN_COMM_ON`의 agent-generated community 글은 실제 기사 그 자체가 아니며, 현재 원 설계의 `trade_share`·`profit_share`는 실제 거래/PnL과 다를 수도 있다. 그러므로 community에서 생긴 모든 문장이 사실이라고 주장해서는 안 된다.

논문 표기는 다음처럼 고정한다.

> Both arms received the same frozen feed derived only from provenance-verified real news articles; no exogenous synthetic news stimulus was injected. Community-on agents could still generate endogenous, unverified user content, which is part of the community treatment rather than the real-news feed.

“모든 agent-visible 정보가 참이다” 또는 “커뮤니티 허위정보가 없다”는 표현은 금지한다. 후자를 원하면 community claim fact-checking이라는 별도 treatment가 되어 원 설계를 바꾸므로 이번 base run에 몰래 추가하지 않는다.

#### GO 전 실뉴스-only negative tests

최소한 다음 공격이 모두 API 호출 전에 거부돼야 한다.

1. baseline title 또는 summary 한 글자 변경, marker 0
2. injection row에서 `is_fake`·`synthetic_id`만 비우고 private fake field·제목·본문 유지
3. daily에서 거래일 하나 또는 임의 ID를 삭제해 날짜 교집합이 줄어든 입력
4. 승인 registry 밖 ID를 visible/read/search/influential/evidence/journal 중 한 곳에만 삽입
5. RN_COMM_OFF/RN_COMM_ON processed 또는 daily hash 하나만 다르게 지정
6. preflight 뒤 파일 swap, symlink, resolved path 변경
7. checkpoint 기본 실행, plain runner, six-condition default, legacy fake restart/resume를 paper manifest에 등록
8. child argv에서 community 외 agent/date/fake/news/model/reasoning/base field 하나 변경
9. old fake run의 DB·journal·log를 real-only namespace로 복사
10. full mode에서 offline stub, provider fallback, reasoning telemetry 누락
11. 2026-04-27 확정 leakage fixture와 AM/PM semantic leakage fixture가 quarantine되지 않음
12. URL/source/version/summary lineage가 끊긴 selected row 또는 허용되지 않은 article version

현재 17개 `test_experiment_safety.py` 테스트는 통과하지만 위 fake-off canonical identity, cross-arm news equality, semantic cutoff, manifest/namespace, checkpoint/journal 공격을 시험하지 않는다. 따라서 기존 테스트 통과를 실뉴스-only GO 증거로 쓰지 않는다.

### 4.11 횟수·날짜·조건·window 하드코딩 전수 감사

#### 판정 원칙

고정값이 있다는 것 자체가 문제는 아니다. 이번 study의 100명, exact cohort, 기간, AM/PM, 최초 3거래일 burn-in, Best `K=5`, BUY/SELL-only처럼 사전 승인한 조건은 **고정돼야 한다.** 문제는 같은 숫자나 규칙이 여러 모듈에 독립적으로 복제되어 일부만 바뀌거나, 입력 누락 시 분모가 조용히 줄어드는 것이다.

모든 값은 다음 네 종류 중 하나로 등록한다.

| 종류 | 의미 | 예시 | 변경 규칙 |
|---|---|---|---|
| study invariant | 이번 논문의 확정 조건 | required agents 100, RN_COMM_OFF/RN_COMM_ON, real-news-only, reasoning off, exact cohort/depth map, BUY/SELL-only | 변경 시 새 study amendment; 런타임 override 금지 |
| authored policy | 한 manifest에서 승인하는 조정 가능 정책 | ordered calendar/event registry, burn-in date IDs, Best K, read/search window, outcome horizon, retry cap | 값과 단위·version·hash를 함께 변경 |
| resolved value | canonical registry와 policy에서 계산되는 값 | `N/A/D/U/E`, active 수, row/call 수, maturity/censor 수 | 사람이 입력하지 않음; resolver만 생성 |
| measured runtime value | canary/실행에서 관측 | latency, token, physical retry, actual post/read 수 | 예상값과 분리; logical update 수를 바꾸지 않음 |

#### 기준 코드에서 확인한 P0/P1 하드코딩

| 등급 | 현재 구현/분석의 문제 | 위험 | 구현 계약 |
|---|---|---|---|
| P0 | `scripts/08_run_six_conditions.py`가 6조건·30명·seed 2를 여러 위치에 고정하고, 조건 생략 시 fake arm까지 실행 | 100명 real-only 2-arm이 아닌 실행을 정상 run으로 등록 | paper entrypoint는 typed RN_COMM_OFF/RN_COMM_ON treatment map과 exact 100-ID registry만 받음. user-authored seed도 manifest 한 곳에서 전달 |
| P0 | `simulation.py`, checkpoint runner, `run_integrity.py`, `run_logger.py`가 `×2`, `2d-1/2d`, AM/PM 홀짝을 반복 사용 | 날짜별 subturn 수가 달라지거나 기간을 확장·축소하면 turn·checkpoint·row count가 어긋남 | ordered `decision_event_id=(date,subturn)` map 하나에서 ordinal, previous/next event, cutoff, execution price, expected keys를 파생 |
| P0 | `trading_dates_between()`이 stock date와 news publication date의 교집합을 사용 | 뉴스가 빠진 날짜가 error가 아니라 연구기간에서 사라짐 | approved market-calendar/event registry가 기간의 원천. news/price/target coverage가 exact event/date set과 다르면 호출 전 실패 |
| P0 | 기존 direction validator는 `skip_initial_days=5`, actual∩simulation 교집합, 교집합 뒤 앞 N행 제거 | 확정 first-3가 아니며 누락일이 분모를 줄이고 제외 날짜까지 이동 | exact approved input/evaluation date IDs를 모두 검사. 현재 study는 45일 coverage 뒤 고정 3일 mask로 42일 평가 |
| P0 | current runtime은 STB table이 없고 scientific digest가 canonical `dim_1~dim_6` 대신 summary/change 중심 | STB/LTB 수·내용·parent link 손상이 통과 | expected decision key set과 STB key set과 LTB-transition key set의 exact equality, canonical 6D·parent/source hash digest 강제 |
| P0 | `config.py`의 depth 재생성 값은 15/55/30을 만들지만 승인 DB는 30/55/15 | 재빌드 시 Depth 2가 많아지고 active cohort가 몰래 변경 | exact 100개 agent→depth map/hash가 canonical. 현재 분포 30/55/15는 assertion이며 불일치 시 fail; 임의 재추첨 금지 |
| P0 | `select_simulation_agents(n)`은 first-N 뒤 Depth 2가 없으면 마지막 agent를 교체 | 축소 canary/cohort에서 ID가 암묵 변경 | 본 실행은 ordered exact 100 IDs만 load. canary는 별도 canary registry이며 본 cohort로 위장하지 않음 |
| P0 | news validator는 어느 slot이든 9개 또는 10개면 허용하고 `scripts/99_validate.py`는 publication-date quota 위반을 출력해도 exit 0 | 실제 shortage와 잘못된 grouping이 구별되지 않고 보고에서 사라질 수 있음 | existing provenance-safe pool에서 event→ordered **target 10 real news-ID** map/hash를 먼저 재선정·봉인한다. 부족 시 safe·unique actual count와 shortage reason을 explicit exception으로 기록해 계속하되, hidden shortage·pair mismatch·duplicate/synthetic/unsafe backfill은 실패다. `99_validate.py`는 archival diagnostic으로 격리 |
| P0 | arm equality가 resolved calendar/event, cohort/depth, news hash, burn-in mask, memory/reasoning policy 전체를 비교하지 않음 | community 외 조건도 달라져 paired contrast가 깨짐 | RN_COMM_OFF/RN_COMM_ON resolved manifests의 structural diff를 계산하고 treatment allowlist `community_mode` 하나 외 차이가 있으면 fail |
| P1 | LLM seed가 global turn ordinal, news seed가 `day_index×2+slot`에 의존 | 앞 날짜를 줄이면 겹치는 calendar event의 seed도 달라짐 | seed namespace를 `(study_seed,condition_pair_id,agent_id,date,subturn,stage,logical_validation_attempt)`에서 결정; paired arm에는 treatment-independent 부분을 동일하게 사용 |
| P1 | 최근 주문 5건과 community author 최근 거래 3건이 direct prompt path에 있고, Depth 2 검색 최근 7 calendar days/top 10, category 5/3/2, read cap 5/10, Best 5가 여러 코드에 산재 | 과거 거래/비공개 portfolio가 LTB·public-profile 경계를 우회하고, 나머지 window도 숨은 treatment가 됨 | historical order/fill direct visibility와 author private portfolio/trade는 금지한다. 나머지는 이름·수·단위(calendar day/trading day/decision event/item)·eligibility·empty behavior를 policy manifest에 고정하고 request audit에 resolved value 기록 |
| P1 | H1/H5와 next-turn outcome 수를 기간 산술로 직접 계산할 유혹 | 짧은 기간·불균일 subturn에서 false missing 또는 미래 누출 | horizon policy를 ordered market calendar/event map에 resolve해 due event와 right-censor key set을 먼저 생성 |
| P1 | validation retry와 transport retry, worker/concurrency가 모듈 기본값에 의존 | 비용/latency가 arm별로 달라지고 logical STB/LTB 횟수와 혼동 | stage별 call policy 한 곳에 freeze. physical attempt는 별도 audit이며 committed logical key count에는 미포함 |
| P1 | `config.N_WARMUP=3`, `N_TRANSITION=4`, `ORDER_CUTOFF_TIME`, commission 값 일부가 실행 경로에서 미사용 또는 체결과 불일치 | 설정 파일을 바꿨는데 실험은 안 바뀌는 false control | dead constant 제거/legacy 표시. effective runtime value와 request/exchange evidence를 run signature에서 검증 |
| P2 | MA5·MA20·20-day volatility 등 시장 feature horizon도 코드 내부 상수 | 입력 의미가 코드 버전에 따라 달라질 수 있음 | feature policy/version과 lookback 단위를 manifest 및 prompt hash에 포함 |
| P2 | 과거 analysis/report가 특정 run ID·45일/20일·24/30/50명·AM/PM·`range(1,91)`을 고정 | 새 결과가 과거 분모·경로로 분석될 수 있음 | legacy analysis는 archival-only. 새 evaluator/report는 resolved manifest와 exact artifact hash만 입력으로 받음 |

#### 단일 count/key 계약

resolver는 다음 집합과 count를 만든다.

```text
N = exact ordered cohort registry의 cardinality
A = ordered condition treatment map의 cardinality
D = ordered approved trading-date set의 cardinality
Q_d = date d의 ordered decision-event list
U = Σ_d |Q_d|
B = exact burn-in date-ID set
E = ordered approved dates에서 B를 제외한 evaluation-date set
P = ordered community/no-op phase-key set의 cardinality
K = {(condition, agent, date, decision_event)}

committed STB key set = K
committed LTB-transition key set = K
committed decision/fill key set = K

arm별 STB rows = N×U
arm별 LTB transition rows = N×U
arm별 LTB states = N×(U+1)  # LTB_0 포함
arm별 fills = N×U
date d의 fills = N×|Q_d|
primary fill keys/arm = Σ_{d∈E} N×|Q_d|
scientific phases/arm = U+P
```

총 row 수가 우연히 같기만 해서는 안 된다. 동일한 `(condition,agent,date,decision_event)` composite-key set이어야 한다. STB 저장 뒤 LTB가 실패하면 그 decision event의 STB도 scientific commit하지 않으며 journal response만 replay 가능하게 남긴다. Community stage는 decision event가 아니므로 STB/LTB count에 더하지 않고, 다음 허용 decision event의 evidence를 예약한다.

Community의 posting/read/Best 수는 `70×D`, `100×D`를 일반 불변식으로 두지 않는다. 각 phase의 permission map, read cap, non-empty Best 여부, in-study next-visible event 존재 여부를 resolve해 exact opportunity/schedule/exposure key set을 만든다. 같은 방식으로 news row 수는 event→ordered news-ID map, outcome 성숙 수는 `(fill,horizon)→due_event|right_censored` map에서 계산한다.

#### 날짜와 분석 분모 계약

- authored calendar/event registry가 유일한 날짜 원천이다. `start/end`, `D`, `U`는 resolver 출력이며 별도 입력으로 받지 않는다.
- 현재 본 실험은 exact 45거래일을 모두 실행하고 exact first-3 date mask를 제외해 42일을 primary로 쓴다. burn-in 기간에도 거래·STB·LTB는 모두 정상 실행된다.
- actual target, stock price, news slot, fill, stance 중 하나라도 approved coverage와 다르면 교집합을 취하지 않고 실패한다.
- sensitivity 0/1/5일도 결과 row를 보고 자르는 옵션이 아니라 사전 생성한 별도 exact date-mask/hash다.
- 동일 event의 seed는 ordinal이 아니라 immutable date/subturn ID에서 파생해 기간 앞부분을 추가·제거해도 겹치는 event의 seed가 변하지 않게 한다.

#### 확장·축소 property tests

- `N={1,7,100}`, `D={1,2,45,63}`, `{AM}`, `{AM,PM}`, 날짜별 불균일 decision-event fixture에서 모든 expected count/key를 resolver 결과로 검사한다. 이는 core의 일반성 test이며 본 paper run의 `N=100` 조건을 완화하지 않는다.
- STB/LTB/fill 중 한 key 누락·중복·서로 다른 key의 동수, LTB parent 단절, retry를 update로 중복 산입한 경우를 각각 reject한다.
- requested 100명인데 99/101명 또는 다른 ID가 있거나 depth map이 승인 hash와 다르면 API 전 reject한다.
- 뉴스·target·price 날짜 하나 누락, 유일한 9-news slot 위치 변경, burn-in mask 이동, PM 없는 날짜를 넣은 fixture에서 조용한 교집합/`×2` 계산이 없는지 검사한다.
- RN_COMM_OFF/RN_COMM_ON resolved diff가 `community_mode` 이외이면 reject하고, resume 시 manifest/event-map hash가 바뀌면 이어서 실행하지 않는다.
- 임의 `N/D/U` fixture를 report에 넣어 30·45·50·90 같은 과거 숫자가 결과 문구에 섞이지 않는 snapshot test를 둔다.

### 4.12 모듈 정합성·연결성·하드코딩 아키텍처 감사

이번 구현은 기능을 각각 고치는 것으로 충분하지 않다. **하나의 experimental contract가 launcher부터 final report까지 끊기지 않고 전달되는지**가 P0다. 현재는 `config.py`, runner argv, DB, CSV, report script가 같은 의미의 값을 각자 들고 있어 경로 하나만 바뀌어도 실험 정의가 달라질 위험이 있다.

새 paper path의 허용 흐름은 다음 하나로 고정한다.

```text
study_spec.json + frozen cohort/calendar/news/base registries
  → resolver
  → resolved_study_manifest.json + immutable RunBundle
  → RN pair launcher / event scheduler
  → typed stage inputs (STB / analysis / decision / LTB / community)
  → side-effect-free staged results
  → one atomic scientific commit + append-only ledger/traces
  → run integrity / paired evaluator
  → run-scoped report bundle + RUN_RECORD.md
```

| 경계 | 필수 계약 | 현재 위험 | P0 수정/검증 |
|---|---|---|---|
| authored spec → resolver | 사람이 쓴 policy와 registry만 입력, 모든 `N/D/U/B/E`는 파생 | launcher·config·script가 같은 count/date를 중복 보유 | `study_spec.json`과 `resolved_study_manifest.json`을 분리하고 runtime은 후자 SHA-256 하나만 수용 |
| launcher → scheduler | condition/cohort/calendar/news/base를 explicit `RunContext`로 전달 | fake/default argv, global `config`, `first_n`, stock/news intersection이 의미를 바꿈 | RN 전용 launcher가 unknown override를 reject하고 event map만 순회 |
| scheduler → stage module | 모든 stage가 typed input/output schema와 event ID를 사용 | raw dict와 global turn/AM·PM shortcut이 module마다 다름 | `decision_event_id`·run/condition/agent key와 exact schema validator를 경계마다 강제 |
| LLM stage → state write | LLM task는 validated result만 반환, DB write는 commit coordinator만 수행 | 현재 `daily_cycle`/community 경로가 중간 DB write·`UPDATE`를 수행 | STB→decision→fill→LTB 결과를 staging하고 full cohort barrier 뒤 한 transaction으로 commit |
| DB → exporter/traces | append-only canonical table과 compatibility export가 one-to-one | legacy pending `trade_log` overwrite, CSV가 field를 버림 | final `paper_fill_ledger` + deterministic export + reconciliation hash, 새 table은 reset/base lifecycle에도 등록 |
| checkpoint/resume → integrity | response journal은 physical call, scientific DB는 committed logical event | resume가 new table·WAL·input drift를 빠뜨릴 수 있음 | manifest/input/digest re-hash, all runtime tables lifecycle 등록, insert-or-identical-hash |
| integrity → evaluator/report | evaluator는 frozen run artifact만 읽음 | global `sys_100.db`, legacy CSV/PDF, date intersection fallback | input allowlist·schema/hash check와 hard-fail; `RUN_RECORD.md`가 source/output을 색인 |

**모듈성 원칙**:

1. 실험 의미를 바꾸는 값은 `config.py` 기본값이나 module constant가 아니라 `StudySpec → resolved manifest`에서만 온다. `config.py`는 legacy default 또는 transport-level default로만 남기며 paper path에서 override/검증된다.
2. `simulation.py`는 event orchestrator, `daily_cycle.py`는 side-effect-free stage composer, `exchange_agent.py`는 deterministic execution, `run_logger.py`는 export adapter로 역할을 분리한다. 한 module이 다른 module의 private DB helper를 직접 호출해 state를 바꾸지 않는다.
3. DB table·CSV·trace·report에 같은 fact가 있으면 canonical source와 compatibility projection을 하나씩 명시한다. 어느 report도 “가장 최근 파일”을 glob으로 찾지 않는다.
4. 새 STB/LTB/outcome/community-exposure table을 추가할 때 schema만 고치지 않는다. `experiment_runtime.RUNTIME_TABLES`, clean-base builder, reset, checkpoint snapshot/restore, integrity digest, exporter, archive, migration test를 **동시에** 수정해야 한다.
5. logical event count와 physical retry count, public post claim과 actual fill, raw source body와 sanitized claim, human log field와 scientific state를 각각 다른 namespace로 둔다.

**현재 즉시 확인된 P0 연결 결함**:

- `outputs/experiment_base_sim.db`가 0 byte라 existing-base 존재 여부만으로는 paper run을 시작할 수 없다. 승인 단계에서 run-scoped clean base를 build·validate·hash하고, paper run 중 자동 rebuild는 금지한다.
- runtime DB, master CSV, legacy analysis artifact가 서로 다른 turn/row 수를 가질 수 있다. 새 evaluator는 legacy master CSV를 정답 원천으로 쓰지 않고 canonical final fill ledger와 exact key set만 사용한다.
- current runtime table reset list에 새 memory/ledger table이 자동 포함되지 않는다. 등록 누락은 이전 run의 belief·community trace가 다음 run에 남는 P0 contamination이다.
- 기존 module은 `INSERT OR REPLACE`, mutable community log, global DB path, untyped context dict를 사용한다. 새 paper path에서는 append-only/idempotent write와 schema/versioned DTO를 강제한다.

## 5. 관련 연구에서 가져올 것과 가져오지 않을 것
---

## 5. 관련 연구에서 가져올 것과 가져오지 않을 것

### 5.1 FUSE의 핵심 아이디어

FUSE는 social interaction에서 최근 접한 의견을 short-term memory로 요약하고, 기존 long-term memory와 새 short-term memory를 다시 통합한 뒤, 그 장기기억과 이전 의견을 이용해 다음 의견을 갱신한다. 공식 구현의 `citizen.py`와 `prompt.py`도 대체로 다음 흐름이다.

```text
neighbors' opinions
→ short opinion memory
→ short summary
→ previous long summary + short summary
→ new long summary
→ opinion / belief update
```

이 연구가 가져올 핵심은 **STM → LTM → belief/opinion update**라는 인과적 골격이다.

### 5.2 그대로 복사하면 안 되는 이유

FUSE와 본 연구는 연구대상과 시간구조가 다르다.

| 항목 | FUSE | 본 연구 |
|---|---|---|
| agent | 40명 social role | 100명 investor persona |
| 환경 | social graph와 이웃 의견 | 단일 공개 종목토론방 |
| 시간 | day 단위 | AM·PM·post-PM community |
| 핵심 상태 | binary belief/opinion과 reasoning | 6차원 belief, portfolio, order |
| 정보 | 진짜 뉴스가 변형되는 과정 | 우선 실제 뉴스만 사용 |
| memory 구현 | Python 객체의 재귀적 자유요약 | SQLite 기반 evidence-linked persistent memory 필요 |
| 복구 | 논문 simulation loop 중심 | 장시간 API 실행의 checkpoint/resume 필수 |

FUSE 구현의 재귀적 LLM summary를 그대로 쓰면 다음 문제가 생긴다.

- 원문·노출 ID와 요약 사이 provenance가 사라짐
- 요약의 환각이 다음 요약에서 사실처럼 강화됨
- 같은 내용을 반복 요약하며 자기확증이 커짐
- correction·contradiction·retraction을 추적하기 어려움
- memory용 LLM call과 비용이 `N×U×arm 수`에 비례해 증가
- process crash 후 in-memory state를 재현하기 어려움

### 5.3 본 연구에 맞춘 adaptation

다음처럼 바꾼다.

```text
FUSE recent opinions
→ 본 연구의 실제 exposure event와 recent episode

FUSE STM summary
→ current information만 반영한 6차원 Short-term Belief

FUSE recursive LTM summary
→ previous Long-term Belief + current Short-term Belief + 이번 Decision/Fill episode + 이미 관찰 가능한 가격 성찰로 거래 뒤 재귀 갱신한 6차원 Long-term Belief

FUSE opinion update
→ previous Long-term Belief와 current Short-term Belief를 분리 block으로 Decision-Making Process를 거쳐 BUY/SELL·수량 decision을 만들고 실제 fill로 실행
```

기존 `update_belief` call을 current-information-only STB 생성에 재사용하고, `update_long_term_belief.txt` call을 매 턴 정확히 하나 추가한다. FUSE의 `previous LTM + current STM → new LTM` 골격을 **`previous LTB + current STB → Decision-Making → committed fill_t → (previous LTB + current STB + decision/fill episode + eligible price outcome) → next-event-visible LTB`**로 옮긴다. LTB updater는 fill row를 문자열로 붙이는 것이 아니라, 이번 판단과 실제 체결을 별도 transaction episode로 해석한다. **LTB는 매 turn 여섯 차원을 새로 작성하지만, transaction episode와 price outcome의 직접 영향은 `dim_6`으로 제한하고 `dim_1~dim_5` 변화는 해당 current STB evidence에만 근거한다.** 원문 evidence ID, decision/fill ID, parent/version hash를 함께 저장해 drift를 감사한다. 별도의 세 번째 fused-belief call은 만들지 않는다.

FUSE 원 구현은 LTM을 “최근 5일”로 제한하지 않고 매 simulation day 재귀 갱신한다. 본 연구가 AM/PM 두 번 거래 판단을 한다는 차이를 반영해, LTB 갱신 단위를 day가 아니라 **global turn**으로 맞춘 것이 이번 adaptation이다.

### 5.4 TwinMarket과의 정확한 관계

TwinMarket 원 논문은 belief를 경제 펀더멘털, 시장 가치평가, 단기 추세, 주변 투자자 심리, 자기평가의 5차원으로 설명한다. 그러나 공개된 belief update prompt는 이 차원들을 별도 DB field로 재조회하지 않고, 다섯 관점이 들어 있는 이전 belief 서술문 전체를 `{old_belief}`로 받아 새 belief 한 문단을 작성한다. 논문은 과거 거래·시장 기록과 현재 뉴스·소셜 정보를 belief 형성에 사용하고 행동 feedback으로 다음 belief를 갱신하지만, 명시적인 STM/LTM 계층이나 누적 LTM updater는 제시하지 않는다.

따라서 현재 삼성 baseline의 `get_previous_belief()`가 `belief_summary`만 반환하는 것을 “TwinMarket도 원래 summary만 조회하므로 그대로 맞다”고 해석하면 안 된다. 삼성 baseline은 이미 `dim_1~dim_6`을 별도 canonical field로 저장하므로 summary만 넘기면 구조화 과정에서 얻은 정보가 사라진다.

이번 설계의 계보는 다음처럼 구분한다.

- **TwinMarket에서 유지**: BDI식 순환 belief update, 현재 정보·과거 거래·사회정보가 belief를 거쳐 행동으로 이어지는 골격.
- **삼성 baseline에서 유지**: `dim_1~dim_6`, `belief_summary`, `view_change`, news→belief→analysis→decision 흐름.
- **FUSE에서 adaptation**: current short state와 previous long state를 재귀적으로 결합해 다음 상태를 반복 갱신하는 계층 구조.
- **본 연구의 최소 수정**: 같은 의미의 six dimensions를 STB와 LTB에 사용하고, 거래에는 `previous LTB + current STB` 두 block을 유지하며, 기존 `belief_history`는 다음 event용 LTB로 유지한다.

즉 full 6D prior 복원은 TwinMarket의 동적 belief 의도를 버리는 변경이 아니라, 현재 삼성 코드가 이미 채택한 6차원 표현을 실제 다음 턴까지 온전히 이어 주는 복구다.

### 5.5 계층 memory·거래 성찰 관련 1차 연구 비교

아래 비교의 목적은 논문에서 임의의 숫자나 이름을 가져오는 것이 아니라, **각 연구가 실제로 무엇을 저장하고 언제 갱신했는지**를 구분해 우리 기존 6D belief에 필요한 원칙만 채택하는 것이다.

| 연구 | 논문에서 실제 사용한 memory 구조와 갱신 | 거래/행동 성찰 | 본 연구의 채택 판단 |
|---|---|---|---|
| [FUSE](https://aclanthology.org/2025.emnlp-main.1330.pdf) | persona와 역할을 별도로 두고, 당일 이웃 상호작용을 STM으로 요약한 뒤 `previous LTM + current STM → current LTM`으로 simulation day마다 누적한다. “최근 5일 LTM” 규칙은 없다. | 장기기억과 기존 opinion을 이용해 news/opinion을 다시 갱신하지만 금융 체결·수익 성찰은 없다. | **재귀 갱신 골격만 채택.** day 대신 우리 AM/PM global turn마다 `previous LTB + current STB → Decision-Making → actual fill → previous LTB + current STB + current decision/fill episode + eligible earlier price outcome → next-event-visible LTB`를 수행한다. FUSE의 별도 opinion state는 기존 6D belief와 중복되므로 복사하지 않는다. |
| [FinMem](https://arxiv.org/abs/2311.13743) | profiling, working memory, shallow/intermediate/deep LTM을 분리한다. working memory가 요약·관찰·즉시/확장 성찰을 하고, LTM 사건을 recency·relevance·importance로 순위화해 각 층 top-K를 조회한다. | 즉시 성찰은 당일 판단, 확장 성찰은 여러 거래의 가격·수익·행동 이유를 재평가해 deep layer에 저장한다. training에서는 미래 가격 방향 label도 사용한다. | **이번 v1에는 비채택.** 3층 저장·vector/top-K 검색·감쇠·자동 승격·별도 reflection·미래 label·HOLD action 모두 구현하지 않는다. 금융 memory 설계의 대안 사례로만 비교한다. |
| [TradingGPT](https://arxiv.org/abs/2309.03736) | short/middle/long 세 층에 서로 다른 decay를 두고 각 층 top-K 사건을 거래에 사용한다. | daily immediate reflection이 추천·거래량·수익을 담고, designated period의 extended reflection이 가격·거래추세·자기평가를 요약한다. 논문은 period 예시로 week를 들지만 보편 규칙은 아니다. | **이번 v1에는 비채택.** 세 memory 층, top-K, decay, 별도 immediate/extended reflection, 5단계/HOLD action, 주간 window를 구현하지 않는다. |
| [Generative Agents](https://doi.org/10.1145/3586183.3606763) | 경험 원문을 comprehensive memory stream에 계속 쓰고 recency·relevance·importance로 조회한다. 여러 기억에서 만든 higher-level reflection도 다시 stream에 기록한다. | 금융 성찰은 아니지만 원 경험과 그 경험에서 파생된 고수준 판단을 구분한다. | **raw ledger와 파생 belief의 분리 근거로 채택.** 원문 사건은 보존하고 거래에는 승인된 previous LTB와 current STB만 전달한다. full retrieval subsystem은 이번 v1에 넣지 않는다. |
| [Reflexion](https://arxiv.org/abs/2303.11366) | trajectory history를 STM, evaluator feedback으로 만든 verbal self-reflection을 LTM으로 두고 다음 trial의 actor가 둘 다 사용한다. 문맥 한계 때문에 보통 1~3개 experience로 cap했다. | 행동 뒤 평가 신호가 생긴 다음에만 “무엇을 다르게 할지”를 LTM 교훈으로 쓴다. | **성찰 시점만 채택.** 체결 즉시 성공/실패를 상상하지 않고, reference price가 관찰된 next-turn/H1/H5에만 평가 교훈을 만든다. 1~3은 day/window 규칙으로 복사하지 않는다. |
| [TwinMarket](https://papers.neurips.cc/paper_files/paper/2025/file/5bf234ecf83cd77bc5b77a24ba9338b0-Paper-Conference.pdf) | BDI식 belief→action→social feedback 순환이며 명시적 STM/LTM 계층은 아니다. | 과거 거래·시장·사회정보가 다음 belief에 영향을 주지만 layered memory 계약은 제시하지 않는다. | **기존 belief·행동 골격을 유지.** 계층화 때문에 삼성 baseline의 6D 의미를 다시 정의하는 근거로 사용하지 않는다. |

FinMem의 숫자는 특히 그대로 옮기면 안 된다. 논문은 각 층에서 top-K 사건을 가져오는 구조이고, 현재 공식 TSLA 예시 설정은 `top_k=3`, `look_back_window_size=7`, 층별 recency factor `3/90/365`다. 이는 **최근 3일·7일만 LTM에 남긴다는 뜻이 아니라 특정 구현·종목의 검색/성찰 hyperparameter**다. FUSE의 재귀 LTM에도 5일 제한이 없다. 따라서 본 연구의 H1/H5는 outcome 평가 horizon이지 memory retention window가 아니다.

또한 FUSE 본문에는 LTM을 갱신한 뒤 행동하는 algorithm 설명과, 의사결정 식에서 직전 LTM을 표기한 부분이 함께 있어 index 표기가 완전히 명료하지 않다. 본 연구는 현재 뉴스가 현재 거래에 직접 반영되어야 하고 같은 근거를 두 번 세면 안 된다는 코드 목적에 맞춰 **`LTB_(t-1) + STB_t`로 먼저 거래하고, 거래 뒤 `LTB_t`를 다음 event용으로 새로 쓴다**고 명시적으로 고정한다. 이는 FUSE의 문자 그대로 복제가 아니라 사전등록한 project-specific adaptation이다.

FUSE 공식 구현의 STM/LTM 요약 prompt 자체에는 persona가 직접 들어가지 않고, persona·role은 이후 opinion/news 갱신에 사용된다. 따라서 본 설계가 frozen persona를 STB 해석 렌즈와 LTB 일관성 제약으로 넣는 것도 FUSE의 그대로인 기능이 아니라, 기존 삼성 investor persona를 보존하기 위한 project-specific adaptation이다. persona는 두 arm에서 hash-identical하고 memory가 수정하지 못해야 한다.

### 5.6 문헌 검토 뒤 확정하는 최소변경 원칙

문헌은 새로운 LTB 내용 taxonomy를 정해 주지 않는다. 오히려 연구별 task와 memory 단위가 서로 다르므로 다음처럼 기존 코드 계약을 보존한다. **2026-07-22 사용자 확인에 따라 재귀 통합을 확정 구조로 채택한다.**

1. STB와 LTB는 모두 기존 `dim_1~dim_6`을 사용하며, field 의미와 글자 제한을 바꾸지 않는다.
2. STB는 현재 턴에 새로 관찰 가능한 정보가 기존 여섯 관점에 주는 해석이고, LTB는 같은 여섯 관점의 누적 최신판이다.
3. persona는 기존 삼성 baseline의 별도 frozen state로 남겨 해석 일관성에만 쓰고, LTB 내용·evidence·갱신 대상으로 넣지 않는다.
4. 뉴스·community 원문·fill·portfolio·outcome은 Generative Agents식 원기록 보존 원칙에 따라 append-only ledger에 남기되, agent-visible 제3 memory로 취급하지 않는다.
5. 거래 fill 사실과 가격상 결과는 STB에 넣지 않는다. **이번 turn의 committed decision/fill episode는 체결 직후 post-fill LTB updater에 한 번만** 들어가고, 가격상 결과는 first-visible/maturity event가 된 뒤에만 별도 price-outcome packet으로 소비한다. 둘은 기존 `dim_6`의 자기평가 근거이며, 새 STB 근거 없이 `dim_1~dim_5`를 직접 뒤집는 독립 evidence가 아니다. 이는 별도 거래 reflection memory가 아니라 기존 6D 재귀 update의 입력이다.
6. 첫 구현에는 FinMem/TradingGPT의 embedding, top-K, 세 decay layer, 자동 승격, 별도 immediate/extended reflection을 넣지 않는다. 추후 도입하려면 동일 6D STB/LTB와 분리된 후속 memory ablation으로 새로 승인한다.
7. v1은 차원 내부를 새 item taxonomy로 쪼개지 않는다. 대신 dimension별 parent/current hash·integration evidence·evidence edge를 서버가 기록해 재귀 갱신의 소실·출처 세탁을 감사한다. stable item list는 manifest-derived `U` 전체와 그보다 긴 확장 stress canary에서 실제로 필요성이 확인될 때만 후속 amendment로 검토한다.

따라서 이 문서에서 “LTB에 무엇이 들어가는가”의 답은 새로운 목록이 아니라 **기존 여섯 차원의 재귀적으로 통합된 최신 내용**이다. raw 사건은 ledger에 남고, 현재 거래에는 직전 LTB와 현재 STB의 여섯 rendered field가 분리되어 들어간다. 이번 설계의 memory architecture 근거는 FUSE이고, FinMem/TradingGPT는 비채택 대안 비교에만 사용한다.

FUSE-EVAL에도 여섯 개 deviation metric이 있지만 이는 뉴스 왜곡을 평가하는 Sentiment/New Information/Certainty/Style/Temporal/Paraphrasing 계열 지표다. 삼성 baseline의 투자 belief `dim_1~dim_6`과 이름·의미·계보가 전혀 다르므로 서로 연결하거나 차원을 맞추지 않는다.

---

## 6. 제안 계층 Belief 아키텍처

### 6.1 결론: 제3의 belief를 만들지 않는다

현재 삼성 baseline은 belief가 거래의 핵심 중간 상태지만, 거래가 belief 하나만으로 결정되는 구조는 아니다. 코드상 market analysis는 belief와 현재 시장정보·뉴스 해석·포트폴리오를 받고, decision은 다시 belief·market analysis·포트폴리오·최근 주문·거래 제약을 받는다. 이번 수정은 이 골격을 버리지 않되, **기존 `today_belief` 자리를 직전 LTB와 현재 STB라는 두 역할 블록으로 명시적으로 나누고, 거래 뒤 새 LTB를 재귀 생성하는 thin adaptation**이다.

```mermaid
flowchart LR
    E["현재 turn의 cutoff-safe external evidence<br/>뉴스 · 시장 · 실제 노출 community"]
    S["STB_t<br/>현재 단기 해석 dim_1~dim_6"]
    P["LTB_(t-1)<br/>직전 장기기억 dim_1~dim_6"]
    A["market analysis<br/>LTB_(t-1) + STB_t<br/>+ 현재 시장·portfolio"]
    D["decision<br/>BUY/SELL·수량"]
    X["실제 시가/종가 전량 체결"]
    R["이번 turn의 structured decision/fill episode<br/>+ 새로 visible한 과거 price-outcome packet"]
    L["LTB_t<br/>재귀적으로 새로 작성한 다음 turn용 장기기억"]
    G["append-only fill/outcome ledger"]

    E --> S
    P --> A
    S --> A
    A --> D
    D --> X
    X --> G
    P --> L
    S --> L
    R --> L
    L -. "다음 decision event" .-> P
    G -. "post-fill transaction episode / 이후 price outcome" .-> R
```

정확한 agent belief 상태는 두 개다.

- **Short-term Belief `STB_t`**: frozen persona가 현재 AM/PM에 새로 관찰 가능한 뉴스·validated community 신호를 어떻게 해석하는지 여섯 차원에 기록한다. 이전 STB, 이전 LTB, 시장/portfolio execution state, 과거 거래 성찰, summary 문장을 입력하지 않는다.
- **Long-term Belief `LTB_t`**: `LTB_(t-1) + STB_t + current decision/fill transaction episode + newly visible earlier price-outcome packet`을 **actual fill 뒤** 재귀 통합해 새로 작성한 누적 여섯 차원이다. 다음 decision event에서 `LTB_(t-1)` 역할을 맡는다. transaction episode는 `dim_6`의 mandatory non-evidentiary context이고, frozen persona는 장기 관점의 일관성 제약이지만 갱신 대상이나 독립 근거가 아니다.

뉴스·시장·community·fill을 저장하는 ledger는 agent의 세 번째 기억이 아니며, STB/LTB가 실제 무엇을 보았는지 감사하는 source of truth다. FUSE에는 long memory와 별도로 opinion update가 있지만, 우리 코드에서는 기존 belief가 이미 행동용 opinion state이므로 **새 opinion/belief call을 더 만들지 않고 LTB와 합친다.** 이 차이를 문서와 논문에서 adaptation으로 명시한다.

### 6.2 Immutable event/exposure ledger

ledger는 append-only이며 다음을 원형에 가깝게 보존한다.

- 실제 뉴스의 URL·source·published/observed/last-modified 시각, raw/summary content hash와 version
- agent에게 실제 제공된 뉴스·검색 결과·community 본문 exposure
- community post, candidate metadata, 실제 read, reaction, Best 5 원문 broadcast
- 시장 가격·각 feature의 as-of 시각
- STB, LTB, analysis, decision, order, actual fill, portfolio state
- next-turn mark와 H1/H5 maturity outcome
- constraint, retry, recovery, validation event

원 source payload, agent-visible payload, evaluator-only target을 물리적으로 분리한다. 실제 `Individuals` target, 미래 기사 correction, 타 agent의 private state는 STB/LTB registry에 들어갈 수 없다. ledger는 evidence의 **출처**를 보증할 뿐 그 주장이 참이라는 것을 보증하지 않는다.

### 6.3 STB: current-only 여섯 차원

STB는 단순 raw context 묶음이 아니라, 현재 턴 정보가 기존 여섯 관점에 주는 **단기 해석**이다.

| STB field | 현재 턴에서 답할 질문 |
|---|---|
| `dim_1` | 지금 관찰한 정보가 향후 약 1개월 주가 방향에 무엇을 시사하는가? |
| `dim_2` | 지금 정보가 valuation 판단에 무엇을 새로 시사하는가? |
| `dim_3` | 지금 드러난 거시·환율·금리·반도체 업황 신호는 무엇인가? |
| `dim_4` | 지금 관찰된 시장심리·수급·다른 투자자 분위기는 무엇인가? |
| `dim_5` | 이번 뉴스·community 사건을 어떻게 해석하는가? |
| `dim_6` | 이번 뉴스·community 신호가 현재 확신·위험·규율에 주는 **즉시적** 함의는 무엇인가? |

STB 입력에 허용되는 것은 run 시작 전에 hash-pin한 **frozen persona profile**과 **현재 cutoff까지 새로 보이는 뉴스·validated community typed evidence**다. 시장가격·portfolio snapshot·현금·보유수량은 STB가 아니라 analysis/decision의 direct execution-state block이다. 과거 fill과 price outcome은 STB에 넣지 않는다. actual fill 뒤에는 current decision/fill episode가 post-fill LTB의 별도 structured field로 한 번 들어가고, earlier markout/matured outcome만 각 due event의 LTB price-outcome packet으로 소비한다. persona의 전략·위험성향·행동특성·허용된 인구통계는 같은 정보를 다르게 해석하는 렌즈이며 evidence ID나 support count를 갖지 않는다. 반면 다음은 금지한다.

- 이전 STB 또는 이전 LTB의 여섯 문장
- 과거 fill, markout, matured outcome 또는 과거 거래 성찰
- 이전/current `belief_summary`, `view_change`
- current market features·announced price·portfolio/cash/holding state
- 자유문장 `action_reason`, `risk_control`의 재주입
- 아직 발생하지 않은 current-turn fill
- 같은 날 최종 실제 개인 수급 target
- 실행 중 persona의 전략·depth·인구통계·행동특성을 memory가 수정한 값

STB는 매 AM/PM 정확히 하나 생성하고 `short_term_belief_history`에 저장한다. turn 0에는 STB가 없다. 초기 기존 belief는 `LTB_0`다.

### 6.4 LTB: 거래 뒤의 재귀 장기기억 갱신

LTB는 “과거 5일만 보관하는 별도 메모”도, STB를 걸러 버리는 필터도 아니다. 매 turn actual fill 뒤 `LTB_(t-1)`에 현재 STB, **이번 actual decision/fill episode**, 새로 확인된 과거 거래 결과에 대한 성찰을 통합해, 다음 turn에 사용할 장기기억을 새로 작성한다.

```text
LTB_t.dim_i = recursive_rewrite(
  LTB_(t-1).dim_i,
  STB_t.dim_i,
  current_decision_fill_episode_t,
  newly_visible_earlier_price_outcome_t,
  evidence_registry
)
```

`LTB_t`는 current decision의 입력이 아니라 **다음 decision event의 long-term block**이다. 이 순서가 현재 STB를 거래에 직접 반영하면서 같은 뉴스·community를 `LTB_t`와 `STB_t` 두 번 세는 문제를 막는다.

LTB의 여섯 차원 의미와 기존 글자 제한은 기존 belief와 동일하게 유지한다. 그러나 STB와 질문의 시간 역할은 다르다. STB는 “이번 정보가 지금 무엇을 뜻하는가”이고, LTB는 “그 정보를 기존 장기 관점, **이번 실제 decision/fill**, 이전 거래 성찰에 비추어 어떤 투자자 기억으로 남길 것인가”다. 특히 LTB `dim_6`은 이번 체결의 outcome-pending 상태와 이전 체결의 markout·성숙 outcome에서 배운 방향·수량·위험 관리의 교훈이 이어지는 자리다.

LTB updater에는 STB와 동일한 frozen persona profile/hash를 read-only 일관성 제약으로 넣는다. 이는 서로 다른 persona의 누적 관점이 동일 문장으로 수렴하는 것을 막기 위한 것이지, persona를 반복 support로 가중하기 위한 것이 아니다. updater가 persona field를 바꾸거나 새 persona 특성을 추론해 저장하면 validation failure다.

**`maintain` action과 서버의 exact-copy 경로는 두지 않는다.** 모든 turn에서 LTB updater는 여섯 차원을 새 텍스트로 작성한다. 새 STB가 장기 방향을 뒤집지 않더라도, 기존 material assertion을 보존하면서 이번 단기 신호와 새 거래 성찰을 거친 현재의 확신·경고·위험·행동 교훈을 명시해야 한다. 이전 문장을 byte-identical하게 복사하거나 여섯 차원 중 하나를 비워 두면 validation failure다. 이는 token 절감 정책이 아니라 STB와 LTB의 역할을 실제로 분리하기 위한 계약이다.

v1은 기존 코드를 크게 뜯지 않기 위해 차원 내부를 stable item list로 다시 분해하지 않는다. 대신 다음 lineage를 강제한다.

- `integration_evidence[dim_i]`: `dim_1~dim_5`는 현재 `STB.dimension_evidence[dim_i]` support/contradict ID의 승인된 부분집합이고, `dim_6`만 이번 turn에 due인 earlier price-outcome event ID를 추가할 수 있다. current decision/fill episode는 별도 `transaction_episode` input lineage이며 evidence ID/support count가 아니다. community는 public `post_id`가 아니라 server-owned `community_claim_id`를 쓰며, 서버가 `community_claim_id → community_claim_sources → source_exposure_ids → distinct root_post_ids`를 resolve한다. 모델이 새 ID·과거 ID를 만들 수 없다.
- historical provenance: `previous_ltb_id → ... → LTB_0` parent chain과 과거 integration edge를 서버가 필요할 때 resolve한다. 과거 원문·root 목록 전체를 다음 prompt에 다시 넣지 않는다.
- current decision/fill transaction episode: 이번 turn의 `decision_id`, actual `fill_id`, requested/actual action·quantity·price, allowed/feasible action·constraint-forced flag, pre/post portfolio, source LTB/STB ID/hash, `outcome_pending`을 포함한다. 자유문장 reason/risk_control은 포함하지 않으며 `dim_6`의 mandatory non-evidentiary context다.
- newly visible earlier price outcome: 이전 fill의 `episode_id`, due horizon, reference/mark price, action-aligned markout, 당시 source LTB/STB ID/hash와 승인된 evidence reference만 포함한다. 자유문장 reason/risk_control은 포함하지 않으며 `dim_6`의 eligible outcome evidence다.

서버는 `dimension`, previous/current text hash, STB evidence ID, current transaction-episode ID/hash, earlier price-outcome ID, parent LTB ID와 policy version을 append-only로 기록한다. 같은 turn의 각 dimension current hash는 parent hash와 달라야 하지만, validator는 evidence 없이 이전의 material assertion을 삭제·반전하는 것도 거부한다. 테스트 fixture에는 이전 장기 주장의 exact sentinel span과 새 STB/current transaction/거래 성찰을 함께 넣어, 무근거 소실·무근거 반전·byte-identical copy를 모두 reject한다.

각 manifest decision turn에서 STB가 validated되면 그 STB에 대응하는 LTB updater가 정확히 한 번 validated되어야 한다. 따라서 고정 숫자와 무관하게 `committed_STB_count = committed_LTB_update_count = U`이고 `(agent,date,subturn)` key도 1:1이다. LTB validation이 실패한 turn은 STB·decision·fill·LTB를 단독 commit하지 않고 phase 전체를 rollback/resume한다.

### 6.5 같은 근거의 중복 가중 방지

거래 시점에는 **직전 LTB와 현재 STB를 함께 쓰되, 역할을 섞지 않는다.** STB가 아직 `LTB_t`에 재귀 통합되기 전이므로 이 둘을 함께 주는 것은 중복이 아니다.

- 거래용 long-term block: `LTB_(t-1).dim_1~dim_6`
- 거래용 short-term block: `STB_t.dim_1~dim_6`
- 금지: `LTB_t`를 같은 turn 거래에 사용하거나, raw 뉴스/community를 STB block 외에 다시 거래 prompt에 넣는 것
- 금지: `belief_summary`, `view_change`, LTB change summary를 추가 근거로 전달
- current market facts·portfolio·order/constraint는 belief가 아닌 상태/제약 block으로만 직접 전달
- 동일 기사의 root evidence는 STB와 이후 LTB integration edge를 따라가더라도 독립 support count를 늘리지 않는다. 자신의 post가 Best로 돌아오면 `self_echo=true`; 사회적 독립 support는 0이다.

serializer는 typed evidence ID와 root를 보존하고 trace validator는 동일 root의 독립 support count를 1로 제한한다. market analysis는 과거 root 전체를 직접 받지 않으며, evidence reference로 `previous_ltb_id:dim_i` 또는 현재 STB에 실제 들어간 evidence ID만 출력할 수 있다. community 근거라면 이 ID도 `community_claim_id`이며 post ID를 직접 인용하지 않는다. validator가 LTB reference의 과거 provenance를 parent chain으로 서버 측 확장하며, 모델이 임의의 과거 root/evidence ID를 직접 쓰면 거부한다. frozen-turn STB/LTB placebo·permutation test로 두 block이 각각 실제로 영향을 주는지도 별도 확인한다.

### 6.6 human-log field 계약과 post-writing 예외

`belief_summary`와 `view_change`는 기존 schema·사람의 로그 판독을 위해 LTB row에 남긴다.

- `belief_summary`: current LTB 여섯 차원의 사람이 읽기 쉬운 통합 요약
- `view_change`: `LTB_(t-1)` 대비 `LTB_t` 변화의 사람용 설명

`belief_summary`는 STB·LTB updater, market analysis, decision, posting, community interpretation, 다음 턴 context의 agent-visible 입력에서 제외한다. `view_change`도 STB·LTB updater, market analysis, decision, community interpretation, 다음 턴 context에서는 제외하지만, **post-writing private context에서만** 새 `LTB_t`와 current `fill_t`에 붙여 허용한다. 이 예외는 `view_change = render_change(parent/new dimension hashes, integration evidence)`라는 결정론적 renderer와 source hash로 검증한다. summary만 변조해도 모든 downstream request hash가 같아야 하며, view_change만 변조해도 posting 이외 request hash는 같아야 한다. 반대로 `dim_3` 하나가 바뀌면 scientific digest와 해당 post context hash가 달라져야 한다. 현재 코드의 summary-only previous-belief 조회와 자유문장 posting 경로는 이 계약을 위반하므로 P0 수정 대상이다.

hash namespace도 분리한다. `ltb_scientific_hash`는 applied `dim_1~dim_6`, parent LTB ID, source STB ID, current decision/fill episode hash, newly visible earlier price-outcome IDs, dimension별 integration evidence와 policy version만 포함하고 LTB ID·parent chain·cache key·scientific digest는 이 hash를 사용한다. `human_log_hash`는 별도이며 scientific parent/cache/digest에 절대 들어가지 않는다. `belief_summary = render_summary(applied six dimensions)`, `view_change = render_change(before/after dimension hashes, integration evidence)`인 순수 결정론 함수로 만들고 renderer code hash와 golden-byte fixture를 고정한다. summary/change 변조가 `ltb_scientific_hash`를 바꾸면 실패한다.

---

## 7. Memory가 개입하는 하루의 정확한 순서

### 7.1 global turn 정의

실행기가 고정 상수 `90`을 갖지 않도록 다음 값을 study manifest에서 계산한다.

```text
D = approved trading-date manifest의 날짜 수
Q_d = 거래일 d에 활성화된 ordered decision subturn 목록
U = Σ_d |Q_d| = agent별 expected decision-turn 수
N = frozen agent 수
A = condition/arm 수

committed_STB_count(agent, arm) = U
committed_LTB_update_count(agent, arm) = U
committed_LTB_state_count(agent, arm) = U + 1  # LTB_0 포함
```

현재 study spec은 모든 승인 거래일에 `Q_d=[AM, PM]`이므로 `U=2D`이고, 현재 45일 manifest에서는 `U=90`으로 **계산된다**. 실험을 확장·축소하면 date/subturn manifest와 hash를 새 version으로 승인하고 동일 코드가 `U`를 다시 계산한다. 코드·schema·validator·예산 산식에 90을 literal로 넣지 않는다.

현재 schedule에서는 결과적으로 AM/PM이 교대로 배치되지만, **현재 구현에서도 `2d-1/2d` shortcut을 사용하지 않는다.** resolver가 만든 ordered `(date, decision_event_id)` map의 ordinal을 유일한 global turn으로 사용한다. 그래야 기간 앞부분 추가·삭제, AM-only pilot, 일부 subturn이 없는 후속 설계에서도 scheduler·seed·checkpoint·validator가 같은 event identity를 유지한다.

각 global turn은 `current external evidence → STB → previous LTB + STB Decision-Making → decision_t → committed fill_t → post-fill recursive LTB`의 단방향 DAG다. 같은 턴 fill은 생성 전 STB·analysis·decision으로 되돌아갈 수 없지만, **post-fill LTB의 transaction episode 입력으로는 정확히 한 번 들어간다.** `LTB_t`는 다음 decision event에만 보인다.

### 7.2 AM phase

```text
1. 이전 checkpoint와 exact cohort completeness(`N`; 현재 본 실험은 100)·run-bundle hash 확인
2. 직전 거래일 15:30 초과~당일 08:59 이하의 provenance-safe 실제 뉴스 window 고정
3. 당일 실제 시가와 AM 허용 market feature를 field-level as-of 검사 후 고정
4. 직전 PM fill의 next-turn mark와 due H1/H5 outcome을 deterministic하게 생성하고 이 first-visible/maturity turn에서만 **LTB earlier price-outcome packet**으로 소비 예약
5. 전날 post-PM의 candidate/selected/Best schedule을 agent별로 resolve하되, 아직 exposure·STB row를 canonical DB에 쓰지 않음
6. 실제 payload가 있는 agent만 raw title/body를 **해당 다음-AM community-interpretation request에 한 번** 넣고 validated interpretation을 staging. scheduled Best에는 새 reader-owned exposure를 만들고, PM에 이미 읽은 candidate/selected 글에는 기존 exposure ID의 두 번째 prompt-consumption edge만 추가; no-exposure agent는 call과 exposure 모두 0
7. 대상 agent의 community interpretation call을 모두 검증하는 barrier. raw body sentinel이 STB request에는 0건인지 확인
8. 100명 각자의 current external evidence packet과 별도의 eligible earlier price-outcome packet을 고정. community 항목은 current external packet에만 validated server-owned `community_claim_id`·claim text/stance와 claim→source-exposures→distinct-roots lineage ID를 넣고 raw title/body는 제외
9. current evidence만으로 STB_t.dim_1~dim_6 생성; exact-cohort STB barrier
10. `LTB_(t-1)` long-term block + `STB_t` short-term block + 현재 market/portfolio/constraint block으로 market analysis 생성. raw news/community는 이 단계에 재삽입하지 않음
11. 제약 전 directional_stance를 기록한 뒤 allowed_actions·portfolio constraint를 포함해 BUY/SELL와 정수 수량 생성
12. decision_t의 제출 수량을 실제 시가에 체결하고, actual side/quantity/price·fee=0·pre/post cash·holdings를 가진 committed `fill_t`와 post-portfolio를 deterministic staging
13. `LTB_(t-1).dim_1~dim_6 + STB_t.dim_1~dim_6 + step 11의 structured decision trace + step 12의 committed fill_t transaction-fact packet + step 8의 eligible earlier price-outcome packet`으로 **다음 turn용** `LTB_t`를 새로 작성; exact-cohort LTB barrier. current fill의 outcome은 아직 `outcome_pending`이며 next-turn/H1/H5 미래값은 input 0
14. §16.3/manifest의 canonical scope대로 exposure·interpretation·community claim·claim-source junction·evidence edge·STB·analysis·stance·decision·order·fill·portfolio·ledger·LTB·trace·phase-call-consumption staging이 exact cohort `N`명 모두 유효할 때 한 scientific transaction으로 commit
15. committed state의 AM checkpoint·digest 확정
```

AM 거래에는 `LTB_(t-1)`와 `STB_t`가 함께 쓰인다. AM 뒤 생성한 `LTB_t`는 같은 AM 거래에는 돌아가지 않고 PM 거래의 long-term block이 된다. **AM fill fact는 AM의 post-fill `LTB_t`에 즉시 들어가지만**, 그 체결이 유리했는지/불리했는지의 PM close markout은 같은 날 PM의 price-outcome packet에서 처음 관찰 가능하다.

### 7.3 PM phase

```text
1. 당일 08:59 초과~15:30 이하의 provenance-safe 실제 뉴스 window 고정
2. 실제 종가와 PM 허용 market feature를 field-level as-of 검사 후 고정
3. 같은 날 AM fill의 PM-close next-turn mark와 due H1/H5 outcome을 생성해 eligible **earlier price-outcome** packet으로 고정. AM fill transaction fact 자체는 이미 AM post-fill LTB에 소비됐으므로 다시 넣지 않음
4. post-PM community가 아직 존재하지 않음을 검증
5. current PM external evidence만으로 STB_t 생성; exact-cohort STB barrier
6. `LTB_(t-1)` long-term block + `STB_t` short-term block + 현재 market/portfolio/constraint block으로 analysis·stance·BUY/SELL·수량 생성. raw news/community 재삽입 0
7. decision_t의 제출 수량을 실제 종가에 체결하고, actual side/quantity/price·fee=0·pre/post cash·holdings를 가진 committed `fill_t`와 post-portfolio를 staging
8. `LTB_(t-1) + STB_t + step 6의 structured decision trace + step 7의 committed fill_t transaction-fact packet + step 3의 eligible earlier price-outcome packet`으로 다음 AM용 LTB_t를 새로 작성; current PM fill은 transaction fact로 포함하되 outcome은 `outcome_pending`
9. exact cohort `N`명 전체 scientific state와 ledger를 한 transaction으로 commit
10. committed PM fill_t가 PM post-fill LTB_t에는 transaction fact로 한 번, 다음 거래일 AM의 price-outcome packet에는 next-turn mark가 도래한 뒤 한 번 보이는지 검증하고 PM checkpoint·digest 확정
```

당일 post-PM community는 아직 존재하지 않으므로 PM belief packet에 들어갈 수 없다.

### 7.4 Community phase

Community ON:

```text
PM 체결·commit 완료
→ manifest permission map의 active agent가 posting 여부와 게시물 생성(현재 resolved 70명)
→ 게시물 exact-schema·length·untrusted-data validation
→ 모든 게시물을 agent_id/post_id 순으로 batch commit
→ candidate board snapshot 고정
→ 자기 글 제외 후 depth별 manifest read cap까지 실제 read(현재 Depth 1=5, Depth 2=10)
→ reaction batch commit
→ score DESC, like DESC, post_id ASC로 Best 5 확정
→ Best 5 ID를 같은 condition의 원문 title/content와 deterministic join
→ title/body hash가 일치하는 frozen payload 생성
→ Depth 1/2에는 실제 read + non-empty Best broadcast schedule 저장; empty면 명시적 empty envelope
→ Depth 0에는 posts_read=[] + non-empty Best-only broadcast schedule 저장; empty면 명시적 empty envelope
→ non-empty Best일 때만 manifest Best-audience 모두의 next-AM broadcast 예약 ID 저장(현재 100명); 마지막 날의 non-empty Best는 `right_censored`, empty Best는 `empty`
→ community checkpoint
```

이 시점에는 Best payload가 **예약**됐을 뿐 실제 Best exposure가 아니다. 다음 AM community interpretation call의 typed community field에 title과 full original body가 삽입된 순간 reader-owned `agent_exposure`를 생성한다. 반면 Depth 1·2의 선택 글은 community phase의 react prompt에서 이미 원문을 보았으므로 그 PM에 read exposure를 기록하되, 거래에 사용할 수 있는 visibility만 다음 AM으로 둔다. 선택 prompt에서 실제 표시된 candidate title/type/score도 `title_only_candidate` exposure로 PM에 기록하고 다음 AM interpretation에 같은 title-only payload로 포함한다. 선택되지 않은 글의 본문은 넣지 않는다. 같은 글을 선택 열람 후 Best로 다시 받으면 exposure는 두 개지만 root support는 하나다.

따라서 raw-body channel은 둘로 구분한다.

- `selected_body`: Depth 1·2가 PM community selected-read/reaction call에서 실제 소비하고, 같은 reader-owned exposure를 다음 AM interpretation call이 다시 소비한다. 두 번의 prompt consumption은 남기되 독립 root support는 하나다.
- `best_only_body`: 전날 선택하지 않았고 Best broadcast로만 예약된 글은 다음 AM interpretation call에서 처음 소비한다. 같은 PM trading stage나 community selected-read/reaction call에는 들어가지 않는다.
- `selected_and_best_overlap`: 같은 `(reader_agent_id, root_post_id, exposure_level, content_hash)` full-body payload가 selected이면서 Best이면 next-AM raw title/body는 **한 번만 직렬화**한다. 그러나 selected exposure와 Best-broadcast exposure 두 ID와 `overlap_deduplicated=true`는 모두 보존한다. selected exposure에는 PM selected-read/reaction + next-AM interpretation 두 consumption edge, Best exposure에는 next-AM interpretation 한 edge가 있어 총 3개다. interpretation claim은 `source_exposure_ids=[selected_exposure_id,best_exposure_id]`를 가질 수 있고, 서버는 distinct root를 하나로 dedup해 support count 1로 제한한다.

어느 channel의 raw title/body도 STB request나 LTB updater가 받는 registry view에는 들어가지 않는다. 그 두 단계에는 server-validated derived claim의 ID/type/hash/polarity와 lineage ID만 들어간다.

Community OFF:

```text
PM 체결·commit 완료
→ posting/read/reaction/Best/broadcast 0건 검증
→ agent-visible arm-label 문구 없는 no-op marker
→ empty community-input audit snapshot; `agent_exposure` row는 만들지 않음
→ community checkpoint
```

두 조건 모두 같은 phase state machine을 가져야 resume logic과 day alignment가 달라지지 않는다.

### 7.5 시점 가시성 규칙

| 항목 | 생성/관찰 시점 | earliest belief/행동 visibility |
|---|---|---|
| 현재 AM 뉴스·validated community claim | AM cutoff | 같은 AM STB |
| 현재 PM 뉴스·validated community claim | PM cutoff | 같은 PM STB |
| 현재 AM 시가·market facts | AM cutoff | 같은 AM analysis/decision의 direct execution-state block |
| 현재 PM 종가·market facts | PM cutoff | 같은 PM analysis/decision의 direct execution-state block |
| `STB_t` | current external evidence 해석 뒤 | 같은 턴 analysis/decision short-term block과 거래 뒤 LTB updater |
| `LTB_t` | current turn 거래/체결 staging 뒤 | **다음** decision event의 long-term block; 같은 turn analysis/decision에는 불가시 |
| AM actual fill | AM decision 뒤 | 같은 AM의 **post-fill LTB transaction episode**; PM close markout은 나중 PM price-outcome packet |
| PM actual fill | PM decision 뒤 | 같은 PM의 **post-fill LTB transaction episode**; next-AM markout은 나중 AM price-outcome packet |
| post-PM Best 5 | PM commit 뒤 schedule; 다음 AM interpretation request에서 actual exposure | 100명의 validated interpretation만 같은 AM STB; raw body는 STB에 없음 |
| Depth 1·2가 선택해 읽은 일반 글 | PM community phase exposure; 다음 AM interpretation에서 재소비 | 해당 agent의 실제 노출·출처가 연결된 interpretation만 다음 AM STB |
| next-turn mark | 다음 reference price가 관찰될 때 | 그 event의 LTB earlier price-outcome packet |
| H1/H5 outcome | 정한 horizon price가 관찰될 때 | 그 event의 LTB earlier price-outcome packet |

`LTB_t`는 turn-level `visible_from_turn=t+1`을 명시적으로 가져야 한다. 같은 turn의 analysis/decision은 parent `LTB_(t-1)`와 `STB_t`만 보며, current fill은 그 뒤의 **같은 turn post-fill LTB updater**에만 structured transaction episode로 한 번 들어간다. 가격 성과는 다음 eligible event의 price-outcome packet에서만 보인다. DB와 trace에는 `created_stage`, `visible_from_stage`, `visible_from_turn`을 구분한다.

---

## 8. 거래 규칙과 기존 `dim_6`의 outcome feedback

### 8.1 확정 거래 규칙

사용자와 원 설계가 확정한 거래 메커니즘은 다음과 같다.

```text
AM: 해당 거래일 실제 시가 제시
PM: 해당 거래일 실제 종가 제시
→ LLM이 allowed_actions 안에서 BUY 또는 SELL과 정수 수량 선택
→ 현금·보유수량·1회 매수 한도 검사
→ 정상 주문은 그 공시가격에 제출 수량 전량 체결
```

- action space는 `buy_sell_only`; **HOLD는 의도적으로 없다.** 참여 여부가 아니라 뉴스·belief에 대한 강제 방향 반응을 관찰하는 실험이기 때문이다.
- 최대 매수 gross notional은 현재 현금의 50%를 넘지 않는다. 이번 baseline은 fee가 0이므로 최대 매수 수량은 `floor((현재 현금 × 0.5) ÷ 공시가격)`이고, cash debit은 정확히 gross notional이다.
- 최대 매도 수량은 현재 보유수량이고 공매도는 없다.
- 수량은 최소 1주의 정수다.
- allowed action이 하나뿐이면 방향은 제약이 강제한 것이므로 `constraint_forced=true`다.
- 둘 다 불가능하면 임의 HOLD나 0주 주문을 만들지 않고 phase를 pause한다.
- 현재 exchange 구현은 slippage·partial fill·market impact 없이 announced price에 full fill하며 fill fee를 `0.0`으로 기록한다. 이는 **이번 fee-free baseline에는 맞는 동작**이다. 다만 config의 기존 `0.0005`, legacy `trade_log`/CSV와 현재 RN의 manifest·prompt·portfolio·PnL·`paper_fill_ledger`/canonical CSV export를 모두 `commission_rate=0.0`, `sell_tax_rate=0.0`, `fee_amount=0`으로 명시적으로 맞추고 validator가 일치 여부를 검사해야 한다.

cash-only initial portfolio 때문에 turn 1 AM에는 SELL이 불가능하고 전원 BUY가 기계적으로 강제된다. 이 규칙은 원 설계를 보존하되 Day 1, one-direction-feasible turn, constraint-neutral sensitivity를 별도로 보고한다. HOLD를 추가해 이 문제를 사후 완화하지 않는다.

### 8.2 즉시 fill fact와 사후 outcome feedback을 분리한다

체결 직후 알 수 있는 것은 action·수량·공시가격·현금·보유수량뿐이다. 그 거래가 좋았는지는 미래 reference price가 관찰되기 전에는 알 수 없다.

- **Immediate execution fact**: fill 직후 ledger에 저장하고, **같은 turn의 post-fill LTB updater**가 structured decision/fill episode로 한 번 해석한다. 이후 도래하는 next-turn/H1/H5 관찰은 같은 fill episode의 별도 maturity record로 각각 한 번씩 추가한다.
- **Next-turn mark**: 다음 실제 decision price가 관찰되는 즉시 계산하는 잠정적 timing signal
- **Matured outcome**: H1/H5가 실제로 관찰된 뒤 계산하는 확인·수정 근거

current fill을 STB·analysis·decision에 넣으면 “거래 전에 자기 체결을 본” 시간 역전이므로 금지한다. 반대로 exchange가 actual fill과 post-portfolio를 확정한 **뒤**의 LTB updater가 그것을 transaction episode로 해석하는 것은 시간 역전이 아니며 필수 단계다. 이 단계에는 미래 price outcome을 넣지 않는다.

### 8.3 fill episode

각 agent-turn마다 다음을 append-only 저장한다.

- source `LTB_(t-1)` ID/hash, source STB ID/hash, analysis ID, decision ID
- requested action/quantity와 allowed actions·max quantity
- actual fill action/quantity/price/status
- pre/post cash·position·total value
- `constraint_forced`, one-direction-feasible, first-turn-mechanical flag
- structured decision reason code와 risk-control code
- 사람용 자유문장 reason/risk_control은 별도 audit log에만 저장

현재 실행은 공시가격 체결이므로 “비싸게 샀다”는 **주문 가격 미끄러짐**이 아니다. 이후 가격과 비교해 AM/PM 타이밍 또는 방향·수량이 불리했다는 뜻으로만 평가할 수 있다.

### 8.4 next-turn mark와 H1/H5

빠른 잠정 outcome과 비교 가능한 성숙 outcome을 분리한다.

- AM fill의 next-turn mark: 같은 날 PM 실제 종가 대비
- PM fill의 next-turn mark: 다음 거래일 AM 실제 시가 대비
- H1: 같은 subturn의 다음 거래일 reference price
- H5: 같은 subturn의 5거래일 뒤 reference price

각 event는 deterministic하게 다음을 계산한다.

- gross timing markout: `future reference price / fill price - 1` (이번 baseline은 `fee_amount=0`이므로 fee-adjusted markout을 별도로 만들지 않음)
- action-aligned move: BUY는 market move, SELL은 부호 반전
- fill 당시 feasible set과 selected-quantity ratio
- outcome status: tentative/confirmed/contradicted/right-censored

next-turn mark는 AM과 PM의 시간간격이 달라 직접 성능비교의 primary가 될 수 없고, 빠른 학습용 잠정 evidence다. H1/H5는 동일 subturn horizon의 확인 근거다. 같은 fill의 H1·H5를 독립 거래 두 건처럼 세지 않는다. 전체 portfolio PnL을 한 fill에 귀속하지 않는다.

### 8.5 LTB dim_6에 녹이는 방식

STB에는 fill fact·markout·matured outcome을 넣지 않는다. 해당 event의 거래가 끝난 뒤 LTB updater는 **(a) current decision/fill transaction episode**와 **(b) 이미 도래한 price-outcome packet**을 구분해 받는다. (a)는 actual side/quantity/price·pre/post portfolio·판단 trace·`outcome_pending`을 즉시 반영하고, (b)는 next-turn/H1/H5가 관측된 뒤에만 성공/실패 성찰로 반영한다. 두 packet을 재귀적으로 해석해 `LTB_t.dim_6`의 누적 자기평가를 갱신하며, 단순 log append는 금지한다. STB `dim_6`은 이번 뉴스·community 신호가 현재 확신·위험·규율에 주는 즉시적 함의이고, LTB `dim_6`은 실제 거래와 그 결과에서 이어지는 방향·수량·위험 관리의 장기 교훈이다.

여기에는 FinMem/TradingGPT식 별도 reflection index, 주간 회고 prompt, top-K 거래 검색, 수익기반 memory 승격을 두지 않는다. 현재 typed packet에 포함된 새 outcome만 LTB update prompt가 자기평가 근거로 해석하고, 그 결과를 다른 차원과 동일한 FUSE식 재귀 통합에 태운다.

예를 들어 AM BUY 뒤 PM 종가가 하락했다면 다음처럼 처리한다.

```text
PM STB.dim_6:
  "이번 PM 정보가 현재 보유·위험 노출에 주는 즉시적 경고 또는 완화 신호를 해석한다. 과거 AM 체결 성과는 넣지 않는다."

PM LTB.dim_6:
  "오늘 AM BUY는 PM 종가 기준 불리한 잠정 mark였다. 기존 거래 성찰을 유지하면서도,
   유사 신호에서 방향·수량·위험 관리를 어떻게 조정할지 이번 turn의 새 장기기억으로 작성한다."
```

HOLD가 없으므로 갱신 가능한 자기평가는 “거래하지 말자”가 아니라 **유사 조건에서 BUY/SELL 방향 또는 수량을 어떻게 조절할지**다. 실제 개인 수급 label은 이 feedback에 절대 넣지 않는다. 가격에 유리한 거래와 개인 수급 방향을 잘 따라간 거래는 다른 목표이므로, price markout은 mechanism이고 retail-flow alignment는 외부 evaluator outcome이다.

### 8.6 censoring

실험 종료 시 아직 관찰되지 않은 next-turn/H1/H5는 `right_censored`다. 연구기간 밖 가격을 가져와 실행 중 STB/LTB를 갱신하지 않는다. 사후 연구용 분석을 하더라도 agent-visible memory와 분리한다.

---

## 9. 뉴스가 STB/LTB에 들어가는 방식

### 9.1 실제 노출만 사용

- 전체 후보 pool이 아니라 agent에게 실제 노출된 기사만 STB 후보가 된다.
- Depth 0/1/2별 headline/body/search exposure 권한을 정확히 기록한다.
- `selected_news`는 본문을 보기 전 선택이 아니라, 현재 구현처럼 본 뒤 영향 기사로 해석한다.
- 같은 기사 재노출과 같은 사건의 서로 다른 기사를 `source_id`, `root_event_id`, `content_hash`로 구분한다.
- 같은 root의 복제본은 독립 사실 support를 늘리지 않지만 exposure count는 보존한다.

current article은 그 턴 STB에서 해석되고, 거래 뒤 작성된 `LTB_t`에 재귀 통합되어 다음 event에 이어진다. 다음 event에는 raw 기사를 무조건 다시 삽입하지 않고 LTB parent chain과 새 STB가 역할을 나눈다. 새 기사나 correction은 새 STB evidence와 parent/current hash·integration edge로 lineage를 남긴다.

### 9.2 기사 version과 as-of provenance가 없으면 사용하지 않는다

현재 `processed_news.csv`에는 오전 timestamp인데 summary가 같은 날 마감값·고가·최종 투자자 수급을 서술하는 반례가 이미 존재한다. 예를 들어 2026-04-27 09:11 기사 summary에는 같은 날 최종 고가와 개인/외국인/기관 수급이 들어 있다. 이는 사후 수정 기사 또는 요약 생성 오류일 수 있으며, 단순 publication time 필터만으로 막을 수 없다.

필수 provenance:

- URL·source
- `published_at`, `observed_at/scraped_at`, `last_modified_at`
- cutoff 시점 archived raw-body version과 hash
- summary model·prompt·request·output hash와 생성시각
- mask/reject/allow 판정과 reviewer ID

version timestamp가 없는데 cutoff 이후에만 알 수 있는 “마감”, 당일 종가·고가·최종 수급을 포함하면 quarantine한다. AM·PM title/body/summary와 Depth 2 search 결과 전체에 semantic scan을 적용한다. 당일 삼성전자 개인 순수급 방향·금액은 primary runtime에서 reject/mask하고, target-adjacent 코스피·테마 개인수급은 blinded reviewer가 실제 target sign을 보지 않은 상태에서 사전 판정한다.

### 9.3 preprocessing reasoning 범위

이번 study의 strict reasoning-off 범위는 **실행 중 발생하는 모든 LLM physical HTTP request**다. 기존 frozen news summary는 새 API 호출로 재생성하지 않으며, 그 전처리 모델·reasoning 상태의 불명확성은 provenance limitation으로 `RUN_RECORD.md`와 논문에 공개한다. 향후 전처리를 재생성하는 경우에는 같은 strict-off request·provider pin·request hash audit을 적용하고, 새 news bundle version으로 양 arm을 처음부터 재실행한다.

---

## 10. Community가 STB/LTB에 들어가는 방식

### 10.1 shared board, private exposure belief

게시판은 공용이지만 STB/LTB는 agent별이다. 기억 가능한 것은 그 agent가 실제로 본 정보뿐이다.

- candidate metadata 노출
- 선택해 실제 읽은 title/body
- reaction
- Best 5의 title/full original body next-AM broadcast
- 해당 exposure에 대한 current STB 해석

다른 agent의 private belief/LTB, portfolio, 자유문장 action reason, 읽지 않은 일반 글 본문은 독자의 memory가 될 수 없다. Depth 2 author profile은 사전등록한 공개 숫자·방향·badge allowlist만 허용한다.

### 10.2 Best 5 원문 경로

`mark_best_posts()`의 현재 반환값인 `post_id·title·post_type·score`만 prompt에 넣어서는 본문을 본 것이 아니다. agent가 ID를 보고 스스로 DB에서 원문을 찾아오는 기능도 없다. 따라서 시스템이 다음을 수행한다.

```text
Best ranking 결과 post_id
→ 같은 run/condition의 frozen community_posts와 deterministic join
→ title + full original body + post_type + score + content_hash
→ 100명 각자의 next-AM community-interpretation typed exposure field에 직접 삽입
```

- Depth 0 30명: Best 5만 보고 글쓰기·선택 열람·reaction 없음
- Depth 1/2 70명: 선택 열람한 일반 글과 Best 5를 봄
- Best 5 audience: 100명
- 같은 PM 거래에는 보이지 않고 다음 거래일 AM STB에 처음 들어감
- 마지막 거래일 PM은 연구기간 안의 next-AM Best broadcast/exposure가 0이다. non-empty Best schedule은 `right_censored`, Best가 없으면 status는 `empty`다. 같은 PM의 D1/D2 선택 열람 exposure는 존재할 수 있다.

현재 선택 글 본문 200자 절단은 full-body exposure 요구와 모순이다. 중간 silent truncation을 없애고 게시글 생성·DB 저장 시점에 사전 고정한 전체 body cap을 적용한다. cap 안의 body는 전부 전달하고 hash를 검증한다.

### 10.3 기존 `community_thinking`의 역할을 하나로 고정

최소 변경을 위해 기존 `community_thinking` 호출은 유지하되, 이를 memory나 자유형 회고문이 아니라 **다음-AM community interpretation 전처리**로만 정의한다.

- `RN_COMM_ON`에서 실제 community exposure가 한 건 이상인 agent에게만 다음 AM 한 번 호출한다. `RN_COMM_OFF`와 no-exposure agent에는 호출하지 않는다.
- 입력은 그 agent에게 실제 표시된 candidate title/type/score, 선택 글 title/full body, Best 5 title/full body, 허용된 public author profile만 포함한다. 선택하지 않은 일반 글의 body는 금지한다.
- frozen persona를 같은 정보를 다르게 해석하는 렌즈로 넣되, previous STB/LTB·summary/change·private belief는 넣지 않는다.
- 모델 출력은 `observed_sentiment`, `claims`, `agreement_disagreement`, `uncertainty`의 짧은 exact-schema `community_interpretation`이다. 모델이 낸 `claims[]` 각 원소는 `claim_text`, 입력 allowlist에서 고른 non-empty `source_exposure_ids`, 실제 표시 문자열의 exact substring인 `supporting_quote`, 삼성전자 방향에 대한 `claim_stance=bullish|bearish|neutral|uncertain`을 함께 가져야 한다. 서버가 검증 뒤 namespaced `community_claim_id`를 부여하고 `community_claim_sources(claim_id, exposure_id)` junction을 만든 뒤 source exposures에서 각 `source_exposure_level`과 distinct `root_post_id` set을 resolve한다. 모델은 claim/root ID·exposure level을 만들지 못하고 입력에 없는 exposure ID를 넣을 수 없다. 숨은 사고과정이나 250~500자 자유형 reasoning을 요구하지 않는다.
- `source_exposure_ids`와 `supporting_quote`는 실제로 해당 reader에게 표시된 글을 연결하는 provenance·visibility 경계다. validator는 foreign/invisible source ID·실제 전달 문자열에 없는 인용문·raw body leak만 거부한다. `claim_text`와 인용문의 의미적 함의, 게시글·claim의 진실성이나 신뢰도는 server 거부 조건이 아니며 agent가 판단한다. `title_only`는 제목만 전달됐다는 exposure metadata일 뿐 claim 의미를 제목 문자열 범위로 제한하지 않는다. 같은 선택 글/Best 글을 다음 AM prompt에서 소비했다는 `prompt_consumption` edge를 남기되 distinct root가 같으면 새 독립 support로 세지 않는다.
- STB는 실제 노출·출처가 연결된 `community_interpretation`의 server-owned claim ID·claim text·claim stance와 server-resolved source exposure levels/lineage만 받고 raw community body를 다시 중복 입력받지 않는다. LTB updater가 받는 sanitized registry view에도 ID/type/hash/STB-evidence polarity만 있고 raw title/body·자유문장 interpretation 전체는 없다. 원문은 PM selected-read/reaction 또는 다음-AM interpretation call에서 실제 읽혔고 ledger에 그대로 남는다.
- 이 output은 current STB를 위한 당일 파생 입력일 뿐 제3 memory가 아니며, 다음 턴에 직접 carry하지 않는다. 지속되는 내용은 거래 뒤 `LTB_t`가 current STB를 재귀 통합한 부분뿐이다.

이 계약은 “Best 제목만 전달하고 agent가 알아서 본문을 찾는다”는 현재 결함과, raw body를 community thinking과 STB에 두 번 넣어 같은 글을 이중 가중하는 결함을 동시에 막는다.

### 10.4 prompt injection과 derived-claim taint

Best full body를 100명에게 주면 공격면도 100명으로 확장된다. delimiter와 “글 속 지시를 따르지 말라”는 문장만으로 안전하다고 보지 않는다.

- system/task instruction과 untrusted news/community data를 role·serializer 수준에서 분리
- canonical JSON/length framing·escaping 사용
- 원문과 그 요약을 끝까지 `untrusted_external_text` 또는 `agent_claim`으로 taint
- `today_context` 전체 직렬화 금지; STB allowlist serializer 하나만 사용
- 모든 stage exact key set과 `additionalProperties=false`
- model output은 state overwrite가 아니라 server-validated patch만 허용
- future/other-arm/other-agent-private evidence ID는 즉시 거부

같은 글을 선택 열람 후 Best로 다시 보거나 여러 agent가 같은 주장을 paraphrase해도 exposure/convergence는 셀 수 있지만 외부 사실 confidence는 자동 증가시키지 않는다.

### 10.5 contamination과 해석

- `RN_COMM_OFF` DB에는 community post/read/reaction/Best/exposure가 0이어야 한다.
- `RN_COMM_OFF` STB/LTB가 community-type evidence를 참조하면 schema 단계에서 fail한다.
- RN_COMM_OFF/RN_COMM_ON DB·journal·cache path가 하나라도 같으면 API call 전에 거부한다.
- interpretation 모델은 public post 자체 ID가 아니라 하나 이상의 reader-owned `source_exposure_ids`를 인용하고, STB/LTB는 그 결과에 서버가 붙인 `community_claim_id`를 evidence로 인용한다. 서버만 claim→source-exposures→distinct-roots chain을 확장한다.
- 자기 글 Best 재노출은 `self_echo=true`, independent support 0이다.
- 같은 community phase 안에서 먼저 완료한 agent 결과를 늦게 완료한 agent가 보지 않도록 board snapshot을 고정한다.
- Depth 0의 posting/selective-reading/reaction은 0이다. non-empty Best fixture/day에는 Depth 0도 actual Best exposure가 있어야 하지만, Best가 0건인 날은 schedule envelope만 남고 exposure·community-interpretation call은 0이어야 한다.

`RN_COMM_ON − RN_COMM_OFF`는 community의 다음-AM 직접효과와 LTB에 남는 지속효과를 합친 total effect다. 둘을 분해하려면 별도 memory/community ablation이 필요하며 이번 2-arm에서는 하지 않는다.

---

## 11. STB/LTB prompt와 기존 거래 파이프라인 연결

### 11.1 기존 six dimensions는 그대로다

STB와 LTB는 모두 현재 runtime update prompt의 다음 여섯 의미를 그대로 쓴다. 새로운 “dim 6짜리 vector”나 LTB 전용 의미를 만드는 것이 아니다. 코드 근거는 `prompts/update_belief.txt:51-59`, 저장 필드는 `twinmarket_kr/db/schema.py:35-42`다.

| 차원 | baseline runtime prompt의 정확한 의미 | 현재 설정상 한도 |
|---|---|---:|
| `dim_1` | 향후 약 1개월 삼성전자 주가 방향 전망 | 150자 |
| `dim_2` | 현재 valuation이 싸다/비싸다/적정하다는 관점과 근거 | 100자 |
| `dim_3` | 금리·환율·경기·반도체 업황 등 거시환경 판단 | 100자 |
| `dim_4` | 삼성전자를 둘러싼 시장심리와 투자자 분위기 | 100자 |
| `dim_5` | 오늘 뉴스·community를 접한 해석과 깨달음 | 100자 |
| `dim_6` | 최근 자기 투자 판단의 적절성·반복 오류에 대한 자기평가 | 100자 |

같은 schema를 쓰는 이유는 기존 downstream과 의미 호환성을 지키기 위해서다. STB는 current-only 관점, LTB는 recursive cumulative 관점이라는 **시간 역할만** 다르다. `dim_1`의 약 1개월 전망 horizon도 STB라고 해서 당일 전망으로 줄이지 않는다.

다만 현재 코드에는 이미 두 가지 의미 drift가 있으므로 구현 전에 문구를 통일해야 한다.

- `prompts/initial_belief.txt`는 `dim_1`을 “향후 1개월 시장 방향”으로 적지만 runtime update는 “향후 1개월 삼성전자 주가 방향”이다. 단일종목 실험이므로 후자를 canonical 정의로 고정한다.
- initial `dim_5`는 “뉴스 해석 성향”, runtime `dim_5`는 “오늘 뉴스·community 해석과 깨달음”이다. initial 값은 turn-0 해석 prior이고, turn 1부터는 같은 field의 현재/누적 해석 내용으로 읽는다.
- initial `dim_6`는 “자기 투자 능력 평가”, runtime `dim_6`는 “최근 판단 자기평가”다. turn-0은 사전 자기평가, 이후에는 관찰 가능한 거래 결과가 누적된 자기평가로 해석한다.

이 정리는 field를 새로 바꾸는 것이 아니라, 초기 prompt와 반복 update prompt 사이의 기존 모호성을 제거하는 것이다. 연구 중간에 정의를 다시 바꾸지 못하도록 canonical 6D 정의와 prompt hash를 run manifest에 고정한다.

위 한도는 `config.py:55-62`에 선언돼 있지만 현재 `parse_belief_json()`과 updater validation은 nonempty만 확인하고 실제 길이를 강제하지 않는다. 따라서 현 상태의 “150/100자 이내”는 prompt 요청일 뿐 저장 계약이 아니다. STB/LTB validator와 DB CHECK가 문자·byte·token cap을 실제로 강제해야 한다.

### 11.2 Prompt A 역할 계약: 기존 `update_belief.txt`를 STB 생성기로 축소

아래 bullet은 실제 prompt 문장이 아니라 구현·검증을 위한 **역할과 입출력 계약**이다. 실제 파일은 기존 한국어 persona 문체와 6D 설명을 유지하되, reasoning을 출력하라는 문구 없이 작성한다. 기존 파일을 재사용하거나 이름만 `update_short_term_belief.txt`로 명확히 할 수 있다. 핵심은 입력 serializer를 바꾸는 것이다.

입력:

- run bundle에 hash-pin한 persona의 허용된 고정 속성·전략·위험성향·행동특성
- 현재 cutoff까지 허용된 실제 뉴스의 typed interpretation/evidence ID와, 해당될 때 validated `community_interpretation`의 server-owned claim ID·claim text·claim stance + server-resolved source exposure levels와 claim/source-exposures/distinct-roots lineage ID. raw `today_context` 전체와 raw community title/body는 이 STB input에 절대 넣지 않음
- typed evidence registry의 STB용 sanitized view

금지 입력:

- previous STB/LTB dimensions
- previous/current summary/change
- current market price/features·cash·holding·portfolio snapshot·allowed actions·quantity bounds. 이들은 memory가 아니라 이후 analysis/decision의 direct execution-state block이다. **historical** order/fill·자유문장 reason은 direct block에 넣지 않고, 관찰 가능한 과거 거래 성찰만 LTB `dim_6` reflection path로 들어간다.
- 과거 fill·next-turn mark·matured outcome 및 current-turn fill. STB에는 모두 금지한다. current-turn fill은 exchange 확정 뒤 **같은 turn post-fill LTB**의 structured transaction episode로만 한 번 쓰며, 과거 price outcome은 각 최초 관찰 가능 event의 LTB packet으로만 쓴다.
- retry/generation metadata

출력:

- STB `dim_1~dim_6`
- dimension별 support/contradict evidence IDs
- exact expected keys만 허용

구현용 output contract 예시는 다음과 같다. 이는 실제 prompt 문장이 아니라 JSON Schema가 강제할 key 구조다.

```json
{
  "dim_1": "nonempty current-turn interpretation",
  "dim_2": "nonempty current-turn interpretation",
  "dim_3": "nonempty current-turn interpretation",
  "dim_4": "nonempty current-turn interpretation",
  "dim_5": "nonempty current-turn interpretation",
  "dim_6": "nonempty current-turn interpretation",
  "dimension_evidence": {
    "dim_1": {"support": [], "contradict": []},
    "dim_2": {"support": [], "contradict": []},
    "dim_3": {"support": [], "contradict": []},
    "dim_4": {"support": [], "contradict": []},
    "dim_5": {"support": [], "contradict": []},
    "dim_6": {"support": [], "contradict": []}
  }
}
```

모든 evidence ID는 입력 registry에 실제 존재하고 current turn에 visible해야 한다. community evidence ID는 반드시 해당 reader의 `community_claim_id`이고 server chain이 exposure/root까지 유일하게 resolve되어야 한다. 여기서 STB의 `support`/`contradict`는 **그 evidence가 생성된 `STB.dim_i` 문장을 지지하는지 반박하는지**의 관계이고, 원문의 bullish/bearish `claim_stance`와 다른 축이다. 빈 배열은 허용하지만 빠진 dimension key, summary/change, 임의 extra key는 허용하지 않는다.

해당 dimension에 관련된 새 근거가 없으면 과거 belief를 상상해 채우지 않고 “이번 턴 외부 신호는 이 관점에 대해 중립적이거나 불확실하다”는 current-only 문장과 빈 evidence 배열을 낸다. 이 문장은 장기 belief의 복사 지시가 아니라, 다음 LTB rewrite가 parent의 material assertion을 근거 없이 뒤집지 않도록 하는 현재 신호다. STB `dim_6`은 과거 성과 평가가 아니라 이번 뉴스·community 신호가 현재 확신·위험·규율에 주는 **즉시적 함의**만 적는다.

### 11.3 Prompt B 역할 계약: 새 `update_long_term_belief.txt`

이 절도 실제 prompt 원문이 아니라 역할 계약이다. 실제 prompt의 핵심 질문은 “직전 누적 여섯 차원, 이번 STB, **이번 Decision-Making Process가 만든 실제 체결 episode**, 지금 도래한 과거 가격 성찰을 통합해, 다음 decision event에서 쓸 장기 여섯 차원을 모두 새 버전으로 작성하라”이다. 이는 FUSE-inspired timing adaptation이다. FUSE의 재귀 LTM 골격은 보존하되, 이번 STB를 같은 거래에서 중복 사용하지 않도록 새 LTB의 visibility를 다음 event로 늦춘다.

입력:

- `previous_ltb.dim_1~dim_6`; turn 1에는 `LTB_0`
- `current_stb.dim_1~dim_6`
- **current `decision/fill episode_t`**: `input_ltb_id`, `input_stb_id`, structured decision trace, committed `fill_id`, actual side/quantity/price, fee=0, pre/post cash·holdings, `outcome_pending`. 이번 체결 사실은 현재 post-fill LTB update에 정확히 한 번만 들어가며 STB/analysis/decision에는 들어가지 않음
- `maturity_turn`이 정확히 현재 turn인 **earlier** next-turn/H1/H5 typed price-outcome packet. 각 `(fill_id, horizon)`은 자기 due event에서 한 번만 소비하며 **미래 outcome**은 포함하지 않음
- STB 및 earlier price-outcome packet이 참조한 validated evidence의 **LTB용 sanitized registry view**. ID/type/hash/polarity/visibility와 community claim→source-exposures→distinct-roots lineage만 포함하고 raw source payload·raw title/body·자유문장 interpretation 전체는 제외
- STB와 byte-identical한 frozen persona profile/hash; 해석 일관성 제약일 뿐 evidence는 아님

모델 출력:

- 기존과 같은 top-level `dim_1~dim_6` proposed text
- dimension별 `integration_evidence` support/contradict IDs. `dim_1~dim_5` ID는 current STB evidence의 부분집합이고, `dim_6`만 eligible price-outcome evidence를 추가로 쓸 수 있음. current decision/fill episode는 필수 **non-evidentiary context**로 별도 input lineage에 기록하며 `integration_evidence` ID로 인용하지 않음

구현용 output contract 예시는 다음과 같다. `dim_i`의 문장 의미·한도는 STB와 동일하고, `integration_evidence`가 재귀 통합의 근거를 감사한다.

```json
{
  "dim_1": "proposed cumulative belief",
  "dim_2": "proposed cumulative belief",
  "dim_3": "proposed cumulative belief",
  "dim_4": "proposed cumulative belief",
  "dim_5": "proposed cumulative belief",
  "dim_6": "proposed cumulative belief",
  "integration_evidence": {
    "dim_1": {"support": [], "contradict": []},
    "dim_2": {"support": ["<visible-evidence-id>"], "contradict": []},
    "dim_3": {"support": [], "contradict": []},
    "dim_4": {"support": [], "contradict": []},
    "dim_5": {"support": ["<visible-evidence-id>"], "contradict": []},
    "dim_6": {"support": [], "contradict": []}
  }
}
```

여섯 dimension/evidence key는 모두 required이고 `additionalProperties=false`다. 모델은 action enum, summary, change, ID, turn, hash를 만들지 않는다.

서버 출력:

- exact six-dimension schema·evidence visibility·parent/eligible-reflection 규칙 검증
- 여섯 dimension 모두 nonempty이고 parent dimension text와 content hash가 달라야 함. byte-identical copy 또는 빈 dimension은 reject
- 승인된 proposed text를 사용하고 before/after hash·parent ID·STB ID·current transaction-episode hash·earlier price-outcome packet hash·integration-evidence edge 기록
- historical provenance는 previous-LTB parent chain으로 resolve하고 과거 root 목록을 LLM output에 복제하지 않음
- final server-applied six dimensions와 parent/current hash 및 integration evidence에서 `belief_summary`·`view_change`를 deterministic human-log renderer로 만든 뒤 기존 `today_belief` 호환 dict를 조립. 모델의 proposed summary/change는 받거나 저장하지 않음

규칙:

- 같은 root evidence를 STB와 previous LTB의 두 독립 support로 세지 않음
- `dim_i`는 `previous_ltb.dim_i`와 `current_stb.dim_i`만 같은 축에서 통합한다. 다른 차원의 text를 대체 원문처럼 이동시키지 않는다.
- current STB·eligible reflection과 previous LTB가 충돌하면 evidence와 불확실성을 보존하며 해당 dimension을 새 문장으로 조건부 재서술한다. 근거 없는 material deletion/reversal은 reject한다.
- STB evidence와 trade-reflection evidence가 모두 비어도 모든 dimension을 새 문장으로 다시 쓴다. 이 경우 parent의 material assertion을 보존하고 현재 신호의 중립성/불확실성 및, `dim_6`에서는 current transaction episode의 outcome-pending 상태를 명시한다.
- `integration_evidence[dim_i]`는 같은 dimension의 STB evidence 또는 `dim_6`에서 허용된 eligible price-outcome evidence의 부분집합이어야 하며, 차원·극성 간 이동을 reject한다. current decision/fill episode는 `dim_6`의 **의무 context**이지만 독립 support/contradict 수로 세지 않는다. price reflection이 `dim_1~dim_5`의 thesis를 자동으로 뒤집거나 PnL만으로 방향 판단을 바꾸면 reject한다.
- raw news/community, 미래 outcome ID, 자유문장 prior reason 참조 금지. current fill ID는 `integration_evidence`에 인용하지 않고 서버 제공 `transaction_episode` field 안에서만 사용
- persona field의 생성·수정·evidence 인용 금지; persona hash가 run bundle과 다르면 호출 전 pause

별도의 세 번째 fused-belief/opinion 호출은 없다. `LTB_t`는 current decision input이 아니라 다음 event의 long-term block이며, current fill은 exchange 확정 뒤 `LTB_t`의 **단 한 번의 transaction-episode input**으로만 쓰인다. renderer output이 final applied six dimensions·parent/current hash·integration evidence와 불일치하면 commit 전에 실패한다.

실제 두 prompt가 달라지는 지점은 내용 schema가 아니라 시간 역할이다.

| prompt | 실제로 묻는 역할 | prior 6D 입력 | current evidence 입력 | 출력 6D 의미 |
|---|---|---:|---:|---|
| STB | “현재 턴에 새로 관찰한 정보가 각 기존 관점에 무엇을 시사하는가?” | 없음 | 있음 | current-turn interpretation |
| LTB | “직전 누적 관점, 현재 해석, 이번 actual decision/fill episode, 새로 관찰 가능한 과거 가격 성찰을 통합하면 다음 event의 누적 관점은 무엇인가?” | 있음 | STB·current transaction episode·eligible price outcome | next-event recursive cumulative belief |

최종 prompt 원문은 구현 단계에서 이 계약을 그대로 반영해 별도 파일로 작성하고, 2-agent offline snapshot에서 금지 입력·출력 schema를 먼저 검증한 뒤 hash-pin한다. 이 문서의 간단한 질문 문구를 그대로 복사해 본 실험 prompt로 사용하지 않는다.

### 11.4 기존 market analysis·decision의 정확한 관계

현재 코드의 실제 경로를 유지한다.

```text
LTB_(t-1)(dim_1~dim_6)
+ STB_t(dim_1~dim_6)
+ current market features
+ portfolio summary
→ market analysis + directional_stance

LTB_(t-1)(dim_1~dim_6)
+ STB_t(dim_1~dim_6)
+ market analysis
+ portfolio summary
+ trading constraints
→ BUY 또는 SELL + 수량
```

따라서 “거래가 belief만으로 이루어진다”고 쓰지 않는다. 대신 **모든 거래가 previous LTB와 current STB를 분리된 두 belief block으로 반드시 사용한다**고 쓴다. 현재 시장가격·portfolio·현금·보유수량·허용 action은 의사결정과 실행에 계속 직접 필요하지만, arithmetic feasibility/execution-state block으로만 제공한다.

현재 뉴스·community interpretation의 raw/direct path는 base에서 제거한다. directional external information은 STB를 거쳐서만 analysis/decision에 들어가며, raw 뉴스·raw community body·자유문장 interpretation은 downstream serializer의 allowlist 밖이다. 이로써 같은 current signal을 STB와 별도 direct path에서 이중 계상하지 않는다.

### 11.5 모든 downstream serializer의 allowlist

- analysis/decision: `previous_ltb.dim_1~dim_6`와 `current_stb.dim_1~dim_6`를 이름이 있는 별도 belief block으로 포함
- post-PM community posting은 새 `LTB_t.dim_1~dim_6` + deterministic `view_change` + 이미 commit된 PM의 structured current `fill_t`만 private input으로 쓸 수 있음. `belief_summary`는 항상 제외하며, current fill은 STB/analysis/decision/community interpretation에는 금지되고 **post-fill LTB와 post-writing private context에만** 허용된다. `view_change`는 STB/LTB/analysis/decision/community interpretation에 금지된다.
- raw 뉴스/community와 raw interpretation은 STB 밖에서 downstream에 재주입하지 않음
- `belief_summary`는 모든 agent-visible path에서 제외. `view_change`는 위 post-writing private context 외에는 제외
- same-turn direct execution-state에는 현재 cash·holding·allowed actions·quantity bounds·announced price만 포함; historical order/fill과 자유문장 reason은 제외
- 모든 LLM output은 exact key set, unknown key rejection, `additionalProperties=false`
- retry count·latency·reasoning telemetry는 audit namespace에만 저장

`market_analysis`는 제약 전 `directional_stance=buy|sell|uncertain`, confidence, evidence reference를 출력한다. 허용 reference는 `previous_ltb_id:dim_i`, `stb_id:dim_i`, 현재 price/portfolio/constraint의 typed execution-state ID뿐이다. community 유래라면 post/root ID가 아니라 해당 reader의 `community_claim_id`여야 한다. validator가 LTB reference의 과거 provenance를 parent chain으로 확장한다. 실제 decision은 HOLD 없이 allowed action을 골라야 하므로 stance와 executed action이 다를 수 있다. 그 차이를 constraint effect로 기록한다.

위 두 memory prompt 외에도 stance schema, summary 제거, untrusted-community 처리 때문에 market-analysis/posting/community prompt와 Python serializer·validator가 함께 바뀐다. “프롬프트 두 개만 수정하면 된다”는 뜻이 아니다.

---

## 12. LTB context budget과 drift 방지

### 12.1 첫 구현에는 retrieval subsystem을 추가하지 않는다

이번 v1은 agent당 직전 `LTB_(t-1)` 하나와 현재 `STB_t` 하나만 거래용 belief block으로 쓴다. embedding, vector store, top-K retrieval, 별도 episodic/semantic memory agent는 추가하지 않는다. 이는 기존 코드를 적게 바꾸고 어느 belief가 거래에 들어갔는지 명확히 하기 위한 선택이다.

### 12.2 snapshot과 dimension transition의 관계

- existing `belief_history.dim_1~dim_6`: 다음 event의 거래에 전달될 LTB snapshot
- required `ltb_dimension_transitions`: dimension별 parent/current hash와 integration evidence
- evidence ledger와 edge: 원 사건·exposure·dimension support/contradiction audit

transition과 ledger는 별도 agent-visible memory가 아니다. downstream trade path는 승인된 previous LTB와 current STB six dimensions를 분리 block으로만 본다. v1은 기존 6D text를 그대로 재귀 갱신하는 thin adaptation이며, dimension 내부 claim list는 만들지 않는다.

### 12.3 크기와 실패 정책

- STB·LTB dimension별 기존 문자 제한과 전체 serialized byte/token cap을 P2 전에 freeze한다.
- cap 초과 시 문자열 중간 silent truncation이나 이전 dimension 내용의 조용한 삭제 금지
- proposed LTB가 cap을 넘으면 schema retry; 반복 실패하면 phase pause
- exact input/output/prompt/token hash와 `ltb_scientific_hash`·`human_log_hash`를 분리 저장
- 새 evidence가 없는 turn에도 LTB의 여섯 dimension은 새 텍스트·새 hash로 작성한다. 다만 parent의 material assertion을 근거 없이 삭제하거나 반대로 바꾸면 reject한다. 새 version row·LTB ID·parent/row hash는 turn마다 달라지는 것이 정상이다.
- short/current-manifest/extended 길이로 parameterize한 worst-case roll-forward에서 재귀요약 drift·핵심 주장 소실·context 증가를 canary한다. 최소한 실제 run의 manifest-derived `U`보다 짧게만 검사해서는 안 된다. synthetic fixture에서 수정 대상으로 표시하지 않은 보호 문장은 외부 fixture가 지정한 exact sentinel span으로 두고 byte-preservation 허용치를 0으로 둔다. 자유로운 “의미가 비슷하다” 판정으로 통과시키지 않는다. 한 건이라도 사라지거나 바뀌면 stable-item 구조를 즉석에서 섞지 않고 run을 NO-GO로 두고 별도 amendment한다.

### 12.4 결정론적 parent와 stage visibility

turn `t`의 LTB parent는 정확히 같은 run/condition/agent의 `LTB_(t-1)` 하나다. LTB 후보가 둘이거나 chain이 끊기면 pause한다. `LTB_t`는 `visible_from_turn=t+1`이며 같은 turn analysis/decision에는 보이지 않는다. 같은 event의 거래는 parent LTB와 current STB만 보므로, 먼저 끝난 agent에게 새 LTB가 새어 나갈 여지도 없다.

---

## 13. 제안 DB 스키마

모든 scientific table은 condition별 runtime DB에 두고 `run_id`, `condition_id`, `agent_id`, `turn` namespace를 key에 포함한다. v1은 외부 memory/vector index를 만들지 않는다.

### 13.1 기존 `belief_history`를 canonical LTB로 유지

- turn 0 row: deterministic initial `LTB_0`
- turn 1~`U` row: 거래 뒤 생성되는 next-event `LTB_t`; `U`는 run manifest의 ordered decision-turn map에서 계산
- 기존 `dim_1~dim_6`, `belief_summary`, `view_change` 유지
- `previous_ltb_id`, `source_stb_id`
- `trade_reflection_packet_hash`, dimension별 `integration_evidence` JSON
- deterministic human-log renderer version/hash
- prompt/policy/input/output hash, causal parent/cache/digest용 `ltb_scientific_hash`, 별도 `human_log_hash`
- `run_id`, `condition_id`, `created_stage=post_fill_ltb`, `visible_from_stage=next_decision`, `visible_from_turn=t+1`
- unique `(run_id, condition_id, agent_id, turn)`

agent·arm당 LTB state 수는 `1 + committed_LTB_update_count = U+1`이고, LTB updater의 committed logical count는 `U`다. 현재 45일 AM/PM manifest에서만 각각 91과 90으로 해석된다. `INSERT OR REPLACE`를 금지하고 insert-or-identical-hash만 허용한다.

### 13.2 새 `short_term_belief_history`

- `stb_id` primary key
- run/condition/agent/turn/date/subturn
- `dim_1~dim_6`
- dimension별 support/contradict evidence IDs
- prompt/policy/input/output/content hash
- `created_stage=stb`, `visible_to_stage=analysis_and_decision`
- unique `(run_id, condition_id, agent_id, turn)`

STB에는 turn-0 state가 없으므로 agent·arm당 committed row 수는 `U`이며 LTB update 수와 같아야 한다. physical retry attempt 수는 이 equality 대상이 아니다.

### 13.3 required `ltb_dimension_transitions`

- server-owned `transition_id`, `ltb_id`, dimension
- previous/current dimension text hash
- 같은 dimension·polarity의 current STB 또는 `dim_6` eligible earlier price-outcome packet에서 온 integration support/contradict evidence IDs; edge/parent ID 자체를 evidence ID로 대체하지 않음
- previous/current LTB ID, source STB ID, **current decision/fill transaction-episode hash**, earlier price-outcome packet hash, created turn/stage, `visible_from_turn`

모델은 ID·turn·hash를 만들지 않는다. 서버가 validated LTB output을 적용한 뒤 transition row를 생성한다. 여섯 dimension 모두 parent와 다른 current hash를 가져야 하고, parent material assertion의 삭제·반전에는 current STB 또는 eligible reflection 근거가 필요하다. 과거 provenance는 `previous_ltb_id` parent chain을 따라 resolve한다. 이 table은 memory 내용이 아니라 같은 6D 재귀 갱신을 감사하는 transition log다.

### 13.4 `observation_events`와 `agent_exposures`

`observation_events` 필수 필드:

- namespaced `event_id`, event/source/root type
- occurred/published/observed/last-modified 시각
- created/visible turn·stage
- public/private payload와 hash·schema version

`agent_exposures` 필수 필드:

- reader-owned `exposure_id`, source event ID, agent ID
- channel·turn·subturn·exposure level
- actually-read flag, visible turn/stage, payload hash
- unique namespaced exposure key

`community_interpretations` 필수 필드:

- reader agent/date/turn과 source exposure IDs, exact raw-input hash
- 서버가 부여한 claim별 `community_claim_id`, schema-valid `claim_text`, non-empty `source_exposure_ids`, server-resolved `source_exposure_levels`·distinct `root_post_ids`, `claim_stance=bullish|bearish|neutral|uncertain`
- unique `(run_id, condition_id, reader_agent_id, turn, community_claim_id)`와 junction table `community_claim_sources(claim_id, exposure_id)`; 각 source exposure는 같은 run/condition/reader이고 해당 turn에 interpretation-visible해야 함
- claim→source-exposures→distinct-roots chain. 동일 root가 selected/Best 등 여러 exposure로 들어와도 root support count는 1
- prompt-consumption edge와 validated output/schema hash
- raw title/body는 이 row의 source payload로만 참조하고 STB 및 LTB sanitized registry serialized payload에는 넣지 않음

### 13.5 `memory_evidence_edges`와 `turn_belief_trace`

edge는 support/contradict/derived-from/supersede/self-echo/prompt-consumption 관계를 append-only로 저장한다.

`turn_belief_trace`는 다음 DAG를 한 줄로 연결한다.

```text
current evidence IDs/hash
→ STB_t ID/hash
→ previous LTB ID/hash + STB_t를 사용한 analysis/decision
→ analysis/stance ID
→ decision/requested order
→ feasible actions/constraint_forced
→ actual fill/post-portfolio ID
→ same-turn post-fill LTB_t ID/hash + parent/STB/decision-fill episode/earlier-price-outcome lineage
→ current fill은 LTB_t에 즉시 한 번, 그 price outcome은 due event에서 각각 한 번 visibility
```

### 13.6 condition-scoped `response_journal.sqlite`

이 journal은 scientific runtime DB의 rollback 대상 table로 두지 않는다. 현재 RN run에서는 `RN_COMM_OFF/response_journal.sqlite`와 `RN_COMM_ON/response_journal.sqlite`로 분리하며 두 arm이 파일을 공유하지 않는다. 각 arm의 scientific runtime DB도 같은 디렉터리의 `paper_run.sqlite`다. phase rollback 뒤에도 validated response를 재사용하려면 journal 자체가 살아 있어야 하기 때문이다.

- deterministic logical call ID
- run signature hash
- condition/date/turn/phase/agent/stage
- request/prompt/schema/seed/model/provider/reasoning hashes
- validated response payload/hash
- physical attempt와 validation attempt
- status: validated/replayed/rejected
- token, cost, latency, finish reason

각 arm의 `paper_run.sqlite`에는 별도의 `phase_call_consumption` table을 두어 어떤 phase commit이 어떤 logical call ID와 response digest를 소비했는지만 기록한다. journal row의 존재는 scientific state commit을 의미하지 않는다.

### 13.7 append-only/idempotency 규칙

`INSERT OR REPLACE`를 사용하지 않는다.

- 처음에는 `INSERT`만 허용한다.
- unique key 충돌 시 기존 payload hash가 완전히 같으면 idempotent replay로 기록한다.
- hash가 다르면 deterministic conflict로 pause한다.
- 삭제 대신 status/version edge를 추가한다.

### 13.8 legacy 출력과 현재 RN canonical artifact

> 현재 RN 구현의 source of truth는 arm별 `paper_run.sqlite`, arm별 `response_journal.sqlite`·`openrouter_attempts.jsonl`, 그리고 hash-indexed finalization export다. 아래의 `submitted_orders.csv`, `exchange_fills.csv`, `portfolio_updates.jsonl` 등은 0720/legacy runner의 역사적 호환 artifact이며 현재 RN finalizer가 생성하는 canonical 파일명이 아니다.

legacy run root의 `agent_turns.csv/jsonl`, `submitted_orders.csv`, `exchange_fills.csv`, `portfolio_updates.jsonl`, `community_posts.csv`, `community_logs.csv`, `run_metadata.json`은 과거 결과·기존 report의 archive 입력으로 보존한다. 새 RN 실행이나 evaluator가 이 파일을 source of truth로 fallback해서는 안 된다.

| 현재 RN artifact | 내용 | canonical 역할 |
|---|---|---|
| `RN_COMM_OFF/paper_run.sqlite#paper_fill_ledger`, `RN_COMM_ON/paper_run.sqlite#paper_fill_ledger` + `traces/rn_comm_off_final_fill_ledger.csv`, `traces/rn_comm_on_final_fill_ledger.csv` | `fill_id`, requested/actual fill, fee rate/amount, gross/net notional, pre/post portfolio, constraint, STB/LTB/analysis/decision ID를 가진 append-only final row. 두 CSV는 table의 deterministic, schema-versioned canonical export이며 `traces/final_fill_export_index.json`이 path·hash·row count를 봉인한다. | 실제 `fill_t`의 canonical ledger와 사람이 읽는 arm별 CSV |
| `RN_COMM_*/paper_run.sqlite#trade_outcomes` | `fill_id`별 next-turn/H1/H5 mark, due/observed event, gross timing markout, action-aligned return, tentative/confirmed/contradicted/right-censored, horizon별 LTB consumption을 저장한다. | 가격 성찰·time gate 증빙 |
| `RN_COMM_*/paper_run.sqlite#turn_belief_trace` | current evidence → STB → previous LTB+STB decision input → decision → actual fill → same-turn post-fill LTB → next-event visibility의 ID/hash DAG를 저장한다. | STB/LTB 인과 trace |
| `RN_COMM_ON/paper_run.sqlite#community_post_trace` + `traces/community_post_trace.jsonl` | 공개 board와 분리된 private table 및 그 deterministic reviewer export다. posting status, post/root ID, author, LTB/view-change/fill, prompt/response hash, title/body hash를 연결한다. OFF table은 0행이어야 한다. | 게시글과 private post context 연결 |
| `community_interactions.csv`, `community_best_posts.csv`, `traces/community_exposure_trace.jsonl` | title-only 후보, 선택·reaction, frozen Best 원문, reader-owned exposure, 해석 claim과 STB evidence edge를 현재 canonical DB/journal에서 재구성한다. | 실제 community mechanism 증빙 |
| `RN_COMM_OFF/openrouter_attempts.jsonl`, `RN_COMM_ON/openrouter_attempts.jsonl` + 각 arm의 `response_journal.sqlite` | physical provider attempt/acceptance와 logical request/accepted response/commit을 run·arm·manifest에 결합한다. | reasoning-off·재시도·resume 증빙 |

`community_best_posts.csv`와 `community_interactions.csv`만으로는 어떤 본문 version이 실제 reader에게 전달됐는지 알 수 없으므로 `traces/community_exposure_trace.jsonl`이 이를 보완한다. 동일 root post가 selected와 Best로 모두 노출되면 exposure row는 둘 다 남기되 `root_post_id`로 evidence support 중복 여부를 판정한다. `RN_COMM_OFF`는 community no-op checkpoint와 0-count artifact만 허용한다. 빈 Best는 `empty`이고 마지막 PM의 non-empty Best는 `right_censored`다. 마지막 PM의 next-AM Best broadcast는 0이지만 같은 PM의 D1/D2 선택 열람 exposure는 존재할 수 있다.

---

## 14. Reasoning-off 설계

### 14.1 운영 정의

여기서 reasoning off는 provider의 extended-thinking/reasoning-token mode를 명시적으로 끈다는 뜻이다. 다음을 의미하지 않는다.

- 모델이 어떤 판단도 하지 않는다는 뜻
- news→belief→analysis→decision pipeline을 한 호출로 줄인다는 뜻
- 결과의 간단한 근거·risk control·view change 필드를 제거한다는 뜻

observable justification은 유지하되 chain-of-thought를 요구하거나 저장하지 않는다.

이번 연구에서 reasoning-off는 실험 통제일 뿐 아니라 **`N agents × U decision turns × arm 수`로 늘어나는 latency·token 비용을 감당하기 위한 운영 요구**다. reasoning을 생성한 뒤 숨기기만 하면 비용·시간 목적을 달성하지 못하므로 `exclude=true`만으로는 통과할 수 없다. canary는 동일 대표 stage에서 reasoning-on/off의 latency·total token·비용도 함께 기록하되, 본 실험에는 off만 허용한다.

### 14.2 기존 사용 모델과 OpenRouter API request

Paper launcher가 강제하는 실제 모델은 `config.py:71`의 `qwen/qwen3.5-flash-02-23`이다. `.env.example`의 `openai/gpt-4o`는 stale하므로 이번 launcher에서 허용하지 않는다. [해당 OpenRouter 공식 model page](https://openrouter.ai/qwen/qwen3.5-flash-02-23/api)는 `reasoning` parameter 지원을 표시한다. 다만 parameter 목록만으로 `none` 수용 또는 실제 off를 보장하지는 않는다.

[OpenRouter 공식 reasoning 문서](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)는 `reasoning.effort = "none"`이 reasoning을 끄고, `exclude = true`는 reasoning을 사용하되 응답에서 숨길 뿐이라고 설명한다. Reasoning token은 output token으로 과금된다. 또한 `/api/v1/models`의 model별 `reasoning.supported_efforts`, `mandatory`, `default_enabled`를 확인하라고 명시한다.

권장 request invariant:

```json
{
  "reasoning": {
    "effort": "none",
    "exclude": true
  }
}
```

`exclude`는 보조 privacy 설정일 뿐 off의 증거로 사용하지 않는다. 실제 off 조건은 `effort: none`과 live canary다.

구현상 현재 `twinmarket_kr/llm/client.py:92-98`의 `extra_body`에는 provider object만 들어간다. 최종 request는 두 object를 같은 `extra_body`에 합쳐야 한다.

```python
extra_body = {
    "reasoning": {"effort": "none", "exclude": True},
    "provider": {
        "only": [PINNED_PROVIDER],
        "order": [PINNED_PROVIDER],
        "allow_fallbacks": False,
        "require_parameters": True,
    },
}
```

`require_parameters=true`는 reasoning parameter를 지원하지 않는 endpoint로 요청이 가거나 parameter가 조용히 무시되는 것을 막는다. [OpenRouter provider-routing 문서](https://openrouter.ai/docs/guides/routing/provider-selection)에 따라 `allow_fallbacks=false`와 정확한 provider allowlist/order를 함께 쓴다. Legacy `include_reasoning=false`는 `exclude=true`와 동등한 **숨김 설정**이므로 사용 금지다.

본 실행 직전 `/api/v1/models`와 endpoint snapshot을 저장한다. 현재 `qwen/qwen3.5-flash-02-23` endpoint는 provider `alibaba`, `reasoning` 지원, `mandatory=false`를 보이지만 `supported_efforts`를 노출하지 않는다. 따라서 metadata에 `none`이 없다는 이유만으로 통과/실패를 추정하지 않는다. **실제 `effort:none` request를 `only/order=[alibaba]`, fallback off, require-parameters true로 보낸 live canary가 reasoning token 0을 증명할 때만** 이 모델을 허용한다. request 거절·telemetry 누락·nonzero token 중 하나라도 있으면 `exclude=true`로 대체하지 않고 strict-off run은 NO-GO다.

### 14.3 모든 stage 적용

다음 중 하나라도 reasoning 설정을 빠뜨리면 run을 시작하지 않는다.

- initial belief는 future persona-conditioned amendment에서 API로 다시 만들 때만 적용; 현재 deterministic neutral `LTB_0`는 API call 0회
- depth별 news interpretation
- Depth 2 pre-search/post-search
- STB update
- per-turn LTB update
- market analysis
- decision
- community posting
- reading selection·reaction
- next-AM community interpretation
- validation retry

적용 단위는 logical stage가 아니라 **실제 전송된 모든 physical HTTP attempt**다. 중앙 client가 provider option merge가 끝난 최종 request에 reasoning-off object를 강제로 삽입하며, stage 호출자·환경변수·fallback이 이를 삭제하거나 덮어쓸 수 없다. transport retry와 schema-validation 재생성 각각에 독립 audit row를 남긴다.

### 14.4 live canary와 fail-closed

본 실행 전 provider별로 reasoning을 유도하기 쉬운 canary prompt와 실제 stage schema prompt를 모두 시험한다.

통과 조건:

- exact requested/returned model 일치
- pinned provider 일치
- request audit에 reasoning object 존재
- response의 `reasoning`, `reasoning_details`가 비어 있음
- `usage.completion_tokens_details.reasoning_tokens == 0`
- structured schema pass
- 동일 policy로 최소 반복 canary pass

reasoning token telemetry가 전혀 없어 off를 감사할 수 없는 provider는 pilot와 본 실행 모두에서 사용하지 않는다. 모든 physical attempt는 **전송 직전 최종 request body**에 reasoning-off object와 그 hash가 있어야 한다. 성공 response에는 exact model/provider, empty reasoning fields, reasoning token 0과 완전한 telemetry가 필요하다. response가 전혀 없는 timeout/network error에는 response telemetry를 요구할 수 없으므로 request audit와 error class를 남기고 사전등록한 transient whitelist에 한해서만 retry한다. response가 왔는데 telemetry가 없거나 reasoning/model/provider가 어긋난 경우에는 non-retryable 즉시 pause다. 다음 retry가 정상이어도 그 phase를 계속하지 않는다.

Canary는 간단한 문장 한 개만 시험하지 않는다. news interpretation, STB, LTB, market analysis, decision, posting/community의 실제 schema prompt를 stage별 최소 1회 보내고 raw redacted request body와 response usage를 저장한다. 이 검증이 없으면 Qwen model page에 `reasoning` 지원이 표시돼도 off가 실제 적용됐다고 보지 않는다.

### 14.5 provider policy

- `OPENROUTER_ALLOW_FALLBACKS=false`
- provider를 검증된 한 곳으로 pin
- returned model/provider mismatch 즉시 pause
- model metadata snapshot과 endpoint metadata hash 보관
- provider가 사라지거나 semantics가 바뀌면 기존 run을 다른 provider로 이어 붙이지 않음

### 14.6 prompt 정리

- “생각 과정을 단계별로 출력”하는 문구 제거
- “현재 판단 시점에서 evidence ID를 근거로 최종 구조화 결과를 출력”으로 변경
- STB prompt의 “매일 아침”을 AM/PM 모두 맞는 “현재 판단 시점”으로 변경
- 새 `update_long_term_belief.txt`에도 chain-of-thought나 단계별 사고 출력 요구를 넣지 않음
- 관찰 가능한 짧은 근거만 출력하고 숨은 reasoning을 요구하지 않음

---

## 15. 100-agent 실행 설계

### 15.1 agent_count와 concurrency 분리

100명을 모두 사용한다는 것은 100개 요청을 동시에 보낸다는 뜻이 아니다.

- 이번 paper `StudySpec`은 `required_agent_count = 100`이고 exact ordered registry cardinality가 100이어야 한다.
- `per_run_worker = 30`을 기본 후보로 유지
- `global_api_concurrency = 16`은 canary 통과 후 freeze
- 중앙 scheduler가 arm별 fair quota와 RPM/TPM token bucket을 관리

후속 pilot/확장에서는 새 `required_agent_count`와 exact agent registry를 승인해 `N`을 줄이거나 늘릴 수 있어야 한다. 이때 launcher·DB schema·validator·분석 코드는 바꾸지 않고 resolved key/count만 달라진다. 기존 run을 중간에 100명에서 다른 수로 resume하거나 `first_n`으로 자르는 것은 금지한다.

전역 16을 각 arm에 무조건 8개씩 영구 고정할 필요는 없지만, 두 arm이 동시에 runnable일 때 starvation이 없도록 round-robin/fair queue를 사용한다. Provider drift를 줄이기 위해 같은 date/subturn pair가 모두 끝나기 전에 한 arm이 다음 subturn으로 진행하지 않는다.

### 15.2 depth 복구 후 예상 구성

확정 분포 30/55/15 기준:

- Depth 0: 30명, Best5-only passive audience; posting/selective reading/reaction 없음
- Depth 1: 55명
- Depth 2: 15명
- active posting/selective-reading/reaction: 70명
- Best 5 passive/active audience: 100명
- initial cash 1억: 90명
- initial cash 10억: 10명

### 15.3 manifest-derived 예상 logical generation 규모

validation/transport retry를 제외한 대략적 성공 호출 수다. 현재 deterministic neutral `LTB_0`에는 별도 initial API call이 없다. 호출 수도 `×4`, `×2` 같은 stage 수를 코드에 다시 쓰지 않고, resolved stage DAG가 만든 logical call/opportunity key set의 cardinality로 계산한다.

| 조건 | 일반식 | 현재 manifest 해석 예시 |
|---|---|---:|
| Community OFF | `|required_core_call_keys(RN_COMM_OFF)|` | 현재 4개 core stage 기준 36,000 |
| Community ON | `|required_core_call_keys(RN_COMM_ON)| + |committed_conditional_community_call_keys|` | community 결과에 따라 약 39,150~49,850 |
| 2-arm 합계 | 두 arm의 exact committed logical call-key cardinality 합 | 약 75,150~85,850 |

현재 예시에서는 `D=45`, `U=90`, `N=100`, `N_depth2=15`, `N_active=70`이다. 현재 core DAG는 각 agent-event마다 `STB → market analysis → decision → post-fill LTB` 네 logical call이므로 arm당 `N×U×4=36,000`이다. fill, Best 확정, 원문 DB join, next-turn/H1/H5 계산에는 별도 LLM call을 쓰지 않는다. `RN_COMM_ON`에는 모든 PM의 eligible author posting decision 3,150회가 추가된다. 실제 게시글이 있을 때만 reader select가 최대 3,150회, 실제 선택이 있을 때만 reaction이 최대 3,150회, 연구기간 안 다음 AM에 실제 community payload가 보일 때 interpretation이 최대 4,400회 추가된다. 따라서 현재 예시의 ON 범위는 39,150~49,850이다. 이 숫자는 현재 schedule을 사람이 풀어 쓴 설명값이며 launcher나 완료 validator의 입력이 아니다. 완료 판정은 journal과 audit에서 파생한 exact logical-call ID 집합을 사용하고 schema/transport attempt는 logical count에 포함하지 않는다.

Unconditional stage는 실행 전 exact required-call key set을 만든다. Posting, selection, reaction, non-empty community interpretation처럼 이전 stage 결과에 달린 call은 실행 전에 opportunity key set을 만들고, runtime이 각 key를 `ineligible|eligible_not_scheduled|scheduled|committed` 중 하나로 단 한 번 종결한다. 그러면 “실제 호출 수가 상한보다 작다”는 이유로 누락을 숨기지 않으면서도 empty board/read 때문에 불필요한 call을 강제하지 않는다.

빈 Best/읽은 글에 별도 community interpretation을 호출하면 community 정보가 없는데도 추가 generation prompt가 treatment cue가 될 수 있으므로 기본 설계에서는 명시적 no-exposure event만 쓰고 call을 생략한다. 현재 75,150~85,850 logical-call 설명 범위에 평균 latency 10초와 **canary 뒤 잠정 freeze할** global concurrency 16을 단순 적용하면 service-time 하한은 약 13.0~14.9시간이다. 기간·cohort·DAG가 바뀌면 이 시간도 exact call opportunities와 canary stage latency에서 다시 계산한다. validation retry, 429, barrier tail, LTB의 긴 출력, checkpoint, DB 작업은 별도다.

STB는 기존 belief call을 재사용하므로 기존 대비 추가 STB call은 0이다. 새 비용은 LTB updater `N×U` calls/arm이며 현재 manifest에서만 9,000으로 해석된다. reasoning-off, 같은 6D와 짧은 structured integration-evidence 출력으로 예산을 관리하되, LTB의 매-turn 새 작성과 no-copy validator는 비용 절감 목적으로 약화하지 않는다. 각 새 manifest의 `D/U/N`과 canary 실측 token·latency·비용으로 예산을 다시 계산한다.

### 15.4 재시도 증폭

현재 설정의 transport `MAX_RETRIES=6`은 코드상 **초기 요청을 포함한 최대 6 attempts**, 즉 최대 5 retries다. schema validation도 초기 생성을 포함해 최대 4 attempts이므로 중첩되면 한 logical stage가 최악 24개 physical request를 만들 수 있다.

필수 완화:

- validation attempt와 transport attempt를 별도 기록
- exponential backoff에 seeded jitter 추가
- rolling 429/schema-failure circuit breaker
- stage별 `max_tokens`
- stage별 retry hard cap과 run cost cap
- 같은 invalid payload를 무한 반복하지 않음
- `RN_COMM_ON`의 더 긴 community/memory context 때문에 `RN_COMM_OFF`보다 retry·truncation이 체계적으로 많아질 수 있으므로 arm×stage별 attempt·invalid-output·latency를 비교한다. canary에서 사전 고정한 arm 간 차이 threshold를 넘으면 본 실행을 pause하고 prompt/schema budget을 먼저 고친다.

---

## 16. 연속 실행·checkpoint·resume 설계

### 16.1 유지할 기존 장점

현재 checkpoint runner의 다음 원칙은 manifest-derived 형태로 유지한다.

- `RN_COMM_OFF/paper_run.sqlite`, `RN_COMM_ON/paper_run.sqlite`를 각각 승인된 `D` 거래일 전체에서 지속 사용
- ordered `(date, subturn)` turn manifest가 계산한 global turn `1..U` 유지
- manifest에 활성화된 decision subturn과 post-decision community phase snapshot
- 성공 phase만 완료 처리
- 실패 시 phase 시작점으로 rollback
- transient failure만 process restart
- deterministic validation/model/local integrity error는 pause

단, validated-response journal은 phase snapshot/rollback 밖에 둔다. scientific runtime state만 rollback하고, 이미 검증된 API response는 durable journal에 보존한다.

### 16.2 100명 phase의 핵심 문제

현재는 한 agent가 실패해도 100명 phase 전체 DB와 로그를 rollback한다. 성공한 99명의 API 응답도 버리고 재호출하면 비용, provider nondeterminism, 시간 모두 크게 증가한다.

### 16.3 validated-response journal

다음 방식으로 바꾼다.

```text
phase input snapshot freeze
→ 100 agent-stage logical call keys 생성
→ validated response를 append-only journal에 staging
→ crash 후 같은 key/hash이면 성공 response replay
→ 누락/실패 key만 재호출
→ AM에는 전날 scheduled community payload resolve·raw-body interpretation call·Best reader exposure/기존-read prompt-consumption staging
→ 대상 agent의 validated community interpretation barrier; Best-only raw body는 여기서 처음 소비되고, selected body는 전날 PM selected-read/reaction 뒤 같은 exposure의 두 번째 prompt consumption. 어느 raw body도 STB/LTB payload에는 넣지 않음
→ 100명 STB response·schema·evidence barrier
→ previous LTB + current STB로 100명 analysis barrier와 decision barrier
→ 주문·actual fill·post-portfolio를 deterministic staging
→ current fill은 current STB·analysis·decision에는 역주입하지 않고, 같은 turn post-fill LTB용 structured decision/fill episode로 ledger staging
→ previous LTB + current STB + current decision/fill episode + eligible earlier price outcome으로 100명 post-fill LTB call·rewrite barrier
→ 짧은 `BEGIN IMMEDIATE … COMMIT`에서 agent_id 순서로 AM exposure/community-interpretation/community-claim/claim-source-junction/evidence-edge/STB/LTB/analysis/stance/decision/order/fill/portfolio/ledger/trace/phase-call-consumption batch commit
→ phase STB/LTB/scientific-state digest
→ checkpoint complete
```

현재 `daily_cycle.py`는 agent coroutine 안에서 belief와 trade DB write를 수행하므로 `asyncio.gather()`가 끝나기 전에 일부 agent의 scientific state가 이미 바뀔 수 있다. `run_agent_turn()`은 DB side effect가 없는 `AgentTurnResult`를 반환하도록 분리하고, 100명 결과가 모두 검증된 뒤 한 transaction 또는 명시적 deterministic batch transaction에서 commit해야 한다.

API call을 SQLite write transaction 안에서 기다리면 lock이 장시간 유지되므로 금지한다. Durable response journal과 phase staging은 canonical scientific tables의 transaction 밖에 있고, 마지막 commit만 짧아야 한다. 기존 구조를 곧바로 side-effect-free로 바꾸기 어렵다면 condition별 shadow DB에서 phase를 완주·검증한 뒤 canonical DB로 승격하는 방법을 차선으로 쓴다. `_update_portfolios_from_results()` 뒤에는 **실제 committed fill과 post-portfolio가 확정됐기 때문에**, 그것을 structured transaction episode로 넣는 same-turn post-fill LTB finalizer가 필수다. 단, current fill을 STB·analysis·decision에 되돌리거나 미래 outcome을 이 finalizer에 넣는 것은 금지한다.

crash reconciliation은 다음 규칙을 쓴다.

- journal validated, runtime consumption 없음: response를 재호출하지 않고 replay해 phase staging을 복구한다.
- journal validated, runtime consumption 있음, phase digest 일치: 이미 소비된 것으로 인정한다.
- consumption은 있으나 runtime row/digest 불일치: 자동 복구하지 않고 pause한다.
- 같은 logical ID에 다른 response/request hash: deterministic conflict로 pause한다.

cache key에는 최소한 다음이 포함돼야 한다.

- run signature
- condition/date/turn/phase/agent/stage
- prompt/input/schema hash
- seed
- model/provider
- reasoning object
- temperature/max_tokens
- STB/LTB policy, exact previous/current version, STB/LTB prompt-input hash

다른 condition, 다른 prompt, 다른 provider, 다른 reasoning setting의 response를 재사용하지 않는다.

실행 시작 때 code/prompt/config/persona DB/news를 immutable run bundle로 복사·hash-pin한다. 현재 코드처럼 prompt를 매 호출 다시 읽거나 persona DB를 phase마다 읽을 경우, 실행 중 파일 수정이 후속 phase에 섞일 수 있으므로 매 phase 시작 hash가 bundle manifest와 다르면 pause한다. Archive/final DB hash 전에는 WAL checkpoint를 수행하고 `db`, `-wal`, `-shm`, response journal 및 manifest의 일관된 snapshot을 보존한다.

Community phase는 하나의 느슨한 gather가 아니라 다음 종속 barrier로 나눈다.

```text
posting decision staging → post batch commit → public board freeze
→ selection staging → selected-body fetch/reaction staging
→ interaction batch commit → score freeze/Best 5 확정
→ 100명 next-AM broadcast 예약 → community phase commit
```

각 LLM substage는 별도의 logical call key를 가지며, 뒤 substage는 앞 barrier의 digest를 input hash에 포함한다.

### 16.4 phase barrier와 scheduler nondeterminism

- 모든 agent는 phase 시작 시 고정된 동일 **공용 외생 market/news/public-board snapshot**을 본다. persona, portfolio, private memory는 agent별로 다르지만 각각 phase 시작 시 고정한다.
- 응답 완료 순서가 post ID, reaction order, memory rank를 바꾸지 않게 agent ID 순으로 commit한다.
- 같은 phase 중 먼저 끝난 agent의 belief·trade·memory를 다른 agent가 볼 수 없다.
- 로그 분석은 append row 순서가 아니라 deterministic event key로 정렬한다.

### 16.5 pair barrier

두 arm의 동일 date/subturn을 하나의 pair block으로 본다.

- `RN_COMM_OFF` AM과 `RN_COMM_ON` AM 완료 후 PM으로 진행
- `RN_COMM_OFF` PM과 `RN_COMM_ON` PM 완료 후 community/no-op로 진행
- `RN_COMM_ON` community와 `RN_COMM_OFF` no-op 완료 후 다음 거래일 진행
- 한 arm이 P0/P1 integrity error로 pause하면 다른 arm도 pair boundary에서 pause

이렇게 해야 며칠 간의 provider·모델·서비스 상태 변화가 한 arm에만 몰리는 것을 줄일 수 있다.

### 16.6 프로세스·장비 재시작

긴 실행을 실제로 이어가려면 checkpoint 외부에 supervisor가 필요하다.

- macOS에서는 별도 `launchd` job 또는 동등한 supervisor
- singleton run lock
- 30~60초 heartbeat
- machine reboot 뒤 같은 exact command와 output root로 resume
- secrets는 repository 밖 권한 제한 env file에서 로드
- transient exit만 제한 횟수 내 자동 restart
- auth/model/schema/data integrity error는 `paused.json`을 남기고 사람 확인 전 재시작 금지
- `run_complete.json`이 있으면 supervisor 종료

### 16.7 resource guard

실행 중 다음을 감시한다.

- disk free space와 WAL size
- API credit·누적 token·예상 비용
- open file descriptor
- rolling latency·429·5xx·schema retry rate
- DB `quick_check`
- heartbeat와 마지막 committed phase

disk free 20% 미만 또는 preregistered absolute GB threshold 미만이면 새 phase를 시작하지 않는다.

---

## 17. clean base와 condition 격리

### 17.1 clean base 내용

허용:

- StockData와 고정 market inputs
- 100명 전원의 승인된 turn-0 `LTB_0`
- 100명 전원의 turn-0 cash-only portfolio
- schema와 immutable study metadata

금지:

- turn 1 이상 belief/portfolio
- trade/fill/community/memory episode
- recovery/system message
- 이전 실행 또는 다른 condition의 LLM journal/cache
- 과거 PnL 또는 초기 stock position

### 17.2 memory schema를 runtime lifecycle에 포함

새 scientific memory tables를 다음 모두에 포함해야 한다.

- `RUNTIME_TABLES`
- clean-base reset/validation
- phase snapshot/rollback
- STB/LTB lineage digest
- resume integrity
- final archive/manifest

예외적으로 `RN_COMM_OFF/response_journal.sqlite`, `RN_COMM_ON/response_journal.sqlite`는 phase snapshot/rollback에 넣지 않는다. 대신 run signature로 namespace를 봉인하고 hash/checkpoint·최종 archive에는 포함한다. 각 arm의 `paper_run.sqlite#phase_call_consumption`만 scientific digest와 rollback에 포함한다.

v1에는 외부 vector/index cache를 생성하거나 사용하지 않는다. 후속 amendment에서 retrieval을 도입하더라도 checkpoint의 source of truth로 둘 수 없고, DB event에서 deterministic하게 재생성·검증해야 한다.

### 17.3 output root

기존 30명 결과나 현재 runtime DB와 별도의 새 root를 사용한다.

권장 구조:

```text
outputs/experiments/realnews_comm_ab_hmem_v1/
  study_spec.json
  resolved_study_manifest.json
  preregistration_snapshot.md
  base/
  RN_COMM_OFF/
  RN_COMM_ON/
  canary/
  integrity/
```

---

## 18. 로그와 run signature

### 18.1 LLM audit 필수 필드

- run, condition, seed, date, global turn, subturn, phase
- agent ID, stage, logical call ID
- request/prompt/response/schema hash
- physical attempt, validation attempt
- requested/returned model과 provider
- reasoning request object
- reasoning text/details presence와 reasoning token count
- input/output/cached token, cost, latency, finish reason
- temperature, max_tokens, timeout
- committed/replayed/rolled_back/rejected

### 18.2 memory audit 필수 필드

- current evidence IDs/cutoff/as-of와 exact STB prompt input hash
- current STB ID/hash와 dimension별 evidence lineage
- previous LTB ID/hash와 입력된 `dim_1~dim_6`
- trade에 실제 사용한 previous LTB ID/hash와 current STB ID/hash
- 거래 뒤 생성한 next-event-visible LTB ID/hash, parent LTB ID, source STB ID, current decision/fill episode hash, earlier price-outcome packet hash와 `visible_from_turn`
- dimension별 parent/current hash와 integration-evidence IDs, parent-chain resolver 결과 hash
- LTB prompt/policy/input/output hash와 next-event visibility
- `directional_stance`, requested decision, actual fill, feasible actions, `constraint_forced`
- fill의 same-turn transaction-episode consumption ID와 next-turn/H1/H5 outcome maturity turn
- cross-run/condition/agent evidence check

### 18.3 run signature 추가 항목

- memory schema version
- ordered decision-event map/hash, 해석된 `U`, cadence `each_decision_event`, committed STB/LTB count invariant `U:U`와 현재 manifest의 derived count
- STB/LTB six-dimension schema와 byte/token cap
- H1/H5 outcome-feedback maturity definition
- LTB all-dimension fresh-rewrite/no-copy policy와 material-assertion preservation rule
- duplicate-root/contradiction policy
- belief-link mode `current_external_evidence_to_stb_then_previous_ltb_plus_stb_to_trade_then_recursive_ltb_for_next_event`
- `commission_rate=0.0`·`sell_tax_rate=0.0`·`fee_amount=0` 강제, full-fill·cash-ratio·no-hold policy
- ordered input-date hash, frozen burn-in-policy/date-mask hash, derived evaluation-date hash와 evaluator metric version. 현재 manifest에서 해석된 45/3/42는 authored constant가 아니라 resolved field로 함께 기록
- AM/PM actual-fill signed-value aggregation, value-primary/volume-secondary policy
- reasoning-off object와 canary result hash
- provider metadata snapshot
- stage temperature/max_tokens
- rate limiter/concurrency
- dependency lock, Python/SQLite/SDK version

### 18.4 `RUN_RECORD.md`와 artifact index

각 condition run은 완료·pause·resume 상태와 무관하게 사람이 읽는 `RUN_RECORD.md` 하나를 deterministic하게 생성한다. 이는 수동 메모가 아니라 frozen manifest와 artifact hash에서 렌더한 **run의 첫 진입점**이다. 팀원은 여러 CSV/DB/PDF를 먼저 찾지 않고 이 파일을 열어야 한다.

필수 항목:

1. `run_id`, `condition_id` (`RN_COMM_OFF` 또는 `RN_COMM_ON`), 완료/integrity/pause 상태, code·prompt·schema·model/provider hash
2. cohort snapshot hash, resolved agent/depth/initial-portfolio 요약, date/event registry, burn-in mask, fee/fill/reasoning-off policy
3. real-news bundle hash, `news_shortage_exception_manifest.jsonl` hash, event별 `target_real=10`·selected/serialized/delivered-real count·payload hash equality·`actual_real_count`·`news_coverage_status`·`fake=0` 검사와 article-version leakage review 결과
4. legacy artifact와 canonical sidecar의 상대경로, schema version, row count, SHA-256, compatibility/canonical 역할
5. expected 대비 observed STB/LTB/decision/fill/community exposure/LLM-call count와 오류·retry·right-censor count
6. paired run이면 RN_COMM_OFF/RN_COMM_ON invariant hash equality와 허용 차이 `community_mode` 하나의 검사 결과
7. 생성된 validator/PDF/report artifact의 입력 hash·출력 path·상태. 실패한 report도 숨기지 않고 `not_generated_due_to_gate`로 남긴다.

`RUN_RECORD.md`와 새 report/evaluator는 전역 `outputs/sys_100.db`, 현재 working DB, 임의 최신 persona DB를 읽지 않는다. 해당 run 아래의 frozen manifest·cohort snapshot·target snapshot·artifact만 읽는다. 과거 PDF는 archival-only이며 새 baseline의 증거 원천으로 재사용하지 않는다.

---

## 19. 분석 계획

### 19.1 primary outcomes

과도한 outcome shopping을 피하기 위해 본 실행 전 소수 primary outcome을 고정한다.

권장 primary는 RQ별 하나씩만 둔다.

1. **RQ1 삼성전자 개인 방향 행동 정합성**: clean community-OFF인 `RN_COMM_OFF`에서 100명 agent의 AM+PM actual fill을 일별 **gross signed fill value(수수료 전)**로 합산하고, 같은 날 실제 삼성전자 `Individuals` 순거래대금 방향과 비교한 manifest evaluation-date set `E`의 balanced accuracy다. 현재 resolved example에서는 첫 3거래일 burn-in 뒤 `E=42`일이며, 원천·체결 완전성은 full date set `D=45`일을 먼저 통과해야 한다.
2. **RQ2 community 총효과**: 같은 evaluation-date set `E`에서 `RN_COMM_ON − RN_COMM_OFF`의 날짜별 initial-capital-normalized gross signed order imbalance 차이를 같은 date/subturn world pair로 합성한 효과다.

RQ1의 raw accuracy, confusion matrix, buy/sell recall, MCC와 `RN_COMM_ON` 정합성은 필수 diagnostic이다. full date set `D`의 지표는 초기조건 영향을 보여 주는 full-period diagnostic이다. 여섯 차원의 belief shift·dispersion·convergence와 PnL은 secondary/mechanism outcome으로 내린다. 이 구분 없이 여러 지표를 모두 primary로 두지 않는다.

원 RQ1은 현재 validation 구현과 같은 행동 재현 지표다. 당일 PM 종가와 PM 정보까지 사용한 체결을 포함하므로 이를 엄밀한 실시간 사전예측이라고 부르지 않는다. 별도로 기억이 만든 방향 자체를 확인하기 위해 다음 RQ1-N을 보조 진단으로 보고한다.

```text
target:      삼성전자 실제 Individuals 일별 순거래 방향(d) = BUY / SELL
prediction:  당일 AM 주문 전에 기록한 100명 directional_stance의 합성 신호(d)
label access: 실험 runtime과 분리된 evaluator가 사후에만 결합
```

- `directional_stance`는 market analysis가 “매수와 매도가 모두 가능하다고 가정할 때 선호하는 거래 방향”을 `buy | sell | uncertain`으로 기록한 제약 전 의도다. 삼성전자 주가가 bullish라는 서술과 실제 개인이 net-buy한다는 target을 자동으로 같은 개념으로 간주하지 않는다.
- 집단 신호는 AM의 `buy` 수와 `sell` 수 차이로 만들고 `uncertain`은 0으로 처리한다. 동률이면 `NO_CALL`이며 coverage와 conditional accuracy를 별도 보고한다. 권장 기본값은 unweighted majority다.
- PM stance는 같은 날 종가를 보므로 same-day 사전예측에 사용하지 않는다. PM은 개인 trace/mechanism에 사용하고, PM으로 다음 거래일 방향을 예측하려면 별도 preregistration을 한다.
- 당일 종가가 관찰되기 전의 AM payload, 당일 확정 개인 순매수, 미래 뉴스, 미래 community 글은 STB/LTB와 prediction payload에 절대 들어가지 않는다. 개인 순매수 label은 evaluator 전용 분리 테이블에 보관한다.

현금·보유수량 제약 또는 첫 turn의 cash-only 조건은 실제 주문 방향을 기계적으로 바꿀 수 있다. 따라서 원 RQ1의 실제 체결 정합성과 RQ1-N의 제약 전 방향 진단을 함께 보고하되 서로 대체하지 않는다.

### 19.2 secondary/mechanism outcomes

- order size와 portfolio exposure
- capital-normalized turnover
- return/PnL
- community-derived evidence가 belief에 인용되는 비율
- community-derived LTB 변화의 persistence duration
- unsupported 또는 expired claim reuse
- support와 contradiction이 함께 제시·반영되는 비율
- belief→decision direction consistency
- intention→execution 차이와 cash constraint
- current total value 정규화 결과는 community와 과거 거래의 영향을 받은 post-treatment denominator이므로 primary가 아니라 sensitivity로만 보고
- 1억-only(=rich-excluded alias)·10억-only·leave-one-rich-out 방향, alias byte-equality, raw-vs-initial-capital-normalized sign discordance와 rich contribution share·`wealth_fragile`

### 19.3 개인 방향 추적성과 STB/LTB 사용 검증

후속 fake-news 실험이 가능하려면, 단순히 맞힌 날의 비율만이 아니라 각 agent가 어떤 경로로 방향을 형성했는지 재구성할 수 있어야 한다. 이는 “LTB가 기존 판단을 고집하게 만들었는가”가 아니라, **previous LTB와 current STB가 어떻게 분리되어 현재 방향·수량에 연결되고, 거래 뒤 실제 decision/fill episode를 해석한 LTB가 다음 event에 어떻게 재귀 갱신됐는가**를 확인하는 장치다.

각 `agent × date × subturn`에 아래 불변 trace를 append-only로 남긴다.

- current external evidence: 노출 news/community ID, cutoff/as-of, STB input hash
- STB: current-only `dim_1~dim_6` ID/hash와 cited evidence IDs
- 판단: previous LTB와 STB를 분리 block으로 받은 analysis의 제약 전 `directional_stance`, confidence와 허용된 `previous_ltb_id:dim_i`/`stb_id:dim_i`/execution-state reference
- 실행: feasible action set, requested action/size, actual fill, constraint/forced-action flag
- LTB 갱신: 거래 뒤 생성된 next-event-visible LTB `dim_1~dim_6` ID/hash, parent/source-STB/current decision-fill episode/earlier price-outcome packet hash와 dimension별 integration evidence
- 시간경계: current fill은 same-turn post-fill LTB에만 transaction episode로 즉시 한 번 들어가며, its price outcome은 각 due event에서만 visible

`directional_stance`는 기존 `dim_1`~`dim_6`의 의미를 바꾸지 않고 market-analysis 출력에 추가하는 최소 schema다. 거래용 canonical belief block은 previous LTB와 current STB이고 summary/change는 사람용이다.

**설명 불가능한 reversal 검증**: 직전 subturn 대비 `buy ↔ sell` stance가 바뀌면, current STB 및 stance가 실제로 인용한 새 news/community evidence 또는 typed execution-state 변화가 있어야 한다. 단지 registry에 새 evidence가 존재한다는 이유로 통과시키지 않는다. cited lineage가 없으면 `unexplained_reversal` flag를 세운다. `uncertain`, 제약에 의한 executed-action 차이, 첫 turn은 별도다.

검증자는 임의 agent/day를 골라 response journal과 hash만으로 다음을 replay할 수 있어야 한다: “어떤 current evidence로 STB가 만들어졌고 → 어느 previous LTB와 함께 stance·주문·실제 fill로 이어졌고 → 그 actual decision/fill episode와 어떤 eligible earlier price outcome을 해석해 next-event-visible LTB를 만들었는가.” 재호출로 같은 문장을 다시 생성하는 것이 아니라 당시 확정 payload를 byte-for-byte 재구성해야 한다.

### 19.4 forced trading 주의

현재 action space는 buy/sell only이며 hold가 없다. 다만 constraint engine이 먼저 feasible action을 거른다. 모든 agent가 cash-only로 시작하므로 첫 turn에는 SELL이 불가능해 사실상 BUY만 허용된다. 이후에도 position·cash에 따라 가능한 방향이 제한될 수 있고, buy/sell 둘 다 불가능하면 LLM 호출 전에 `DecisionConstraintError`로 pause해야 한다.

- trade 여부나 trade count를 자연스러운 시장 참여율로 해석하지 않는다.
- 방향, 수량, normalized notional, belief와의 정합성을 본다.
- 현금 부족으로 낙관 belief와 SELL이 함께 나올 수 있으므로 SELL을 곧바로 bearish belief로 간주하지 않는다.
- 첫 turn BUY를 community나 belief 효과로 해석하지 않고 기계적 초기조건으로 표시한다.
- 첫 turn은 확정된 3-day burn-in 안에 있어 primary 42일에는 들어가지 않는다. 그래도 45일 integrity/full-period diagnostic에서는 삭제하지 않고 `constraint_forced`로 남긴다.
- hold를 추가하려면 이번 arm에 섞지 말고 별도 trading-rule ablation으로 한다.
- **primary는 정확히 첫 3거래일 제외**다. full-45, Day1만 제외, 5일 제외, 양방향 feasible turn만 남긴 결과, equal-agent 및 initial-capital-normalized 결과는 이름이 고정된 sensitivity로 함께 보고한다. 결과를 본 뒤 이들 중 가장 좋은 skip을 headline으로 고르지 않는다.
- `constraint_forced`는 LLM 서술이 아니라 서버가 `len(allowed_actions) == 1`인지로 계산한다.

### 19.5 ITT와 participation-mode subgroup

- primary: 100명 전체 ITT
- secondary: Best5-only passive Depth 0의 30명과 active Depth 1·2의 70명
- 실제로 글을 읽었는지, 어떤 글을 골랐는지는 post-treatment mediator이므로 primary adjustment covariate로 넣지 않는다.
- execution-notional-weighted raw 결과와 agent별 fixed-initial-capital-normalized equal-agent 결과를 함께 보고한다.
- 초기자산·연령·성별·strategy subgroup은 무작위 요인이 아니며 10억 group이 모두 45세 이상이다. 상호 독립 효과나 실제 human demographic effect가 아니라 constructed-persona descriptive trace로만 보고한다.

### 19.6 독립성·반복

100명이 같은 뉴스·가격·공용 게시판을 공유하므로 100개 독립 실험단위가 아니다. Community ON에서는 interference가 설계 자체에 포함된다.

- agent-turn 수 `N×U`를 iid 표본으로 세지 않는다. 현재 manifest의 9,000은 파생 예시일 뿐이다.
- 같은 agent/date/subturn의 arm 간 paired contrast를 사용한다.
- 반복 seed가 있으면 seed/community world를 cluster로 둔다.
- agent와 date/event는 repeated effect로 처리한다.
- single seed 결과는 seed-specific simulated-world result로 제한한다.
- 첫 45일 RN_COMM_OFF/RN_COMM_ON 한 쌍은 development paired world로 실행 가능하지만 모집단 community 효과의 유의성 근거가 아니다.
- 논문용 robustness는 최소 3개보다 강하게, 권장 5개 이상의 paired world seed를 보고한다. 양측 exact sign-flip에서 이론상 `p<.05`까지 요구하려면 최소 6 pair가 필요하므로 inferential claim의 seed 수는 pilot effect size와 ICC/power simulation으로 확정한다.
- seed별 arm 실행 선후를 교차·균형화하고 pair barrier를 유지해 provider 시간 drift와 treatment를 분리한다.

### 19.7 regime과 heterogeneity

- 하락/상승 날짜 구간은 결과를 본 뒤 바꾸지 않는다.
- community×regime만 이번 실험에서 해석한다.
- persona interaction은 사전에 2~3개만 primary/explanatory로 고정한다.
- 작은 demographic cell은 실제 인간 집단 차이로 일반화하지 않는다.
- multiple comparison과 exploratory/FDR 표기를 구분한다.

### 19.8 일별 target과 evaluator 계약

현재 실제 데이터는 `Date`별 최종 투자자 순거래대금/순거래량만 있으며 intraday time bucket이 없다. 따라서 다음을 고정한다.

- 원 행동 정합성 primary: 100명의 AM+PM actual fill을 합친 일별 signed net **value** 대 같은 날 `Individuals` 최종 순거래대금 방향. 이는 full-day reconstruction이다.
- opening-nowcast diagnostic: 시가 관찰 직후 AM `directional_stance`의 unweighted `buy-sell` 합성 대 같은 날 최종 일별 방향. 오전 수급 label을 예측했다고 표현하지 않는다.
- stance의 initial-capital-weighted 합성, desired-notional 합성, 순거래량 target은 sensitivity다. 10명의 10억 agent가 결과를 지배할 수 있으므로 결과를 본 뒤 primary를 바꾸지 않는다.
- 시뮬레이션·target·fill coverage는 resolved full input date set `D`와 exact equality를 요구한다. 그 뒤 manifest의 frozen burn-in set `B`를 제외한 evaluation set `E`만 primary로 계산한다. 현재 resolved example은 `|D|=45, |B|=3, |E|=42`다. validator의 현재 default `skip_initial_days=5`는 제거하고, `B`의 date ID/hash가 다르면 실행을 거부한다.

Evaluator는 다음 조건에서 fail-closed한다.

- approved full input date set `D`와 actual/simulation/prediction date set이 정확히 일치하지 않거나, frozen `B`/`E` date set·hash와 맞지 않음
- target date 중복, 빈 값, NaN/Inf, 숫자 parse 실패, 예상하지 않은 0/flat label
- fill action이 명시적 `buy|sell` 이외임. 현재처럼 non-buy 전부를 sell로 처리하지 않음
- agent/date stance가 누락·중복되거나 `buy|sell|uncertain` 이외임
- target와 prediction을 교집합으로 조용히 축소함
- skip rule, target column, value/volume primary, aggregation rule이 frozen manifest와 다름

평가기간의 실제 buy 비율을 사용한 `actual_ratio_random`은 test-label prevalence를 본 oracle diagnostic으로만 명명하고 model comparator에서 제외한다. 항상 매수/매도, 사전 seed 10,000회의 50:50 random 분포, 전일 방향, 사전 고정한 AM opening-gap price-only rule을 같은 evaluation set `E`·label·NO_CALL 처리로 비교한다. Target manifest에는 `target_id`, 종목 `005930`, source/API query, 추출·공개시각과 KST, unit, revision policy, raw file hash, exact `D` hash, `B` hash와 exact `E` hash를 기록한다.

### 19.9 AM·PM 체결 합산과 방향 지표의 단일 계약

“AM과 PM 반응을 합친다”는 말은 주문 수를 단순 다수결한다는 뜻이 아니다. **실제로 체결된 수량과 각 체결가격**을 날짜별 순거래대금으로 합산한다.

이 raw-value primary는 **execution-notional-weighted이고 wealth-sensitive한 집계**다. 고정 초기자본 비율을 직접 weight로 곱하는 산식은 아니지만 10억 agent 10명이 초기자본의 52.63%를 가져 더 큰 주문을 낼 가능성이 높다. 따라서 이를 “100명 다수결”이나 고정 capital-weighted 결과라고 부르지 않고 §4.9의 equal-agent/wealth sensitivity를 반드시 함께 둔다.

```text
side(action) = +1 for buy, -1 for sell
daily_net_value[c,d]  = sum(side × filled_quantity × executed_price)
daily_net_volume[c,d] = sum(side × filled_quantity)
predicted_direction[c,d] = sign(daily_net_value[c,d])
actual_direction[d] = sign(Individuals_net_trading_value[d])
```

한 날짜·조건의 expected fill 수는 `N × |Q_d|`이고, arm 전체는 `N×U`다. 현재 manifest에서는 하루 `100×2=200`, 45일 `9,000`이며 첫 3일 burn-in 뒤 현재 metric에는 `100×2×42=8,400`이 대응하지만, validator는 이 숫자를 literal로 쓰지 않고 exact evaluation-date/subturn manifest에서 계산한다. 각 fill은 `(run, condition, agent, date, subturn, stock_code)`가 유일하고 다음을 모두 만족해야 한다.

- `stock_code == 005930`; 빈 stock code를 대상 종목으로 대신 해석하지 않음
- `subturn ∈ {am,pm}`, action은 exact `buy|sell`, `fill_status == filled`, `requested_quantity`와 canonical `filled_quantity`는 양의 정수, `executed_price`는 양의 finite 숫자
- AM executed price는 해당 날짜 실제 시가, PM executed price는 실제 종가와 일치
- requested order가 아니라 **actual fill**만 합산. full-fill invariant로 `requested_quantity == filled_quantity`를 key별 검사하며 rejected/partial/missing fill, fallback row, typo는 sell이나 0으로 바꾸지 말고 run integrity failure로 처리
- 날짜별 expected `N×|Q_d|` fill-key set의 누락·중복, agent의 decision-event 중복, approved date 밖 행, daily-summary와 fill 합계 불일치는 모두 fail-closed. 현재 resolved manifest에서만 하루 200행이다.

순거래대금 방향이 primary고 순거래량 방향은 secondary다. AM과 PM 가격이 다르므로 두 방향이 이론상 달라질 수 있으며, 그 경우 하나를 선택해 숨기지 않고 divergence 날짜를 공개한다. AM-only와 PM-only 합산도 같은 **일별 최종** Individuals label에 비교하는 exploratory diagnostic일 뿐, 시간대별 실제 개인수급 정답이 아니다.

필수 AM/PM 진단에는 AM-only, PM-only, AM+PM 방향과 함께 `AM·PM 방향이 반대인 날짜`, `abs(daily net)/[abs(AM net)+abs(PM net)]` 상쇄율, phase별 signed-value 기여도, signed-quantity 방향, one-direction-feasible/constraint-forced 비율을 포함한다. 이는 왜 당일 합산 방향이 나왔는지 설명하는 분해이며, 셋 중 성능이 가장 좋은 것을 사후 primary로 바꾸는 절차가 아니다.

Primary evaluation-date set `E`의 지표 정의는 하나만 구현한다.

- accuracy: `predicted_direction == actual_direction`인 날짜 비율
- buy recall: 실제 순매수일 중 predicted buy 비율
- sell recall: 실제 순매도일 중 predicted sell 비율
- balanced accuracy: `(buy recall + sell recall) / 2`
- MCC와 `actual × predicted` confusion matrix
- 실제 target이 non-flat인데 agent 일별 합이 정확히 0이면 predicted flat으로 유지하고 **오답**으로 센다. 조용히 nonzero subset에서 빼지 않는다.

현재 `validation/data_trading_value.csv`와 `validation/data_trading_volume.csv` spot-check에서는 45일이 unique·finite·non-flat이고 value 기준 순매수 28일/순매도 17일이다. burn-in 세 날짜는 모두 순매수이며, primary 42일은 순매수 25일/순매도 17일이다. 따라서 같은 42일 always-buy accuracy는 `25/42 = 59.5%`, always-sell은 `17/42 = 40.5%`, 두 baseline의 BA는 각각 0.5다. value와 volume target 방향은 이 45일에서 모두 같지만, source hash가 바뀔 때마다 다시 검사한다.

상관계수는 방향 primary를 대체하지 않는 secondary다. 별도 양의 상수로 나눈 max-abs Pearson과 z-score Pearson은 원 raw Pearson과 수학적으로 같은 정보이므로 세 개를 독립 증거처럼 나열하지 않는다. 누적 흐름 Pearson은 추세·비정상성에 지배될 수 있고, naive Pearson/Spearman p-value·Wilson·iid bootstrap은 날짜 자기상관과 single-world 불확실성을 반영하지 못하므로 exploratory로만 둔다. 규모 비교가 필요하면 agent별 고정 initial capital을 분모로 쓰며 current total value는 post-treatment sensitivity다.

`RN_COMM_OFF`와 `RN_COMM_ON`은 같은 resolved input date set `D`와 burn-in/evaluation mask를 사용한다. evaluator는 각 arm을 따로 출력하는 데서 끝나지 않고 date-level `RN_COMM_ON − RN_COMM_OFF` **gross signed fill value(수수료 전)** 차이, 방향 discordance, paired agent/date/subturn completeness를 함께 만든다. 한 arm에만 누락이 있으면 교집합으로 줄이지 않고 두 arm 모두 결과 산출을 중단한다.

전일 방향·전일 market baseline은 **full input date set `D`에서 lag를 먼저 만든 뒤** evaluation mask `E`를 적용한다. 현재 example의 첫 평가일 2026-03-05에는 2026-03-04의 관찰 가능 값이 있어야 하며, 잘린 `E` 배열에서 shift해 첫 값을 임의 0으로 만들지 않는다. evaluator는 raw target file을 직접 strict-parse하고, 이미 교집합·skip 처리된 `daily_comparison_*.csv`를 다시 정답 원천으로 사용하지 않는다.

RQ2의 continuous paired outcome은 agent별 초기자본을 먼저 분모로 고정한 뒤 평균한다. `AM+PM`은 fill row 200개를 바로 평균하지 않고, **같은 agent의 AM·PM signed notional을 먼저 합산**한 하나의 daily agent value로 만든다.

```text
agent_signed_notional[c,d,AM] = side(AM) × filled_quantity(AM) × executed_price(AM)
agent_signed_notional[c,d,PM] = side(PM) × filled_quantity(PM) × executed_price(PM)
agent_signed_notional[c,d,AM+PM] = agent_signed_notional[c,d,AM] + agent_signed_notional[c,d,PM]
imbalance[c,d,k] = mean_agent(agent_signed_notional[c,d,k] / fixed_initial_capital[agent])
community_effect[d,k] = imbalance[RN_COMM_ON,d,k] - imbalance[RN_COMM_OFF,d,k]
```

여기서 `k ∈ {AM, PM, AM+PM}`이고 RQ2 primary는 `k=AM+PM`, 같은 evaluation mask `E`다. pair key는 `(agent_id, date, subturn)`이며 두 arm의 exact pair가 확인된 뒤에만 agent-day 합과 condition 차이를 만든다. 정확도 차이는 날짜별 paired correctness로 보고 McNemar는 single-world descriptive diagnostic으로만 사용한다.

### 19.10 validator와 report의 단일 계약

현재 evaluator는 `evaluator_contract.json`, `resolved_study_manifest.json`, `RUN_FINALIZATION.json`, `traces/final_fill_export_index.json`, `traces/rn_comm_off_final_fill_ledger.csv`, `traces/rn_comm_on_final_fill_ledger.csv`, evaluator-only target registry를 명시적으로 받는다. report는 이 검증 결과와 hash-indexed community sidecar만 사용한다. legacy comparison/PDF script의 고정 condition명·20/30/50-agent·고정 날짜·전역 DB fallback·교집합 date/agent 축소는 새 baseline에서 허용하지 않는다.

그래프·PDF를 만들기 전에 아래를 fail-closed로 검증한다.

- arm별 exact manifest key set에서 committed STB = committed LTB update = decision = filled logical event 수
- AM/PM full-fill, stock/action/price/fee, duplicate/missing fill, `fill_t` lineage
- current fill은 same-turn STB/analysis/decision에는 0이고 post-fill LTB transaction episode에는 정확히 1회이며, 미래 next-turn/D+1/D+5 outcome은 same-turn LTB에 들어가지 않았음
- `RN_COMM_OFF`의 post/read/Best/exposure/claim/evidence가 모두 0이고, `RN_COMM_ON`은 manifest 권한·next-AM visibility·full-body Best 정책과 일치함
- event별 target real-news 10개, selected=serialized=delivered-real count/payload hash equality, actual count/shortage exception, fake 0개, source/provenance hash 일치. target 미달은 frozen non-blocking shortage record와 pair equality가 있을 때만 허용
- reasoning request/returned metadata/reasoning token이 strict-off policy와 일치
- RN_COMM_OFF/RN_COMM_ON의 cohort·calendar·news·price·prompt·model·seed·fee·burn-in hash가 같고 `community_mode`만 다름

보고서는 최소한 다음 다섯 축을 분리해 제시한다.

1. run integrity·API/resume·reasoning-off·news provenance
2. 일별 AM+PM gross signed fill value 대 삼성전자 실제 개인 순거래대금 방향
3. STB/LTB lineage와 `dim_6` reflection maturity/right-censoring
4. RN_COMM_ON의 full-body exposure → claim → STB → decision 연결
5. fee-free execution/portfolio diagnostics와 legacy compatibility export 대조

좋아요·싫어요·게시 수 또는 사후 선택한 대표 agent만으로 community가 거래를 바꿨다고 서술하지 않는다. depth별·수익률별 결과는 기술통계로 표기하고, primary 3-trading-day burn-in/date mask와 AM+PM aggregation은 manifest에서만 파생한다.

---

## 20. 데이터 누출·편향·자기확증 방지

### 20.1 미래 누출

자동 test가 다음을 검증한다.

- AM이 당일 PM 뉴스·종가·post-PM community를 보지 않음
- PM이 post-PM community를 보지 않음
- Depth 2 search가 decision cutoff 뒤 기사를 보지 않음
- memory evidence의 source event가 현재 cutoff 이전임
- outcome feedback은 maturity price 관찰 전에는 존재하지 않음
- 실제 개인투자자 flow가 prompt/memory에 없음
- `information_mode=pre_close_cutoff`가 manifest·run signature에 고정되고, AM market feature 각각의 `as_of`가 forecast origin을 넘지 않음. 현재 코드의 `same_day` mode는 close만 시가로 바꿔도 volume/pct change/MA 등에 당일 미래값이 남을 수 있어 금지
- 실제 뉴스 원문도 semantic leakage scanner를 통과함. AM payload에서 같은 날 삼성전자/005930의 개인·개미 순매수·순매도, 장중 부분수급, 확정 수급을 직접 알려주는 문구가 발견되면 자동 통과시키지 않고 원 기사 시각·대상 종목·언급 기간을 사람이 확인해 reject/mask/allow 사유를 남김
- target CSV 파일명·경로·열 이름·정답 부호·집계 결과는 runtime code, prompt, STB/LTB, response journal request payload와 완전히 다른 evaluator namespace에 둠
- 뉴스는 publication timestamp만 보지 않고 archived content version·observed/last-modified 시각을 검증
- AM/PM title/body/summary와 Depth 2 search 모두 cutoff-impossible “마감·최종 고가·최종 수급” scan
- 현재 확정 반례 `news_20260427_섹터_0032`는 09:11 timestamp인데 당일 삼성전자 224,500원 마감과 최종 개인 순매도를 포함하므로 그대로는 승인 불가
- `fake=0`은 시점 안전성의 대리변수가 아니다. structural fake-marker gate와 article-version/as-of gate를 각각 통과해야 함

### 20.2 자기확증 loop

- previous LTB는 prior state로 표시하고 새 외부 evidence count에서 제외
- `belief_summary`와 change summary는 사람용이며 모든 agent-visible 입력에서 제외. `view_change`는 LTB scientific state와 분리된 결정론적 human trace이고, **post-writing private context에서만** `LTB_t`·current `fill_t`와 함께 허용된다. STB/LTB·analysis·decision·community interpretation에는 제외한다.
- current STB는 거래에 직접 한 번 쓰고, 거래 뒤 post-fill LTB에 한 번 재귀 통합하되 그 새 LTB는 같은 거래의 belief block으로 다시 쓰지 않음
- current fill은 STB/analysis/decision에는 보이지 않지만 same-turn post-fill LTB에 structured transaction episode로 한 번 들어간다. 그 가격 성과만 다음 eligible price-outcome packet까지 보이지 않음
- support와 contradiction을 함께 저장
- repeated summary 자체를 독립 support count로 세지 않음
- 같은 root article/repost 중복 cap
- LTB 문장의 certainty 표현이나 transition 강도는 새 independent root evidence 없이 반복 갱신만으로 강화할 수 없음
- 자신의 자유문장 decision/action reason을 STB/LTB 입력에 재사용하지 않음
- 자기 작성 Best 재노출은 `self_echo`로 분류하고 외부 support를 증가시키지 않음

### 20.3 persona·자본 편향

- 10억 agent 10명은 인원 10%지만 초기자본 52.63%이므로 raw signed value를 지배할 수 있다.
- 10억 group이 모두 45세 이상이어서 wealth와 age가 구조적으로 confounded된다.
- big influencer가 매우 적은 cell이면 결과가 한 agent에 좌우될 수 있다.
- age/gender/strategy/behavior traits는 독립 무작위 요인이 아니다.
- persona prompt의 demographic stereotype 가능성을 별도 audit한다.
- raw-value primary와 equal-agent initial-capital-normalized, 1억(=rich-excluded alias)/10억 stratified, leave-one-rich-out 결과를 구분한다.
- constructed fixed-slot persona이므로 human demographic representativeness나 demographic causal effect를 주장하지 않는다.

### 20.4 treatment integrity

- 두 arm은 같은 resolved immutable real-news bundle을 참조하고 file/public-row/ordered-ID/slot-map hash가 동일
- marker 0뿐 아니라 visible/read/search/influential/evidence/journal external-news root가 clean registry에 닫혀 있음
- `RN_COMM_OFF` community exposure 0
- `RN_COMM_ON` posting/selective-read/reaction은 Depth 1·2에서만. non-empty Best이고 연구기간 내 다음 AM이 있을 때에만 Best exposure가 100명 모두의 interpretation phase에서 생성되며, empty Best/final-day schedule의 next-AM Best broadcast/exposure는 0
- `RN_COMM_ON`의 post-PM content는 next AM STB 이전 belief link 0
- memory policy와 reasoning policy hash 동일

---

## 21. 테스트 계획

### 21.1 unit tests

- exact 100 agent selection
- `fixed_slots.csv`↔`sys_100.db`의 ID/gender/age/age_group/initial-cash 100/100 exact match, 1억 90·10억 10·total 190억
- frozen `agent_id → news_depth` map과 prompt-depth가 **100/100 exact match**이고, canonical `sys_100.db`의 `30/55/15` 분포·각 agent의 information permission 문구·cohort manifest hash가 모두 일치
- persona prompt의 gender/age/location/strategy/user-type/cash/behavior fields와 DB mismatch 0
- `render_persona_v1`의 NFC·LF·trailing-LF·section-order·escaping 규칙과 canonical parse/round-trip 100/100, DB↔prompt 모든 의미 field mismatch 0, depth 외 structured value 변화 0, ordered `(agent_id,prompt_sha256)` map hash 재현
- turn-0 belief·portfolio 100행
- STB/LTB ID deterministic generation, manifest-derived STB `U` rows와 `LTB_0 + U updates` parent chain
- same-payload replay와 different-payload conflict
- visibility/as-of filter
- real-news bundle exact allowed schema·file/canonical-row/ordered-ID/date/slot hash 검증과 runtime ID→immutable source URL/version 1:1 복원
- fake-off에서 marker 없는 title/summary 변경, `synthetic_id`-only row, private injection metadata 잔존, known injection row-hash overlap을 모두 reject
- selected article의 body/summary에 target-date EOD close·high·최종 investor-flow fixture가 있으면 cutoff/version validator가 quarantine
- `belief_summary`를 변조해도 모든 agent-visible request bytes/hash가 불변. `view_change`를 변조해도 STB·LTB·market-analysis·decision·community-interpretation request bytes/hash는 불변이며, 결정론적 post context validator만 변조를 reject
- `ltb_scientific_hash`는 human field를 제외하고 `human_log_hash`와 분리되며 renderer code hash/golden-byte fixture 일치; summary/change 변조가 LTB ID·parent·cache·scientific digest를 바꾸지 않음
- STB prompt에는 current news/validated-community typed evidence만 있고 previous STB/LTB·summary·market/portfolio·past/current fill은 없음
- current fill fact는 STB/analysis/decision에 0이고 same-turn post-fill LTB의 structured decision/fill episode에 정확히 1회, 각 horizon record는 자기 maturity turn에서 정확히 1회만 소비되고 이후 재삽입 0; portfolio snapshot은 STB가 아닌 direct execution-state block에만 존재
- LTB prompt에는 allowlist된 previous LTB six dimensions, current STB six dimensions, **current structured decision/fill episode**, eligible earlier price-outcome packet, raw payload가 제거된 sanitized evidence registry, frozen persona hash/profile만 있고 그 밖의 causal field는 없음
- LTB output의 `integration_evidence[dim_i]` support/contradict는 같은 dimension의 STB evidence 또는 허용된 trade-reflection evidence의 부분집합이며, community ID는 `community_claim_id → claim_sources → source_exposure_ids → distinct root_post_ids`로 resolve되고 과거 provenance는 parent-LTB chain으로만 resolve됨
- persona output key 생성·수정·evidence 인용은 reject하고 phase마다 persona profile/map hash가 run bundle과 다르면 호출 전 pause
- market-analysis/decision belief block에는 previous LTB와 current STB의 six dimensions가 각각 이름 있는 block으로 있고 summary/change·raw news/community는 없음
- market-analysis evidence reference는 `previous_ltb_id:dim_i`, `stb_id:dim_i` 또는 typed execution-state ID만 허용되고 hallucinated historical evidence/root ID는 reject
- community next-AM boundary
- Depth 0은 posting/selective-reading/reaction 0이며, non-empty Best day에는 다음 AM community-interpretation 단계에서 Best 5를 수신. empty day에는 call/exposure 0
- same-phase isolation
- root/source dedup cap
- H1/H5 maturity와 end censoring
- reasoning object가 모든 stage request에 존재
- 모든 physical attempt의 final request에 reasoning object 존재; 성공 response에만 exact returned model/provider·reasoning telemetry와 token=0 요구
- max_tokens와 schema length enforcement
- LTB의 여섯 dimension은 모든 turn에 parent와 다른 content hash로 새로 작성되고, 근거 없는 material assertion 삭제·반전과 byte-identical copy는 reject
- LTB model output에 summary/change·action enum이 extra key로 오면 reject하고, committed human summary/change는 final server-applied six dimensions·parent/current hash·integration evidence에서 deterministic render되어 일치
- 새 evidence가 없는 20회에도 여섯 dimension의 새 hash가 생성되며, parent material assertion의 protected sentinel이 근거 없이 손실·반전되지 않음
- short/current-`U`/extended-length protected-claim fixture에서 수정 대상이 아닌 externally marked exact sentinel span의 byte 손실·변화 0; 한 건이면 NO-GO
- stable-item table/retriever, vector/top-K/decay, standalone reflection call이 모두 0이며 drift failure 때 자동 fallback하지 않고 pause
- 자신의 자유문장 action reason 또는 self-authored Best만으로 LTB support/confidence가 증가하지 않음
- current fill ID를 STB/analysis/decision 또는 `integration_evidence`에 주입하면 reject하고, post-fill LTB의 mandatory `transaction_episode`에서 빠져도 reject; future maturity ID는 current LTB에서 reject하며 eligible earlier outcome ID만 `dim_6` price-outcome evidence로 허용
- 정상 필수 key와 악성 extra key를 함께 반환하면 모든 stage에서 reject
- future/타 agent/타 condition evidence와 hallucinated evidence ID reject
- resolved `D` input→frozen `B` burn-in→exact `E` metric evaluator, duplicate/missing/malformed target과 invalid action/status/price/quantity/fill key fail-closed. 현재 45→3→42는 별도 golden fixture로만 유지
- `wealth_sensitivity_v1` synthetic fixture에서 1억-only와 rich-excluded alias byte-equality, 10회 leave-one-rich-out, initial-capital-normalized agent-first aggregation과 `wealth_fragile` flag가 expected 값과 일치

### 21.2 integration tests

- 2 agents×2 days offline full cycle
- 최소 6거래일 synthetic/offline full cycle로 H5 maturity·correction·censoring 검증
- 1 agent의 short/current-`U`/extended-turn worst-case LTB roll-forward로 cap·drift·핵심 주장 보존 검증
- 100 agents×1 day offline `RN_COMM_OFF`
- 100 agents×1 day offline `RN_COMM_ON`
- live reasoning-off stage canary
- live JSON Schema canary for every stage
- injected single-agent failure 후 journal replay
- process kill at AM/PM/community 각 지점 후 resume
- uninterrupted run과 resumed run의 STB/LTB/phase digest 동일
- condition DB/cache namespace isolation
- RN_COMM_OFF/RN_COMM_ON이 동일 resolved real-news bundle object와 hash를 쓰며 child argv의 유일한 차이가 community mode인지 parent pair gate로 검증
- preflight 뒤 news file swap·symlink·mtime/content drift를 다음 physical API attempt 전에 reject
- visible/read/search/influential/evidence/journal external-news ID의 clean-registry closure와 fake/private metadata 0을 최종 검증
- plain runner·six-condition default·legacy fake restart/resume·offline full run은 paper manifest registration 단계에서 reject
- `RN_COMM_OFF` no-op community checkpoint resume
- **선택되지 않은 Best-only fixture**의 본문 끝 sentinel은 같은 PM trading/selected-read/reaction request에서 0명, 다음 AM에는 정확히 100명의 community-interpretation request에서 발견되고 STB/LTB request에서는 0명. 별도 selected-body fixture는 PM selected-read/reaction과 다음-AM interpretation 두 소비 edge, root support 1개를 검증. selected+Best overlap fixture는 next-AM body serialization 1회, `source_exposure_ids` 2개, claim-source junction 2개, consumption edge 총 3개(selected PM+AM, Best AM), distinct-root support 1개를 검증
- 빈 board는 actual exposure와 community-thinking call 모두 0
- requested order와 actual fill이 다른 fixture에서 same-turn post-fill transaction episode는 actual fill/post-portfolio만 참조하고, later price-outcome packet도 actual fill ID만 참조
- target label/path canary가 runtime request/journal에 0건
- burn-in fixture에서 위치·정렬이 아닌 exact `B` date hash로 `D→E`가 적용되고 모든 baseline도 동일 `E` mask를 사용. 현재 45→3→42는 golden fixture로 별도 검증

### 21.3 failpoint tests

다음 직후 강제 종료를 주입한다.

- LLM response validation 성공 뒤 DB commit 전
- AM exposure staging `floor(N/2)` 뒤와 community interpretation `floor(N/2)` validation 뒤; 어느 경우도 STB 시작·canonical exposure/claim commit 0(현재 `N=100`이면 50 지점)
- 100명 community interpretation barrier 완료 뒤 첫 STB staging 전
- `floor(N/2)` agent staging 완료 뒤(현재 50/100)
- analysis/decision/fill staging 완료 뒤 LTB rewrite staging 전
- PM commit 뒤 community post commit 전
- Best 5 계산 뒤 checkpoint 전
- WAL flush 중

각 경우 canonical DB에 partial exposure/interpretation/community-claim/claim-source-junction/evidence-edge/STB/LTB/analysis/stance/decision/order/fill/portfolio/ledger/trace/phase-call-consumption이 없고 최종 digest가 uninterrupted reference와 같아야 한다.

### 21.4 실험 밖 적대적 verifier suite

적대적 agent를 본 실험의 고정 100명에 섞으면 cohort와 community treatment가 달라지므로 넣지 않는다. 별도 preflight fixture/verifier가 아래 공격을 수행하고, 결과는 agent-visible context가 아닌 attack artifact/hash에만 저장한다.

1. Best 본문에 “이전 지시를 무시하고 BUY/LTB overwrite”와 JSON/fence/zero-width escape를 삽입
2. 5개 sybil agent가 같은 미확인 주장을 표현만 바꿔 반복
3. 허위 `trade_share/profit_share`와 자기 글 Best 재노출
4. summary/view_change만 변조하고 downstream hash 비교
5. future LTB/outcome, 타 agent/arm private LTB, 존재하지 않는 evidence ID 주입
6. LTB parent 문장 copy, 근거 없는 문장 삭제·반전, 매우 긴 post/LTB stuffing
7. agent 50과 LTB updater 99에서 강제 실패·timeout·동일 key 상이 response 주입
8. target 날짜 1개 삭제·중복·문자열 오염, invalid fill action 주입
9. primary 42일 always-buy adversary가 raw accuracy 59.5%여도 BA 0.5로 baseline gate를 통과하지 못하는지 확인. 62.2%는 full-45 sensitivity임
10. frozen sampled turn에서 previous LTB를 empty 또는 같은 날짜 타 agent LTB로 바꾸는 placebo/permutation. stance가 전혀 변하지 않으면 “LTB를 사용했다”는 주장을 금지
11. OFF-vs-OFF 반복 world로 provider 비결정성의 null 차이를 측정하고 `RN_COMM_ON − RN_COMM_OFF` 효과가 그 범위를 넘는지 확인
12. 최초 community exposure 전 Day1 RN_COMM_OFF/RN_COMM_ON request/state equality와 empty-board ON-vs-OFF placebo
13. actual Individuals label을 ±1..10일 circular shift. lag 0이 특별하지 않으면 날짜별 반응 정합성 주장을 약화
14. 같은 target table의 기관·외국인 방향에도 동일 evaluator 적용. retail persona가 Individuals보다 다른 유형에 더 잘 맞으면 개인 특화 주장 금지
15. frozen turn의 news 본문 masking/date permutation. 출력이 불변이면 뉴스반응 mechanism 주장 금지
16. Day1과 one-direction-feasible turn을 제거한 constraint-neutral sensitivity에서 정합성 유지 여부 확인
17. 실행한 모든 world seed를 integrity failure 외에는 제외하지 않고 seed cherry-picking 탐지
18. 오전 timestamp 본문에 당일 종가·최종 고가·최종 수급을 넣어 quarantine되는지 검사
19. 필수 key와 함께 `target_label`, `system_override`, `ltb_overwrite` extra key를 반환해 exact-schema reject 확인
20. STB sentinel이 LTB에 흡수된 뒤 downstream root support count가 정확히 1인지 확인
21. 승인 baseline 한 row의 marker 없는 title/summary mutation과 injection marker만 제거한 row를 주입해 preflight reject 확인
22. daily 거래일/ID 하나를 삭제해 `actual∩news` 교집합이 44일로 줄어들어도 exact date gate가 metric 생성 없이 reject하는지 확인
23. RN_COMM_OFF/RN_COMM_ON news hash 하나만 바꾸거나 preflight 뒤 file swap하여 pair/phase gate가 reject하는지 확인
24. `news_20260427_섹터_0032`와 같은 09:11 당일 종가·최종 개인수급 payload가 structural fake=0 검사와 별개로 quarantine되는지 확인
25. old fake journal/DB/log와 clean registry 밖 search/influential ID가 real-only namespace 및 final integrity에서 reject되는지 확인

모든 공격이 기대한 거부·pause·불변성 결과를 내야 P0를 통과한다. Sybil 반복은 community consensus 변화로는 측정할 수 있지만 외부 사실 truth confidence를 높이지 않는다.

### 21.5 구현 파일별 적대적 검증 절차

구현 후 “정상 예시가 실행된다”는 것만으로 완료 처리하지 않는다. 아래 파일군은 변경 diff와 테스트 fixture를 서로 다른 적대적 reviewer가 대조한다. reviewer는 구현자와 독립적으로 **누락·시간역전·legacy fallback·로그 불일치·과장된 report 문구**를 찾으며, 발견 사항은 `RUN_RECORD.md`의 verifier 결과와 issue list에 남긴다.

| 파일군 | 적대적 반례 | 통과 기준 |
|---|---|---|
| launcher/scheduler/manifest | 45→확장기간, AM/PM 하나 제거, 99/101명, arm argv/news hash 하나 변경 | hardcoded count/교집합 축소 없이 manifest-derived key set에서 fail-closed |
| news/provenance | unsafe 기사·marker 없는 변조·9개 slot·duplicate·file swap | target real 10을 우선 재선정. 부족한 9개 slot은 actual count/reason/ID hash가 frozen shortage exception이고 pair hash가 같을 때만 계속 가능; immutable version/cutoff·duplicate/synthetic 금지는 유지 |
| LLM client/API audit | `exclude`만 사용, fallback/provider drift, missing/nonzero reasoning telemetry, timeout retry | final request `effort:none`, pinned provider, token 0; response가 있던 strict-off 위반은 pause |
| STB/LTB/prompt | current fill의 pre-fill 주입 또는 post-fill 누락, future outcome 주입, summary/view_change 재주입, parent copy/삭제, dim6 외 가격성찰 | two-block trade input, **decision/fill 뒤 post-fill LTB**, six-dimension lineage, dim6-only transaction/outcome reflection |
| exchange/portfolio/trade log | fee policy drift, requested/fill 불일치, pre/post cash mismatch, H1/H5 time reversal | `commission_rate=0`, `sell_tax_rate=0`, `fee_amount=0`과 fill/portfolio/export 일치, outcome observed event gate |
| community/posting/log | Best 제목만 전달, 본문 hash 누락, Depth0 완전 제외, post context/fill claim 혼동 | full-body exposure trace, passive Best5 audience, public claim과 actual fill ledger 분리 |
| validator/PDF/report | legacy C00/C10/fake wording, skip=5, global DB fallback, 30/50-agent description, date intersection | RN_COMM IDs, manifest 3-day burn-in, run-scoped snapshot only, integrity pass 전 report 생성 금지 |
| checkpoint/resume | kill after response/AM/PM/community, duplicate physical call, stale DB/journal | durable journal replay, atomic phase, same digest as uninterrupted run |

적대적 reviewer가 구현 중 새로 발견한 설계 선택지가 사용자의 실험 의미를 바꾸거나 외부 권한을 요구하면, 임의로 결정하지 않고 이 대화에서 즉시 질문한다. 단순 코드 결함·누락 로그·명백한 기존 계약 위반은 이 설계의 P0에 따라 수정 대상으로 기록한다.

---

## 22. 실행 전 Go/No-Go gate

### P0: 하나라도 실패하면 유료 호출 금지

- [ ] branch와 commit이 승인된 값과 일치
- [ ] working tree의 실험 code/prompt/input diff를 snapshot·승인
- [ ] external approval record의 P2-required input/treatment 값이 sealed manifest에 고정되고 runtime config/manifest에 `<required>`, `<freeze...>`, `pending`, `TBD`, null placeholder 0
- [ ] authoring `StudySpec`은 canonical registry/policy만 포함하고 derived count/date field를 거부; resolver output의 `N/A/D/U/B/E/P`, exact decision/news/community/maturity key-set hash와 expected row/call count가 생성됨
- [ ] launcher·scheduler·seed derivation·checkpoint·run integrity·evaluator·report가 모두 같은 `resolved_study_manifest.json` SHA-256을 사용하며 자체 `×2`, date intersection, first-N cohort, implicit skip/default count 산식 0
- [ ] 100 agent IDs 정확히 일치
- [ ] `fixed_slots.csv`와 DB의 gender/age/age-group/initial-cash 100/100 exact match, null·duplicate source 0, persona/source hash freeze
- [x] depth 정책 30/55/15 확정(2026-07-22 사용자 확인)
- [x] 원 설계의 Depth 0 Best5-only 규칙 유지(active 70명, Best 5 audience 100명)
- [ ] frozen canonical `agent_id → news_depth` map을 `sys_100.db`에서 추출해 hash-pin하고, DB depth·re-rendered prompt depth/정보권한 문구·report·config가 **agent ID별 100/100** 일치 (단순 30/55/15 분포 일치는 불충분)
- [ ] `render_persona_v1` canonical parse/round-trip 100/100, DB↔prompt의 gender/age/location/strategy/user-type/cash/behavior/depth mismatch 0, depth 외 structured value 변화 0, ordered agent-prompt map hash 고정, 두 arm agent별 prompt byte identity
- [ ] initial cash 1억 90명/10억 10명, rich IDs exact match, total initial cash 190억과 10억 group share 52.63%, cash-only portfolio 확인
- [ ] `wealth_sensitivity_v1` evaluator fixture·alias equality·leave-one-rich-out·fragility flag test pass
- [ ] reasoning-off가 모든 stage와 transport/schema retry의 모든 physical attempt에 중앙 강제 적용되고 호출자 override 불가
- [ ] live canary에서 매 physical attempt의 reasoning request object 존재; 성공 response의 reasoning field empty, reasoning tokens 0, telemetry 누락 0
- [ ] provider/model pin과 fallback off
- [ ] paper entrypoint는 전용 real-news 2-arm launcher 하나뿐이며 plain runner·six-condition default·legacy fake restart/resume·offline stub 경로는 study manifest 등록 불가
- [ ] 승인된 `real_news_bundle_manifest`의 raw snapshot/provenance sidecar·processed·daily exact schema/file hash/canonical row hash/ordered clean-ID registry/event-to-news-slot map/selection seed·algorithm version이 모두 일치. current resolved example은 `D=45`, `U=90`, `target_real_news_articles_per_event=10`, `target_article_slots=900`이며 각 event는 actual count·shortage status와 ordered ID/hash를 가진다. 10개 미만은 허용된 shortage exception으로만 존재하며 pair에서 동일해야 한다.
- [ ] fake rows/synthetic IDs/private fake metadata 0이며 marker가 아니라 승인 clean bundle exact identity로 판정; 발견 시 filtering 후 계속하지 않고 fail
- [ ] RN_COMM_OFF/RN_COMM_ON은 동일한 resolved read-only bundle을 참조하고 processed/daily/public-row/ID-registry/slot-map hash가 pair gate에서 동일
- [ ] 모든 visible/read/search/influential/evidence/journal external-news ID가 clean registry의 부분집합이고 known bearish/bullish injection ID·title·row-hash overlap 0
- [ ] preflight 뒤와 매 phase API 전 news bundle re-hash가 동일하며 file swap·symlink·TOCTOU test가 거부됨
- [ ] clean base에 turn 0 LTB·portfolio만 각 100행, STB 0행; approved allowed-table schema와 LTB₀ scientific digest 일치. 0-byte/stale/자동 rebuild base 금지
- [ ] launcher command, manifest, invariant 모두 agent_count 100이며 두 child argv diff allowlist는 community mode 하나뿐
- [ ] STB input은 current news/validated-community typed evidence만 전달. LTB input exact allowlist는 previous LTB six dimensions + current STB six dimensions + **current structured decision/fill episode** + eligible earlier price-outcome packet + sanitized evidence registry + frozen persona hash/profile이며 raw source/community payload와 그 밖의 causal field는 0
- [ ] current fill fact는 same-turn post-fill LTB transaction episode에서 1회, `next-turn`/H1/H5의 각 `(fill_id,horizon)`은 due event에서 1회만 price-outcome packet으로 소비되고 이후 재삽입 0; market/portfolio는 STB가 아닌 direct execution-state block에만 존재
- [ ] LTB integration evidence는 같은 dimension의 STB evidence 또는 `dim_6`에서 허용된 earlier price-outcome evidence의 부분집합이며, current transaction episode는 별도 non-evidentiary context이고 community claim→claim-source junction→reader source exposures→distinct root posts FK chain이 완결
- [ ] market analysis·decision은 previous LTB와 current STB `dim_1~dim_6`의 분리 allowlist를 쓰며 summary/change·raw news/community sentinel 유입 0; post-decision posting은 stage-qualified `LTB_t`만 허용
- [ ] `ltb_scientific_hash`와 `human_log_hash` 분리, renderer code/golden bytes 고정, human field 변조 시 causal ID·parent·cache·digest 불변
- [ ] non-empty Best fixture/day에서 Best 5의 ID·제목·유형·점수와 frozen 원문 본문은 100명의 next-AM **community-interpretation request**에 삽입되고, STB request의 raw-body sentinel은 0
- [ ] Depth 0은 Best5 원문만 받고 posting/selective-read/reaction과 일반 글 본문은 받지 않음
- [ ] 모든 expected decision-turn key에서 committed STB와 LTB update가 1:1이고 `count(STB)=count(LTB update)=U`; previous-LTB/current-STB Decision-Making → actual fill → same-turn post-fill LTB → next-event visibility tests pass
- [ ] short/current/extended·AM-only/AM+PM·불균일 event·후속 축소 cohort property fixture에서 exact key/count가 자동 재계산되고, 현재 paper run은 required 100 registry 밖 수로 시작/resume할 수 없음
- [ ] AM exposure·interpretation·community claim·claim-source junction·evidence edge·STB·LTB·analysis·stance·decision·order·fill·portfolio·ledger·trace·phase-call-consumption을 staging한 뒤 exact cohort `N=100` 전부를 한 scientific transaction으로 commit; interpretation/agent `floor(N/2)`와 LTB `N-1` failpoint에서 canonical DB 무변경
- [ ] `information_mode=pre_close_cutoff` 고정과 AM market feature field-level as-of 검사
- [ ] resolved full date set `D`, frozen burn-in set `B`, evaluation set `E`의 exact hash와 strict numeric/duplicate/action/status/price/fill/date evaluator·AM stance evaluator 통과. 현재 45/3/42는 `current_manifest_golden` fixture에서 별도 확인
- [ ] 뉴스 URL·source·published/observed/scraped/last-modified/raw-version/body·summary hash와 summary 생성/절단 provenance 확보, runtime ID→immutable raw version 1:1 복원
- [ ] AM/PM/search semantic leakage audit와 blinded human review 완료
- [ ] `news_20260427_섹터_0032`의 09:11 payload에 당일 종가·최종 개인수급이 포함된 확정 반례 및 나머지 scanner 후보가 reject/mask/quarantine manifest에 반영
- [ ] 모든 LLM output exact key set·unknown-key rejection과 untrusted full-body injection canary 통과
- [ ] stable-item/retriever/vector/decay/standalone-reflection table·call 0, short/current-`U`/extended protected exact-sentinel drift 0; 실패 시 자동 fallback 없이 pause
- [ ] `RN_COMM_OFF` cross-community contamination 0
- [ ] response journal crash/resume digest test pass
- [ ] API budget, cost cap, disk cap 설정

### P1: 별도 resolved canary spec 통과 뒤 본 실행 판단

- [ ] canary도 본 cohort exact 100명을 모두 쓰며, 현재 승인 후보인 2거래일의 exact event map을 별도 canary spec으로 resolve
- [ ] RN_COMM_OFF/RN_COMM_ON 모두 같은 canary cohort·calendar/event·news hash로 live 실행
- [ ] arm별 committed STB와 LTB update가 각각 `N_c×U_c`이고 exact key set 동일. 현재 100명·2일·AM/PM candidate를 resolve하면 `U_c=4`, 각각 400; LTB parent/next-event visibility와 same-turn decision/fill episode link·later outcome link 완결
- [ ] latency p50/p95/p99 기록
- [ ] tokens/cost per stage와 projected total 기록
- [ ] schema/transport retry 비율 threshold 이하
- [ ] DB/log/WAL 증가량을 event/agent/stage 단위로 측정해 full resolved `N/D/U` storage 추정
- [ ] 첫날 PM community/Best5가 둘째 날 AM에만 실제 노출되는지 감사
- [ ] 각 previous LTB + current STB→same-turn Decision-Making/actual fill→same-turn post-fill LTB→next-event visibility와, AM/PM fill의 later price-outcome maturity visibility 감사
- [ ] memory packet byte/token cap 적절
- [ ] no missing/duplicate agent-stage key

### P2: 본 실행 직전 freeze

- [ ] preregistration 문서 hash
- [ ] study manifest hash
- [ ] external approval record의 모든 P2-required field가 명시값이며 placeholder/pending/null 0; canary에서 정할 값도 P2 전에 freeze
- [ ] code/prompt/dependency/base/persona/news hash
- [ ] model/provider metadata snapshot
- [ ] primary outcomes와 analysis script hash
- [ ] `wealth_sensitivity_v1` code/config/golden-fixture hash
- [ ] max budget와 pause thresholds
- [ ] paired scheduling order와 supervisor 설정

### P3-A: base run의 기술·해석 타당성

아래는 “community 효과가 통계적으로 유의했는가”만으로 판단하지 않는다. 가짜뉴스를 얹기 전에 base가 **해석 가능한 실험 플랫폼**으로 작동했는지 확인하는 gate다.

- [ ] RN_COMM_OFF/RN_COMM_ON 모두 manifest의 `D` 거래일·`U` decision turns·`N` agents 완결; 현재 study 해석값은 45·90·100이며 duplicate/missing turn 0
- [ ] 실제 뉴스·가격·cutoff·model·reasoning-off·STB/LTB run signature가 arm 간 일치
- [ ] `RN_COMM_OFF` community exposure/memory 0, `RN_COMM_ON`의 실제 exposure lineage 완결
- [ ] agent·arm별 `STB update count = LTB update count = U`이고 exact `(date,subturn)` key가 1:1이며, LTB state는 initial `LTB_0` 포함 `U+1`
- [ ] 각 subturn의 current external evidence→STB + previous LTB→`directional_stance`→feasible action/actual fill→same-turn post-fill LTB→next-event visibility trace가 hash로 재구성 가능
- [ ] previous LTB와 current STB만 same-turn 거래에, current fill은 post-fill LTB transaction episode에만 즉시 보이고 미래 price outcome은 next eligible packet에서만 보이는 stage/turn visibility 완결
- [ ] 표본 감사에서 `unexplained_reversal` 0 또는 모든 flag의 원인·영향을 공개하고, 방향 예측 payload에 개인 순매수 label·미래 정보가 없음을 검증
- [ ] 삼성전자 실제 개인 방향의 원 체결 정합성과 AM stance 예측 진단을 구분해 45일 input coverage와 42일 primary balanced accuracy·혼동행렬을 RN_COMM_OFF/RN_COMM_ON별 동일 evaluator로 산출
- [ ] STB/LTB/주문/portfolio checkpoint의 resume digest가 일관
- [ ] 사전등록한 primary/secondary outcome 및 null 결과까지 보고
- [ ] base를 본 뒤 prompt·memory·cohort·기간을 변경하지 않겠다는 frozen-extension manifest 승인

### P3-B: baseline 일별 개인 거래반응 정합성

사용자 확정에 따라 primary는 **100명의 AM+PM actual fill을 날짜별로 합산한 방향 대 같은 날 삼성전자 개인 최종 순거래대금 방향**이다. 기술적으로 완주했다는 사실만으로 “개인 반응을 잘 재현한다”고 판정하지 않는다.

- [ ] actual class balance와 always-buy/always-sell 성능 공개
- [ ] `wealth_sensitivity_v1`의 raw, initial-capital-normalized, 1억-only(=rich-excluded alias), 10억-only, leave-one-rich-out 방향·alias equality·rich contribution 공개; `wealth_fragile=false`가 아니면 robust baseline/community claim과 fake-news extension 보류
- [ ] 원 AM+PM 체결 정합성과 AM-only stance 진단을 혼동하지 않음
- [ ] AM+PM daily-fill 방향을 always-buy/sell과 동일 날짜·동일 label·동일 skip rule로 비교
- [ ] daily reconstruction을 AM opening-gap·PM current-return 등 사전 고정 price-only 규칙과 비교
- [ ] raw accuracy뿐 아니라 balanced accuracy, buy recall, sell recall, MCC와 date-level moving-block uncertainty 보고
- [ ] price-only baseline 대비 incremental score 또는 residual-flow 설명력을 사전 고정 방식으로 보고
- [ ] AM stance의 coverage/NO_CALL/BA는 내부 방향 형성 보조 진단으로 별도 보고
- [ ] 45일 실행·42일 primary replay 결과는 development evidence로만 표기하고 unseen 예측 성공으로 사용하지 않음

**Development replay의 baseline 반응 승격 기준(사전 고정 권고값)**

- exact 45일 input coverage와 exact 3-date burn-in·42일 metric coverage, target/runtime leakage·누락·중복 0
- AM+PM daily-fill buy recall `> 0.50`, sell recall `> 0.50`, balanced accuracy `> 0.50`
- raw accuracy가 동일 42일의 더 강한 constant baseline `max(always-buy 59.5%, always-sell 40.5%)`를 초과하고 MCC `> 0`. target source가 바뀌면 비율을 재계산
- 사전 고정 price-only reconstruction baseline과의 `ΔBA`를 반드시 공개. 이 값을 넘지 못하면 “가격정보를 넘어선 추가 반응 설명력”은 주장하지 않음

앞의 첫 세 bullet을 wealth 조건 없는 `core_p3b_pass`로 계산한다. price-only `ΔBA` bullet은 추가 설명력 주장 gate이며 core pass 자체에는 넣지 않는다. `wealth_sensitivity_v1`가 full-100과 각 leave-one-rich-out의 core pass 및 RQ2 mean-effect 부호를 비교해 `wealth_fragile`을 만든 뒤, **후속 fake-news extension용 최종 gate는 `robust_p3b_pass = core_p3b_pass && !wealth_fragile`**로 둔다. 어느 계산도 `robust_p3b_pass`를 다시 입력으로 사용하지 않는다.

이는 반응 **정합성/reconstruction** gate이지 시간적으로 앞선 forecast gate가 아니다. PM이 종가와 당일 정보를 본다는 점을 결과표 제목과 초록에서 명시한다.

### P3-C: 선택적 prospective/nowcast 주장

논문에서 별도로 “실제 개인 방향을 미리 예측한다”고 주장할 때만 적용한다. 이번 AM+PM primary를 이 gate의 대용으로 사용할 수 없다.

- AM opening-nowcast coverage `>= 90%`, buy/sell recall 모두 `> 0.50`
- 사전 고정한 AM opening-gap price-only baseline 대비 `ΔBA > 0`
- 현재 시가를 보므로 명칭은 opening nowcast. 진짜 pre-open forecast는 당일 시가·파생 feature를 제거한 별도 frozen pipeline 필요
- code·prompt·memory·aggregation·skip rule freeze 뒤 설계에 사용하지 않은 `>= 40` 거래일
- 최소 3개, 권장 5개 paired world seed
- seed별 100명 stance를 먼저 하나의 date-level 신호로 합성하고, 100명을 독립 표본으로 세지 않음
- date moving-block bootstrap에서 AM price-only baseline 대비 `ΔBA`의 95% 신뢰구간 하한 `> 0`
- buy/sell recall, coverage, MCC와 seed sensitivity가 모두 사전등록 범위 충족

이 수치들은 설계 권고값이며 본 실행 전에 preregistration에서 승인·freeze한다. 결과를 본 뒤 낮추면 해당 결과는 exploratory다.

가짜뉴스 확장은 두 수준으로 구분한다. P3-A와 P3-B를 통과하면 일별 실제 개인 반응에 최소 정합성을 보인 simulator에서 **탐색적 mechanism experiment**를 할 수 있다. “실제 개인 방향을 미리 잘 예측하는 base에 가짜뉴스를 얹었다”는 강한 표현은 P3-C까지 통과해야 한다. 확장 시 synthetic claim의 source ID·truth anchor·injection 시각·public/private visibility와 paired contrast를 별도 사전등록한다.

---

## 23. 실행 중 즉시 pause 조건

- requested/returned model 또는 provider mismatch
- reasoning setting 누락 또는 reasoning token > 0
- missing telemetry로 off 여부 입증 불가
- 미래 news/price/community/memory exposure
- current-turn fill의 same-turn STB/analysis/decision 참조, 또는 actual fill이 확정된 뒤 post-fill LTB transaction episode에서 누락됨
- cross-condition memory/cache/evidence ID
- duplicate/different-payload idempotency key
- 100명 중 missing/duplicate agent result
- DB `quick_check` 실패
- `RN_COMM_OFF`에서 post/selective-read/reaction/Best exposure 또는 community-derived STB/LTB evidence 생성. 단, 스케줄 정합성을 위한 no-op phase marker와 agent-visible content가 없는 explicit empty audit snapshot은 허용한다.
- fake row 또는 synthetic ID 발견
- rolling 429, 5xx, schema retry가 canary threshold 초과
- disk free, API credit, token/cost hard cap 도달
- heartbeat 정지 또는 pair block 불일치
- code/prompt/config/input hash drift
- frozen article-version provenance 누락 또는 cutoff 이후 마감·최종 수급 정보 발견
- LLM output의 unknown/extra key 또는 untrusted text의 state namespace 탈출

pause 시 자동으로 기본 belief·기본 주문·fallback model을 사용해 계속 진행하지 않는다.

---

## 24. 완료 판정

현재 100명 cohort의 확정 30/55/15 depth 정책 기준으로 먼저 **arm별**, 그다음 두 arm 합계를 검증한다. 후속 축소 study에서는 새 exact agent registry와 그 registry에서 파생한 permission map을 사용하며 기존 100명 registry의 first-N slice나 암묵 교체를 금지한다.

- manifest resolver가 `A` conditions, `N` agents, `D` dates, ordered decision-turn map의 event 수 `U`, ordered community/no-op phase-key 수 `P`와 expected composite-key set을 먼저 생성하고 모든 완료 count는 여기서 계산
- arm당 decision phase `U`개와 community/no-op phase `P`개 완료. 현재 policy는 **모든 거래일 PM 뒤** community/no-op phase가 하나씩 있어 `P=D`, AM/PM study에서는 `U=2D`이고 90+45=135 phase/arm으로 해석한다. 마지막 PM phase도 실행·기록하며 연구기간 안 next-AM Best broadcast/exposure는 0이다. 이때 non-empty Best schedule만 `right_censored`이고 Best가 없으면 status는 `empty`다. 마지막 PM의 eligible D1/D2 선택 열람 exposure는 별도로 존재할 수 있다.
- arm당 agent-turn `N×U`, 전체 `A×N×U`; 현재 2-arm·100명·45일 AM/PM 예시는 arm당 9,000, 합계 18,000
- 각 arm의 fixed persona는 1억 90명·10억 10명, total initial cash 190억이며 두 arm agent별 demographic·cash·prompt hash 동일
- paired comparison key 수는 paired arm마다 `N×U`; 현재 예시는 9,000쌍
- turn `1..U`의 각 expected `(agent,date,subturn)` key에 committed STB와 LTB update가 각각 정확히 1개이고 key set이 동일
- arm당 STB row와 LTB update row는 각각 `N×U`, `LTB_0` 포함 LTB state는 `N×(U+1)`
- agent당 `STB states = LTB updates = U`, `LTB states = U+1`이며 LTB parent chain이 끊기지 않음
- portfolio state: 각 agent-turn 정확히 1개
- decision/order/fill 관계가 각 agent-turn에서 constraint와 일치하고 HOLD·0주·partial fill 0
- exchange summary는 arm당 `U`, 전체 `A×U`
- 전체 phase count는 `A×(U+P)`; 현재 `P=D`인 2-arm AM/PM 예시는 270
- Community OFF: post/read/reaction/Best/exposure 0, community-derived STB/LTB evidence 0
- Community ON active posting/selective-read/reaction **기회**는 `Σ_p |eligible_active_agents(p)|`; 현재 동일 permission·일별 1회 policy에서 `N_active×D=3,150`. 실제 post row 범위도 `0..opportunity_count`
- Depth 0 passive schedule은 `Σ_p |eligible_best_only_agents(p)|`; 현재 예시는 `N_depth0×D=1,350`
- Best broadcast/community-log schedule envelope는 `Σ_p |best_audience(p)|`; 현재 예시는 `N×D=4,500`
- 연구기간 안 실제 next-AM Best 소비 기회 상한은 ordered date map에서 후속 AM이 존재하는 schedule key 수로 계산; 현재 매일 AM/PM 구조에서는 `N×(D-1)=4,400`. 마지막 날 audience envelope 100개는 next-AM Best broadcast/exposure 0이며, non-empty Best에 대응하는 schedule만 `right_censored`이고 empty Best는 `empty`
- post-level 선택 열람 상한은 `Σ_p Σ_{a∈eligible_readers(p)} read_cap(a,p)`; 현재 일별 동일 permission에서는 `(55×5 + 15×10)×45 = 19,125`
- next-turn/H1/H5 mature·censored count는 fixed 숫자가 아니라 fill key와 ordered future reference-price map에서 계산한다. 현재 45일 AM/PM·100명 예시의 8,900/100, 8,800/200, 8,000/1,000은 resolved audit 값일 뿐이다.
- Depth 0 posting/selective-read/reaction 0. non-empty Best이면서 연구기간 내 다음 AM이 있는 날에는 Depth 0 Best exposure가 존재하고, empty Best/final-day schedule에는 exposure 0
- 모든 community influence가 다음 거래일 AM 이후
- 모든 LTB dimension change에 끊기지 않은 evidence lineage
- orphan/cross-arm memory row 0
- reasoning token 0
- non-treatment invariant가 두 arm에서 동일
- final SQLite `quick_check` pass
- final STB/LTB/scientific-state digest와 manifest/archive hash 생성

Community schedule log는 manifest Best-audience의 **모든 거래일 PM 직후** schedule phase에 한 envelope를 둔다(현재 `N=100`, 매 거래일 1회). 글이나 Best 5가 없으면 명시적 `empty` event를 저장한다. 마지막 PM도 같은 phase/checkpoint를 저장하되 non-empty Best만 `right_censored`, Best가 없으면 `empty`다. `community_best` exposure는 연구기간 안 다음 AM **community-interpretation request**에 제목·본문이 실제 삽입된 경우에만 `delivered`가 되므로 마지막 PM의 next-AM Best broadcast는 0이다. 반면 `community_selected` exposure는 eligible D1/D2가 같은 PM에 실제 본문을 선택해 읽으면 생성될 수 있고, 후속 AM이 없으면 그 이후 해석만 발생하지 않는다. STB request에는 원문을 넣지 않는다. Active posting/read/reaction 행은 permission map의 eligible set만 생성할 수 있다(현재 Depth 1·2의 70명).

---

## 25. 구현 파일별 변경 계획

### `config.py`

- study-specific agent 수를 전역 config에 복제하지 않음. paper `StudySpec.required_agent_count=100`과 exact cohort registry를 loader가 강제하며 후속 study는 새 registry로 `N`을 바꿀 수 있음
- reasoning object와 verification policy
- fallback false/provider pin
- memory policy version/per-turn cadence/byte cap
- `decision_space=buy_sell_only`, max-cash ratio 0.5, `commission_rate=0.0`·`sell_tax_rate=0.0`·`fee_amount=0`, full-fill policy
- stage별 max_tokens
- rate/cost/disk circuit-breaker
- `.env.example`과 일치시킴

### `data/fixed_slots.csv`·`twinmarket_kr/persona/select.py`·persona repair manifest

- existing 100 IDs, gender, age/group, initial cash와 90/10 asset split을 **재추첨·재층화하지 않고 freeze**
- `fixed_slots.csv`↔`sys_100.db` exact row hash와 100 unique source-user mapping 저장
- pre-repair `sys_100.db`의 agent별 news depth 30/55/15를 canonical map으로 freeze하고 Depth 2=15를 유지. `scripts/01_build_persona.py`/`match_agents()` 재실행과 config-ratio 재배정은 금지
- 현재 DB structured row를 입력으로 100개 prompt **전체 serialization만** `render_persona_v1`의 NFC·LF-only·exactly-one-trailing-LF·고정 section order/separator·scalar escaping 규칙으로 재생성하여 depth 문장과 A001 형식을 함께 복구한 run-local persona snapshot 생성
- canonical parser round-trip 100/100, DB↔parsed prompt의 `agent_id → depth → permission`과 모든 의미 field mismatch 0, 비-prompt structured value 변화 0을 검증하고 ordered `(agent_id,news_depth,prompt_sha256)` map hash·pre/post mismatch(60→0)·renderer hash를 repair manifest에 기록
- init-memory/turn-0 belief/paper launcher/runtime/validator/report는 explicit sealed `--persona-snapshot`을 받고 global `outputs/sys_100.db` fallback은 paper mode에서 hard-fail
- rich ID list, demographic count, total initial capital 190억, rich capital share 52.63%, match-score distribution을 manifest에 기록

### `twinmarket_kr/llm/client.py`

- 모든 request에 reasoning-off 강제
- logical call context 전달
- provider/model/reasoning telemetry audit
- seeded jitter와 rate limiter
- stage별 max_tokens
- validated response journal interface

### `twinmarket_kr/agents/memory_agent.py`

- previous canonical LTB `dim_1~dim_6` 전체 조회; summary-only 반환 제거
- current STB 별도 저장과 actual fill 뒤 생성되어 next event에 visible한 LTB append-only 저장
- event/exposure/evidence API
- `INSERT OR REPLACE` 제거
- idempotent hash conflict 처리
- exact LTB parent-version, next-event stage visibility, same-turn transaction-episode/late price-outcome visibility 검증

### `twinmarket_kr/db/schema.py`

- existing `belief_history`를 canonical LTB로 유지하고 `short_term_belief_history`, dimension transition, event/exposure/evidence/turn-trace/phase-consumption table 추가
- `community_claim_id → community_claim_sources → reader source_exposure_ids → distinct root_post_ids` FK chain과 같은 reader/turn namespace, fill fact와 `(fill_id,horizon)`별 `consumed_by_ltb_transition` 1회 uniqueness 추가
- `ltb_scientific_hash`와 `human_log_hash`를 별도 column/constraint로 두고 parent/cache/scientific digest는 전자만 사용
- 모든 scientific key에 run/condition/agent/turn namespace 추가
- unique, foreign-key, check constraints
- schema version/migration

### `twinmarket_kr/agents/news_agent.py`와 뉴스 전처리

- runtime ID와 URL/source/published/observed/scraped/last-modified/raw-version/body·summary content hash, summary 생성/절단 방법·시각을 1:1 provenance sidecar로 보존
- real-only mode는 `is_fake` 하나를 필터하는 방식이 아니라 승인된 exact clean-ID/row-hash registry 밖 row를 즉시 reject
- frozen cutoff-time body snapshot이 없는 기사 quarantine
- AM/PM/title/body/summary/Depth2 search semantic leakage scan
- `news_20260427_섹터_0032` 같은 당일 종가·최종 삼성전자/개인수급 direct target 문구의 reject/mask/allow manifest
- daily selection seed, algorithm version, resolved event-to-news-slot ordered ID map과 event별 target/actual count·shortage reason을 manifest에 저장하고 `None` seed 금지. current resolved example의 `45 dates·90 decision events·target 900 article slots`와 actual total은 분리 audit field로 기록
- preprocessing LLM reasoning 정책과 model/prompt/request hash 저장

### `twinmarket_kr/agents/exchange_agent.py`

- BUY/SELL only, no-short, 50% cash cap, full-fill invariant 유지
- fill/audit field를 `fill_status`, `requested_quantity`, `filled_quantity`, `executed_price`로 canonicalize하고 `filled`이면 requested=filled를 강제
- `constraint_forced`를 server-side allowed-action count로 계산
- `COMMISSION_RATE=0.0005`와 실제 fee 0.0 불일치 해소; 이번 baseline은 config를 `0.0`으로 고정하고 portfolio·PnL·`paper_fill_ledger`·canonical CSV export·manifest 모두 `fee_amount=0`으로 일치 검증
- completed turn에서 HOLD·0주·partial/unresolved fill을 fail-closed

### `twinmarket_kr/community/agent.py`

- `mark_best_posts()`는 ranking/ID 확정과 frozen full payload 조회를 분리
- Best payload에 `post_id`, title, content, type, score, content hash 포함
- 100명 broadcast schedule과 실제 next-AM exposure를 별도 append-only event로 저장
- `community_logs`의 `INSERT OR REPLACE`와 다음-AM destructive `UPDATE` 제거
- Depth 2 public author profile을 allowlist로 sanitize

### `twinmarket_kr/community/thinking.py`와 prompt

- Best 5의 원문 본문 전체를 100명에게 동일 format으로 제공
- 선택 열람 본문의 silent 200자 절단 제거
- selection prompt에 실제 표시된 candidate metadata는 title-only exposure로 기록하고, 선택 body/Best body와 노출 수준을 구분
- §10.3의 next-AM `community_interpretation` exact schema로 입력 allowlist의 non-empty reader `source_exposure_ids`를 인용; 서버가 `community_claim_id`와 claim-source junction을 부여하고 distinct root posts를 FK로 resolve하며 자유형 250~500자 reasoning memory로 사용하지 않음
- selected body는 PM selected-read/reaction과 다음-AM interpretation의 두 prompt-consumption edge, Best-only body는 다음-AM 첫 소비로 기록; root support는 각각 하나
- selected+Best 동일 reader/root/level/content overlap은 next-AM body 1회 직렬화, source exposure ID·junction 2개, consumption edge 총 3개, distinct-root support 1개로 deterministic dedup
- `RN_COMM_OFF`/no-exposure call은 생략하고, STB/LTB에는 visible-source provenance가 기록된 claim ID/text/claim-stance와 sanitized lineage만 전달해 raw body 주입 금지
- community text와 system/task instruction을 role·canonical serializer 수준에서 분리
- length/required-field/evidence-ID validation 추가

### `twinmarket_kr/community/posting.py`

- post title/body cap과 `max_tokens` 강제
- public post와 private source news/fill/LTB lineage를 분리하고 독자에게 private lineage를 노출하지 않음
- summary/change 대신 stage-qualified LTB six-dimension allowlist 사용
- public post와 canonical fill/portfolio ledger를 분리한다. 게시글 내용의 일치·진실성은 validator가 판정하지 않는다.

### `twinmarket_kr/core/collect_context.py`

- STB용 current typed evidence packet만 생성하고 raw `today_context` 전체 직렬화 제거
- previous LTB `dim_1~dim_6`와 current STB `dim_1~dim_6`를 analysis/decision에 분리 block으로 제공하고, exchange 확정 뒤 LTB updater에는 **current structured decision/fill episode**와 eligible earlier price-outcome packet을 추가 제공
- `belief_summary`, `view_change`, historical order/fill, free-text action reason, retry metadata 제외
- community explicit empty slot
- Depth 0도 전날 Best 5-only schedule을 community-interpretation 전용 block으로 불러오되 일반 `posts_read`는 비어 있음을 검증; raw body는 STB block에 복사하지 않음
- exact STB, analysis/decision, LTB input snapshot/hash를 별도로 저장

### `twinmarket_kr/llm/belief.py`와 prompt

- 기존 belief call을 current-evidence-only STB 생성기로 변경
- dimension별 support/contradiction IDs
- 실제 length limit 강제
- AM 전용 문구 제거
- exact key set·unknown-key rejection
- STB에는 summary/change를 생성하지 않음

### 새 `twinmarket_kr/llm/long_term_belief.py`와 `prompts/update_long_term_belief.txt`

- 각 AM/PM STB·analysis/decision·deterministic fill staging 뒤 agent당 정확히 1회 호출
- previous LTB six dimensions + current STB six dimensions + **current structured decision/fill episode** + eligible earlier price-outcome packet + sanitized registry + frozen persona hash/profile exact allowlist 입력
- next-event-visible LTB `dim_1~dim_6`, dimension별 integration-evidence IDs 출력; 모든 dimension이 parent와 다른 hash인지, material deletion/reversal에 근거가 있는지 server 검증
- final applied dimensions·parent/current hash·integration evidence에서 server가 사람용 summary/change를 deterministic render하고 `ltb_scientific_hash`/`human_log_hash` 분리
- reasoning-off, schema validation, deterministic seed, response journal 적용
- next-event LTB는 `visible_from_turn=t+1`; same-turn analysis/decision에는 불가시이며 current fill은 그보다 앞선 **post-fill LTB updater의 transaction episode field에만** 허용

### `twinmarket_kr/simulation.py`

- frozen phase inputs
- journal replay
- AM scheduled exposure resolve→community-interpretation/claim barrier→STB barrier→analysis/decision barrier→deterministic fill staging→LTB rewrite barrier→exposure부터 phase-call-consumption까지 single batch commit
- same-phase visibility 차단
- current fill fact는 same-turn post-fill LTB transaction episode에서 1회, next-turn/H1/H5 horizon record는 각각 maturity turn에서 1회만 price-outcome packet에 소비하고 재소비 방지
- AM/PM fill fact는 각자의 post-fill LTB에 즉시 1회 보이도록 하고, 이후 horizon record는 각 due event에서 별도 `dim_6` price-outcome evidence로 보이도록 ledger visibility·consumption 적용
- posting/selective reading/reaction은 Depth 1·2만 유지하고, non-empty Best full-payload schedule은 100명 모두 저장; empty/final-day next-AM Best broadcast/exposure 0
- posting→board freeze→selection→reaction→Best 확정→broadcast 예약을 각각 barrier로 분리

### `twinmarket_kr/experiment_runtime.py`

- scientific memory와 phase-consumption tables lifecycle 포함
- restart safety classification 확장
- clean base는 runtime table blacklist 삭제가 아니라 approved allowed-table schema·row-count·100 LTB₀/portfolio scientific digest allowlist로 생성·검증
- paper run 중 mutable `sim.db`에서 base 자동 생성/`--rebuild` 금지; 별도 승인 단계에서 봉인한 base만 사용
- clean base에서 scientific staging/cache와 forbidden legacy/fake/memory row 0 검증
- condition별 `response_journal.sqlite`는 rollback 밖에 두고 signature/hash/archive 대상으로 관리

### `twinmarket_kr/run_integrity.py`

- current external evidence→STB + previous LTB→stance→decision/fill→same-turn post-fill LTB→next-event visibility의 manifest-derived `U:U` 1:1 lineage digest
- scientific digest에 LTB six dimensions를 포함하고 summary/change는 별도 human-log digest로 분리
- community claim→claim-source junction→reader exposures→distinct roots와 STB/current decision-fill episode/price-outcome→LTB lineage, `dim_6` integration subset, fill fact 및 `(fill_id,horizon)`별 1회 consumption 검증
- reasoning token/provider 검증
- resolved exact cohort count/ID set 검사(현재 paper spec은 required 100; 후속 spec은 자체 `N`)
- community temporal checks
- cross-condition contamination checks
- fake-off marker 검사만으로 통과시키지 않고 real-news bundle의 exact schema/file/public-row/ID/date/slot hash를 검증
- visible/read/search/influential/evidence/journal의 모든 external-news root ID가 clean registry에 속하는지 closure report 생성
- 마지막 committed scientific digest와 resume 직전 DB를 actual compare하고, news bundle도 매 phase 호출 전에 re-hash

### 새 `validation/validate_realnews_community_ab.py`

- `resolved_study_manifest.json`, raw value/volume target CSV, RN_COMM_OFF/RN_COMM_ON canonical final fill ledger·submitted-order·run-integrity artifact를 직접 입력으로 받음
- manifest의 ordered date/event map과 두 arm의 exact `(condition_id, agent_id, date, decision_event)` expected fill-key set을 strict-validate하고 교집합 축소 금지. 이번 study input의 `N=100`도 exact agent-ID manifest와 함께 검사하되 `45×2` 산술은 validator에 재작성하지 않음
- action/`fill_status`/stock/subturn/positive-integer `requested_quantity`·`filled_quantity`/finite `executed_price`, AM=open·PM=close, `requested_quantity == filled_quantity` 전량체결을 key-by-key 검증
- excluded row position이나 evaluator 기본값이 아니라 manifest의 frozen burn-in date mask를 적용해 primary evaluation-date set 생성. 현재 study의 resolved 결과는 3개 제외·42개 평가일
- §19.9의 단일 2×3 direction metric implementation으로 value primary, volume·AM-only·PM-only·constraint-neutral sensitivity 생성
- full-45와 first-3-excluded 결과를 명확히 다른 namespace로 저장하고 headline key는 42일만 가리킴
- 전체 approved input-date set에서 lag baseline을 만든 뒤 resolved evaluation-date mask 적용; actual-ratio oracle과 random distribution을 일반 comparator와 분리
- RN_COMM_OFF/RN_COMM_ON invariant와 `(agent_id,date,subturn)` exact pair를 검증한 뒤 agent별 AM+PM signed notional 합→fixed-initial-capital 정규화→agent 평균 순서로 paired daily effect·discordance 출력
- 두 arm의 resolved real-news bundle·canonical public-row·ordered clean-ID·slot-map hash equality와 downstream external-news ID closure를 먼저 검증
- raw/1억-only(=rich-excluded alias)/10억-only/initial-capital-normalized/leave-one-rich-out를 `wealth_sensitivity_v1`으로 산출하고 alias byte-equality·rich contribution·`wealth_fragile` 판정 출력
- legacy `validate_trading_direction.py`와 historical analysis output은 migration 대조용으로만 읽고 paper metric source로 재사용하지 않음

### 기존 report/PDF와 새 run-scoped report bundle

기존 convention은 보존하되, 과거 의미·전역 DB·고정 분모를 새 baseline에 재사용하지 않는다.

- archive-only로 보존: `scripts/generate_run_report_pdf.py`, `scripts/generate_community_report_pdf.py`, `scripts/generate_deep_analysis_report.py`, `scripts/generate_condition_comparison_report.py`, `validation/validate_trading_direction.py` 및 `analysis/current_experiment_review`, `analysis/paper_0721_c00_review`.
- 새 evaluator/report 경로는 `evaluator_contract.json`, `resolved_study_manifest.json`, `RUN_FINALIZATION.json`, `traces/final_fill_export_index.json`, arm별 `traces/rn_comm_*_final_fill_ledger.csv`, frozen evaluator-only target, `community_interactions.csv`, `community_best_posts.csv`, `traces/community_post_trace.jsonl`, `traces/community_exposure_trace.jsonl`만 명시적으로 받는다. belief/outcome 세부 검증은 각 arm의 `paper_run.sqlite` canonical table을 finalization gate가 먼저 대조한다.
- 새 bundle은 기존 파일명·CSV를 compatibility export로 계속 링크하되, `outputs/sys_100.db`, 현재 working DB, glob으로 찾은 최신 run, legacy condition alias, skip=5, date/agent intersection fallback을 감지하면 hard-fail한다.
- 현재 evaluator의 필수 machine-readable 산출물은 `daily_flow_comparison.csv`, `paired_condition_summary.json`, `direction_validation.json`, `evaluation_artifact_index.json`이다. 사람이 읽는 PDF/Markdown report는 이 indexed 결과에서 후속 생성한다. 모든 output은 input artifact hash·generator version·생성 시각을 기록하고 `RUN_RECORD.md` 또는 후속 report index가 path·SHA-256·gate 상태를 역링크한다.
- 기존 `generate_run_report_pdf.py`의 global persona DB 조회와 “Depth 0 community 미참여” 서술, `generate_community_report_pdf.py`의 좋아요→판단 강화 단정, `generate_deep_analysis_report.py`의 고정 분석 시작일/50-agent/구식 depth/Gini, `generate_condition_comparison_report.py`의 legacy C-code·전역 DB·고정 run path는 새 baseline에서 재사용 금지다.

### 새 2조건 launcher

권장 파일명: `scripts/09_run_realnews_community_ab.py`

- 조건을 RN_COMM_OFF/RN_COMM_ON 두 개로만 고정
- fake/news-path/sim-db/base-rebuild option 자체를 노출하지 않고 typed study spec에서 real-only bundle·approved base를 해석
- paper `required_agent_count=100`과 exact cohort registry를 단일 source로 사용하고, 후속 축소/확장 spec에서도 같은 loader로 `N` resolve
- paired date/subturn scheduler
- 두 child argv diff allowlist는 community mode 하나뿐이고, 동일 read-only news bundle object를 참조하는지 parent가 API 전 검증
- condition별 독립 DB/output/journal/audit, sealed study root, symlink·`outputs/logs/current`·legacy run 입력 금지
- canary와 full-run mode 분리
- full-run에서 offline stub/fallback을 첫 호출 전 거부
- manifest에 모든 invariant와 known injection overlap 0, exact clean-news closure policy 기록

### tests

- 기존 `tests/test_experiment_safety.py` 유지
- memory temporal integrity, reasoning off, 100-agent, pair resume용 테스트 파일 추가
- evaluator 전용 fixture로 authored date/burn-in policy→resolved evaluation mask, date별 `N×|Q_d|` fills, event별 reference price, `requested_quantity == filled_quantity`, duplicate/missing/invalid/flat, lag-before-mask, paired-condition key set, agent-first daily sum을 독립 테스트. 현재 study의 45→3 제외→42와 하루 200 fills도 golden fixture로 포함
- real-only fixture로 marker 없는 내용변경, synthetic/private metadata만 남은 row, 44일 교집합 축소, RN_COMM_OFF/RN_COMM_ON hash 불일치, preflight 뒤 file swap, clean-registry 밖 read/search/influential/evidence ID, old fake journal/DB/log, 2026-04-27 EOD leakage를 모두 reject

---

## 26. 권장 구현·실행 순서

1. **study amendment 승인**: 30→100, 6조건→실제뉴스 2조건, 계층 메모리, reasoning off를 새 version으로 명시한다.
2. **persona repair**: 100개 identity·demographic·90/10 asset split과 **`sys_100.db`의 agent_id별 depth map**은 고정하고, 그 DB structured field에서 100 prompt/report를 재렌더한다. `30/55/15` 분포만 맞추는 재배정·재추출은 금지하며, prompt parser의 `agent_id → depth → information permission`이 DB와 100/100 일치해야 한다. 동시에 한 canonical template로 재생성해 A001의 붙은 문장을 없애되, 비-depth persona 의미 field 변화는 0이어야 한다.
3. **news provenance repair**: cutoff-time article version과 summary provenance를 만들고 이미 발견된 same-day close/flow leakage를 quarantine한다.
4. **reasoning-off client**: live canary와 provider pin을 먼저 완성한다.
5. **event/STB/LTB schema**: existing belief=LTB를 유지하고 current-only STB·lineage·stage visibility를 구현한다.
6. **STB call**: 기존 belief call을 current typed evidence-only six-dimension STB 생성기로 바꾼다.
7. **post-trade LTB updater**: previous LTB + current STB + **current actual decision/fill episode** + eligible earlier price outcome→next-event-visible 6D LTB prompt·no-copy server validator를 구현한다.
8. **downstream allowlist**: previous LTB와 current STB의 분리 belief block만 analysis/decision에 전달하고 summary/change/raw news/community/extra key를 차단한다.
9. **trade outcome maturity/feedback**: actual fill의 same-turn post-fill LTB transaction-episode visibility와 next-turn/H1/H5→LTB `dim_6` price-outcome 연결을 구현한다.
10. **response journal/barrier**: STB→analysis→decision→fill→LTB의 100명 crash-resume 문제를 해결한다.
11. **community full-body path/security**: Best 원문 100명 next-AM exposure와 injection/namespace 검증을 구현한다.
12. **strict evaluator·2-arm launcher**: real-news·fake-off·100명·paired scheduling, exact 45-date input completeness, frozen first-3-date burn-in, exact 42-date primary와 RN_COMM_OFF/RN_COMM_ON fill pairing을 강제한다.
13. **offline failpoint/adversarial tests**: short/current-`U`/extended maturity·drift와 uninterrupted/resume digest를 확인한다.
14. **100명 2-day RN_COMM_OFF/RN_COMM_ON live canary**: STB/LTB·community 경계와 비용·지연·retry·storage를 계측한다.
15. **freeze·45-day paired run·final integrity·analysis** 순으로 진행한다.

---

## 27. 현재 구현에서 발견한 설계 결함·의문점 전수 목록

이 목록의 목적은 함수 이름이나 DB field가 존재한다는 이유만으로 실제 노출·기억·인과 경로가 구현됐다고 간주하는 오류를 막는 것이다. 모든 정보 흐름은 다음 다섯 단계를 따로 검증한다.

```text
생성/확정 → payload 조회 → agent prompt 삽입 → 실제 exposure 기록
→ 이후 memory/belief에서 동일 lineage로 사용
```

### 27.1 Community 경로

| ID | 현재 구현과 근거 | 결함·의문 | 본 설계의 처리 |
|---|---|---|---|
| C-01 | `community/agent.py:118-136`의 `mark_best_posts()`는 ID·제목·유형·점수만 반환 | Best ID로 원문을 다시 찾을 것처럼 보이지만 후속 본문 조회가 전혀 없음 | **P0 확정 수정**: 확정 ID를 DB 원문과 join하고 제목+본문 전체를 100명 다음 AM payload로 만듦 |
| C-02 | `community/thinking.py:57-63`은 Best 제목·유형·점수만 formatting | Depth 1·2도 Best 경로에서는 제목만 봄 | **P0 확정 수정**: 동일 frozen title+content+score를 typed source-data field로 100명에게 삽입; delimiter만 보안경계로 간주하지 않음 |
| C-03 | `simulation.py:922-930`, `collect_context.py:78-85`, `daily_cycle.py:133-140` 모두 사실상 Depth 1·2만 처리 | Depth 0은 Best 제목조차 못 봐 Best5-only 원 설계와 충돌 | **P0 확정 수정**: broadcast/log/context/thinking gate를 100명으로, posting/select/react gate만 70명으로 분리 |
| C-04 | Depth 1·2의 일반 선택은 candidate metadata만 보고 결정한 뒤 `get_post_content()` 호출 | 이 경로만 원문을 실제 조회하며 Best 경로와 별개이고, 선택 prompt에 보인 미선택 제목도 실제 causal exposure임 | 원 설계 유지. exact shown candidate metadata는 title-only exposure, 선택 body는 full-read exposure, Best는 broadcast exposure로 따로 기록하고 다음-AM interpretation에 노출 수준대로 전달 |
| C-05 | `community/thinking.py:72`가 전날 선택해 읽은 본문을 Python 문자 기준 앞 200자로 자름 | agent가 실제 읽은 핵심이 뒤에 있으면 다음 AM 회상에서 사라지고 truncation 표시도 없음 | silent 200자 절단 금지. 게시글 생성·저장 시 frozen cap을 적용하고 cap 안의 원문 전체와 hash를 next-AM에 전달 |
| C-06 | Best는 `score DESC, like DESC, post_id ASC`; 반응이 0이면 가장 먼저 저장된 글이 뽑힘 | “Best”가 참여자가 평가한 우수글이 아니라 insertion order일 수 있음 | 원 ranking은 유지하되 `zero_engagement_best_count`를 기록하고 논문에서는 `rank-top-5` 산식 공개. engagement 최소조건을 추가하면 별도 amendment |
| C-07 | candidate snapshot을 먼저 고정하고 모든 선택 뒤에 반응을 batch 적용 | 당일 agent는 다른 agent의 새 like/unlike를 보고 따라가지 못함 | 재현성을 위한 동시 batch 설계로 유지. 실시간 cascade/FUSE식 확산을 재현했다고 주장하지 않음 |
| C-08 | posting·select는 빈 결과 허용, reaction은 실제 읽은 글이 있을 때만 실행 | “70명이 매일 게시·열람·반응” 또는 “매일 Best 정확히 5개”라는 표현은 실제보다 강함 | 3,150은 **기회/eligibility**로만 표기; Best는 `top-K-or-fewer-available`이며 강제 posting 없이 `available_post_count/best_count/actual_exposure_count`와 실제 post/read/reaction 수를 별도 보고 |
| C-09 | `posting.py:19`는 `trade_share`가 실제 거래를 반영하지 않아도 된다고 명시; `profit_share`도 실제 PnL 검증 없음 | 공개 게시글과 canonical fill/PnL ledger가 서로 다른 역할인데 이를 혼동할 수 있음 | 현재 원 설계를 보존하고 “exogenous fake-news injection 없음”으로만 표현한다. 게시글 내용을 ledger와 일치시키거나 server가 진실성을 판정하지 않으며, 실제 체결·PnL 계산에는 canonical ledger만 사용한다. |
| C-10 | 게시글 output에는 source news/fill/belief evidence ID가 없음 | 어떤 입력·해석에서 나온 글인지 추적·중복 제거 불가 | 게시자 private provenance로 source IDs·root IDs를 저장하되 독자에게 비공개하고, 공개글의 의미·신뢰도와 분리해 출처 lineage만 보존 |
| C-11 | `posting.py:67-73,109-120`은 제목·본문 nonempty만 검증; DB에도 길이 CHECK 없음 | column/반복 출력이 context를 잠식하고 arm별 retry 차이를 키울 수 있음 | title/body 문자·byte·token cap, stage `max_tokens`, DB CHECK를 manifest에서 freeze |
| C-12 | `community_thinking.txt`는 250~500자와 6개 내용을 요구하지만 `thinking.py:36-54`는 nonempty만 검사 | 한 글자 응답도 성공하며 필수 분석·근거 연결을 보장하지 못함 | §10.3의 next-AM exact-schema community interpretation으로 축소; 모델의 non-empty source exposure IDs·서버의 claim-source junction→distinct roots·length validator를 두고 hidden CoT는 요구하지 않음 |
| C-13 | Depth 2 `get_author_profile()`이 `_latest_portfolio()` row 전체와 최근 거래를 PM react prompt에 전달하지만 다음-AM formatter는 profile을 완전히 버림 | stable `agent_id`나 비공개 field가 익명성을 깨뜨릴 수 있고, 반대로 허용된 profile도 stateless LLM의 다음 판단에는 남지 않음 | explicit public allowlist만 PM에 노출·ledger 저장; agent ID·persona·belief·private memory 제거. 원 설계가 허용한 public profile snapshot은 다음-AM packet에도 lineage와 함께 유지 |
| C-14 | `community_logs`는 `INSERT OR REPLACE`, 다음 AM thinking은 어제 row를 `UPDATE` | 어떤 payload가 PM에 확정됐고 AM에 실제 소비됐는지 과거 상태가 덮어써짐 | schedule, actual exposure, thinking을 append-only 별도 event로 저장; same-key different hash는 pause |
| C-15 | 같은 글을 선택 열람한 뒤 Best로 다시 받을 수 있음 | 두 노출을 독립적인 두 증거로 세면 community echo가 가짜 confidence를 만듦 | exposure는 2개, LTB root support는 1개로 계산; `root_post_id` dedup |
| C-16 | 마지막 거래일 PM에도 Best를 계산할 수 있으나 다음 거래일 AM이 연구기간 밖 | 4,500 audience schedule envelope와 실제 효과 4,400을 혼동할 수 있음 | 마지막 100개는 actual prompt exposure 0. non-empty Best만 `right_censored`, empty Best는 `empty`로 분리 |
| C-17 | 첫 거래일 AM에는 전날 게시판이 없음 | 첫날 `RN_COMM_ON − RN_COMM_OFF` 차이를 community 효과로 해석하면 안 됨 | Day 1을 temporal negative control로 표시; 최초 직접 community input은 Day 2 AM |
| C-18 | generated post 원문을 같은 user message/prompt 안에 그대로 삽입하고 `today_context` 전체가 belief로 우회 유입 | 게시글 속 명령문이 system/task 지시처럼 작동해 STB→LTB로 영구화될 수 있음 | role/canonical serializer 분리, untrusted taint, exact output containment, server transition/output validator; delimiter만으로 통과 금지 |
| C-19 | Best·선택 원문과 interpretation claim의 의미·진실성은 server 검증 대상이 아님 | evidence-linked memory를 fact-check 통과 결과로 오해할 수 있음 | event type을 `observation`과 `agent_interpretation/claim`으로 분리. validator는 visibility·ownership·exact quote provenance·privacy만 검증하고, 의미·신뢰도 판단은 agent에게 맡긴다. |
| C-20 | 한 agent당 posting decision은 하루 한 번이고 자기 글은 candidate에서 제외 | 여러 글 작성·자기 글 반응이 가능한 일반 커뮤니티와 다름 | 원 설계의 제한으로 명시하고 그대로 유지 |

### 27.2 Memory·belief·실행 경로

| ID | 현재 구현/초안의 문제 | 위험 | 본 설계의 처리 |
|---|---|---|---|
| M-01 | `get_previous_belief()`가 저장된 여섯 차원을 버리고 `belief_summary`만 반환 | canonical 관점이 손실되고 요약 문장이 실제 causal memory가 됨 | 기존 `belief_history.dim_1~dim_6` 전체를 previous LTB로 복원; summary/change는 사람용으로만 보존 |
| M-02 | `daily_cycle.py`의 agent task가 gather 완료 전 belief/trade를 직접 DB에 씀 | 100명 barrier 실패 시 partial scientific state | side-effect-free staged `AgentTurnResult` 뒤 deterministic batch commit |
| M-03 | response journal을 runtime rollback에 포함하면 성공 응답도 사라짐 | crash 때 99개 성공 call을 재호출 | condition별 외부 durable journal + runtime consumption/digest의 two-phase reconciliation |
| M-04 | 원 pipeline에는 current news interpretation과 recent order history의 direct path가 있음 | same-turn STB와 raw news가 함께 들어가면 동일 신호를 이중 계상하고 과거 거래가 LTB 성찰을 우회 | raw news/community interpretation과 historical order/fill direct path를 제거하고, analysis/decision에는 previous LTB + current STB와 현재 execution state만 분리 block으로 제공 |
| M-05 | `dim_1`은 약 한 달 전망인데 H1/H5로 판정하려는 유혹 | horizon mismatch와 잘못된 자기성찰 | H1/H5는 timing/sizing/action-aligned mark만; forecast accuracy는 별도 horizon field 없이는 계산 금지 |
| M-06 | 한 fill 뒤 다른 거래가 portfolio PnL을 바꿈 | 전체 PnL을 한 fill의 성과로 잘못 귀속 | fill reference price 기반 mark만 사용; portfolio PnL은 별도 aggregate outcome |
| M-07 | 6차원 한국어 belief에서 거래 의도 방향을 사후 추론하기 어려움 | 임의 sentiment parser가 방향을 왜곡 | market-analysis가 제약 전 `directional_stance=buy/sell/uncertain`을 명시적으로 출력하고 실제 action과 분리 |
| M-08 | `belief_summary`, `view_change`를 현재 analysis/decision/posting이 실제 사용 | 같은 내용을 반복 가중하고 summary→자기 글→Best→다른 agent STB/LTB 우회 feedback 발생 | `belief_summary`는 모든 agent-visible path에서 제외한다. `view_change`는 parent/new LTB six-dimension diff와 integration evidence에서 deterministic하게 렌더하고, **post-writing private input에만** 허용한다. STB/LTB/analysis/decision/community interpretation serializer에는 제외하는 sentinel test를 둔다. |
| M-09 | turn-0 prior가 거의 동일 | persona heterogeneity가 초기 belief에 적게 반영 | 원 설계 최소 변경을 위해 neutral prior 유지; 두 arm byte-identical 검증. 다양화는 별도 amendment |
| M-10 | `update_belief.txt`가 AM·PM 공용인데 “매일 아침”이라고 씀 | PM agent에게 잘못된 시간 cue | subturn-aware 문구로 수정 |
| M-11 | 첫 turn cash-only·no-short라 SELL 불가 | 첫 BUY를 belief/community 효과로 오해 | feasible-action filter와 mechanical-first-BUY flag를 로그·분석에 반영 |
| M-12 | evidence ID가 존재해도 주장이 사실이라는 뜻은 아님 | provenance와 truth 혼동 | source/outcome observation, untrusted STB interpretation, LTB dimension transition을 별도 type으로 저장 |
| M-13 | `RN_COMM_ON`은 context가 길어 schema/transport retry가 더 많을 수 있음 | treatment가 내용 외에 generation 품질 차이까지 포함 | arm×stage retry·truncation·latency 감사와 preregistered pause threshold |
| M-14 | `run_agent_turn()` 반환 시점에는 실제 exchange fill과 post-trade portfolio가 아직 확정되지 않음 | proposal을 actual fill로 오인하거나, current fill을 pre-fill STB/analysis/decision에 넣어 시간 역전 | exchange가 actual fill/post-portfolio를 확정한 뒤에만 structured transaction episode를 만들고, post-fill LTB updater는 parent LTB·current STB·**that actual episode**·eligible **earlier price outcome**를 쓴다. future outcome은 넣지 않는다 |
| M-15 | 기존 market analysis가 belief와 news를 받고 decision이 belief와 analysis를 다시 받음 | 파생정보 echo가 이미 존재하며 STB/LTB 추가 시 더 커질 수 있음 | raw news/community direct path를 제거하고 analysis/decision에는 previous LTB와 current STB만 별도 belief block으로 제공; summary/change 및 root-evidence 중복 audit |
| M-16 | 모든 turn에 LTB updater를 호출하면 변화가 없어도 새 텍스트가 생길 수 있음 | 단순 재서술이 장기 belief 강화로 오인 | 매 turn 새 작성은 유지하되 여섯 dimension no-copy, parent material assertion preservation, current integration-evidence lineage를 함께 검증하고 변화 자체를 독립 support로 세지 않음 |
| M-17 | 차원별 단일 text는 여러 장기 주장을 부분 correction할 때 일부를 잃을 수 있음 | 한 주장 수정 때 다른 기억이 조용히 소실·재서술 | v1은 6D thin adaptation을 유지하고 before/after hash·evidence edge·short/current-`U`/extended adversarial preservation gate를 둠. gate 실패 시 본 실행 NO-GO 후 stable-item 구조를 별도 amendment |
| M-18 | H1/H5 가격 성찰은 개인 수급 방향 label을 학습하는 신호가 아님 | 가격에 유리한 매매와 실제 개인 반응 모사가 서로 다를 수 있음 | target label은 memory에 넣지 않되 LTB 효과를 과장하지 않고 daily reaction alignment와 price-only baseline을 외부 평가 |
| M-19 | current `belief_history` dim field가 nullable이고 `INSERT OR REPLACE` | 누락 차원·동일 key의 상이 payload가 조용히 덮어써짐 | six dims NOT NULL/schema validation, insert-or-identical-hash, 다른 hash 즉시 pause |
| M-20 | analysis가 unknown extra key를 허용하고 `generation_attempts` 같은 retry metadata가 causal dict에 섞임 | prompt injection key 또는 arm별 retry 차이가 다음 STB/결정의 treatment cue가 됨 | 모든 stage exact key set·`additionalProperties=false`; audit metadata는 causal payload 밖에 저장 |
| M-21 | scientific phase digest가 canonical six dimensions보다 summary/change에 치우쳐 있음 | 실제 LTB가 바뀌어도 integrity hash가 놓치고 사람용 문장 차이는 과대반영 | STB/LTB six dimensions·integration evidence·parent/source lineage·reflection packet·fill/portfolio를 scientific digest에 포함, human-log digest 분리 |
| M-22 | DB key가 주로 agent+turn이고 run/condition namespace가 약함 | 동일 numeric post/belief ID의 cross-arm/cache 오염이 정상처럼 보일 수 있음 | 모든 ID·FK·journal key에 run/condition/agent/turn 포함; reader-owned exposure ID 사용 |
| M-23 | `COMMISSION_RATE=0.0005`가 있으나 exchange/fill은 fee 0.0 | config와 runtime/보고서가 다른 fee 정책을 주장할 수 있음 | 이번 baseline은 `commission_rate=0`, `sell_tax_rate=0`, `fee_amount=0`을 config/exchange/portfolio/PnL/export/manifest에 동일 적용하고 불일치 시 fail |
| M-24 | 기존 persona는 belief·analysis·decision에 반복 주입되지만 memory용 causal role과 수정 금지 경계가 없음 | persona를 evidence처럼 반복 가중하거나 LTB가 strategy/depth를 임의 변경할 수 있음 | frozen sanitized persona를 STB 해석 렌즈·LTB read-only 일관성 제약·기존 거래 성향으로 사용; evidence count 0, persona hash drift 즉시 pause |
| M-25 | initial prompt의 `dim_1/5/6` 표현이 runtime update 정의와 다름 | market↔삼성전자, 해석 성향↔당일 해석, 능력평가↔최근 판단평가가 turn 0/1에서 조용히 바뀜 | §11.1 runtime 6D 정의를 canonical로 고정하고 turn-0 값은 해당 차원의 prior로 해석; prompt/schema hash freeze |
| M-26 | 최근 주문 query는 `actual_close`를 읽어도 formatter가 버리고 action·quantity·fill price 중심으로만 전달 | `dim_6`이 거래 결과를 성찰하라는 prompt를 받아도 객관적 markout 없이 그럴듯한 교훈을 환각 | server가 next-turn/H1/H5 markout을 deterministic event로 만든 뒤 first-visible/maturity 시점 LTB earlier price-outcome packet에만 전달; raw action reason이나 실제 개인수급 label로 대체 금지 |

### 27.3 설정·코호트·분석 경로

| ID | 발견 사항 | 위험 | 조치 |
|---|---|---|---|
| S-01 | DB depth는 30/55/15지만 config/report는 15/55/30, persona prompt는 60/100 불일치 | 정보권한과 모델 자기인식 오염 | DB의 agent별 depth를 canonical로 고정하고 prompt/report/config만 복구 |
| S-02 | Depth 1 prompt의 “10개 요약본 모두”와 actual slot 수·본문 semantics가 불일치할 수 있음 | 모델이 실제 노출보다 많은/적은 뉴스를 믿거나 shortage가 숨음 | paper bundle은 event별 target 10 real-news slot을 먼저 봉인한다. shortage 시 actual count·reason은 manifest/trace에 기록하고, agent prompt는 고정 숫자/quality notice 없이 허용된 actual payload만 직렬화한다. |
| S-03 | launcher가 30명을 하드코딩 | 100-agent 실험 불완전 | 새 2-arm launcher에서 100명 exact manifest 검증 |
| S-04 | `reasoning` request 자체와 live telemetry 검증이 없음; 숨김 설정도 없음 | 기존 실행이 reasoning on/off/hidden 중 무엇인지 불명이고 비용·지연 통제 불가 | 모든 physical attempt에 `effort:none` 중앙 강제, provider pin, returned metadata와 token 0 fail-closed; `exclude`만으로 통과 금지 |
| S-05 | primary normalization에 current total value를 쓰면 post-treatment denominator | community가 denominator도 바꿈 | initial capital을 primary, current value는 sensitivity |
| S-06 | `N`명은 공용 뉴스·가격·게시판을 공유 | `N×U` agent-turn/arm을 iid로 세면 과대확신 | paired repeated measures; single seed는 한 simulated-world 사례로 제한 |
| S-07 | validator `--skip-initial-days` 기본은 5, 최신 **legacy C00** headline은 skip 0·price baseline은 0/5만, `generate_report_artifact.py`는 5일 설명을 사용 | 같은 run이 분석 파일에 따라 45·42·40일로 바뀌고 사후 skip 선택 가능 | 45일 input 완전성 확인 뒤 명시된 첫 3개 날짜를 제외한 exact 42일만 primary. skip 0/1/5는 sensitivity이고 암묵 default 금지 |
| S-08 | “코스피 개인 방향” 표현과 실제 target이 혼용될 수 있음 | 코스피 전체 수급을 예측한 것으로 과대주장 | target은 삼성전자(005930) 실제 investor CSV의 `Individuals`로 고정 |
| S-09 | 원 validation은 당일 AM+PM 체결을 합치며 PM 종가·정보를 사용 | 이를 same-day 실시간 예측으로 부르면 시점 주장이 틀림 | primary는 사용자가 확정한 일별 거래반응 정합성/reconstruction; AM stance는 opening-nowcast 보조 진단 |
| S-10 | `num()`은 malformed/null을 0으로, `load_actual()`은 duplicate date를 overwrite, actual∩simulation만 평가 | 오염·누락일을 조용히 숨긴 결과 | strict parse/duplicate reject/exact 45-date set equality, 예상치 못한 flat fail |
| S-11 | fill action이 `buy`가 아니면 전부 sell로 계산 | `hold`, typo, 빈 action도 순매도로 오분류 | explicit buy/sell allowlist 외 nonzero exit; 별도 stance evaluator 구현 |
| S-12 | `actual_ratio_random`이 평가구간 실제 buy 비율을 사용 | test-label prevalence를 comparator가 미리 봄 | oracle-prevalence diagnostic으로 재명명하고 model comparator에서 제외 |
| S-13 | actual CSV는 일별 최종값만 있고 시간대별 개인 수급 label 없음 | AM을 “오전 개인 수급 예측”으로 과장 | AM은 같은 날 최종 label과 비교하는 opening nowcast; intraday claim 금지 |
| S-14 | `information_mode=same_day`가 허용되고 AM에서 close 외 당일 파생 feature가 남을 수 있음 | 미래 market feature leakage | `pre_close_cutoff` manifest 고정과 field-level as-of validator |
| S-15 | 실제 오전 기사 summary에 같은 날 “마감”·최종 고가·개인/외국인/기관 수급이 들어 있는 반례가 존재하고 processed CSV는 URL/last-modified/scraped/raw-version을 버림 | post-edited 기사·요약 hallucination을 원 publication time으로 backdate해 미래정보와 target proxy가 누출 | immutable cutoff-time article snapshot/provenance, AM/PM/search semantic scan, blinded reject/mask/allow manifest, target namespace 물리 분리 |
| S-16 | prompt/persona/config를 실행 중 다시 읽고 DB/WAL/hash 범위가 불완전할 수 있음 | 45일 중간 input drift와 archive digest 누락 | immutable run bundle, 매 phase hash check, archive 전 WAL checkpoint와 journal 포함 snapshot |
| S-17 | 뉴스 summary 전처리는 별도 Claude CLI 경로이고 reasoning/provenance 통제가 없음 | “reasoning-off 실험” 범위와 입력 시점이 불명확 | audited off pipeline으로 재생성 권장; 유지 시 runtime-only reasoning-off로 주장 제한하고 전처리 hash 공개 |
| S-18 | launcher가 offline stub도 완료 처리할 수 있음 | 실제 모델 실험이 아닌 결과가 paper run처럼 남을 수 있음 | paper launcher에서 offline env/API stub를 호출 전 fail-closed |
| S-19 | `load_simulation()`은 fill status, positive integer quantity, finite/positive price, AM=open·PM=close, agent×date×subturn uniqueness를 검사하지 않고 빈 stock code도 `005930`으로 간주 | rejected/partial/중복/오염 fill이 정상 순거래처럼 합산되고 누락·중복이 상쇄될 수 있음 | arm당 expected `N×U` actual `filled` rows와 submitted orders를 manifest key set으로 대조; canonical field를 `fill_status`, `requested_quantity`, `filled_quantity`, `executed_price`로 고정하고 equality·stock/action/subturn/date를 strict fail-closed |
| S-20 | `sign_metrics()`는 flat-flat을 match로 세지만 `compute_direction_metrics()`는 세지 않고, 같은 summary의 `llm_vs_individuals`와 `primary_metrics`가 서로 다른 함수를 사용 | simulated flat이 있는 run에서 PDF·JSON·후속 보고서 headline이 불일치 | 단일 metric implementation만 사용. actual flat은 target error, predicted flat은 2×3 confusion에 남기고 non-flat 실제 class의 miss로 계산 |
| S-21 | 전일 방향 baseline을 burn-in으로 잘린 배열에서 만든 뒤 shift해 첫 평가일을 0으로 채움 | 2026-03-05 평가에서 이미 관찰 가능한 2026-03-04 정보를 버려 baseline을 부당하게 약화 | 45일 전체에서 lag 생성 후 frozen 42-date mask 적용 |
| S-22 | raw, max-abs, z-score Pearson/cosine을 별도 증거처럼 반복하고 cumulative Pearson을 headline에 사용 | 양의 affine/scale 변환으로 사실상 같은 수치를 중복 제시하고 추세 상관을 반응 재현으로 과장 | BA를 primary로 두고 raw Pearson/Spearman만 secondary; 중복 normalization 제거, cumulative correlation과 naive p-value는 exploratory |
| S-23 | `analysis/paper_0721_c00_review/analyze_latest_c00.py`는 raw target이 아니라 이미 처리된 validation CSV를 읽고 inner merge하며, action non-buy를 sell로 만들고 `len(fills)==len(turns)`만 full-fill로 판정 | 선행 skip·교집합이 이중 적용되고 누락과 중복이 상쇄돼도 integrity가 pass할 수 있음 | 새 paired evaluator는 raw target·raw fills에서 독립 재계산하고 composite-key set equality와 2×3 confusion을 강제; legacy 분석은 참고 전용 |
| S-24 | `analysis/current_experiment_review/analyze_current_runs.py`의 일별 표는 actual fill이 아니라 submitted decision quantity를 집계하고, expected dates를 관찰된 date set에서 만들며 non-buy를 sell로 처리 | 실제 체결 반응과 주문 의도가 혼용되고 날짜 전체 누락을 발견하지 못함 | fill-based primary와 decision-intent diagnostic을 분리하고 approved date manifest를 외부 기준으로 사용 |
| S-25 | AM exchange 호출·audit row에 당일 미래 종가가 이미 전달·기록됨(`simulation.py:322-344,561-564`) | 현재 pre-close prompt의 직접 누출은 확인되지 않았지만 logger/context 재사용 시 accidental leakage와 as-of 오해 위험 | AM scientific/audit payload에는 open execution fact만 저장; EOD close는 PM 이후 evaluator-only/event table에 기록하고 raw-body/market field-level as-of test 수행 |
| S-26 | 기존 validator는 한 run directory씩만 평가하고 RN_COMM_OFF/RN_COMM_ON exact agent/date/subturn pairing·invariant hash를 검증하지 않음 | 두 arm의 누락·다른 cohort·다른 input이 community 효과처럼 보일 수 있음 | 새 evaluator에서 동일 resolved `N/D/U`·burn-in·news/price/persona/prompt/model hash와 `N×U` pair를 먼저 검증한 뒤 manifest evaluation-date contrast 생성 |
| S-27 | 50:50 random baseline도 고정 seed 한 번의 realized sequence만 보고 | 우연한 한 draw를 안정적 comparator처럼 해석 | 사전 seed 10,000회 분포·기대값·구간을 보고; actual-ratio random은 oracle diagnostic으로만 유지 |
| S-28 | evaluator 핵심 parse·date·flat·fill·AM/PM aggregation에 대한 직접 unit test가 없음 | 지표 정의가 바뀌거나 오염 입력을 받아도 실험 전 발견되지 않음 | strict parser, exact date/event keys, `N×|Q_d|` fills/day, duplicate/missing/status/price/action, flat, burn-in, paired-condition fixture를 P0 test suite에 추가; 현재 200/day는 resolved golden case |
| S-29 | `analyze_current_runs.py` 일부 correlation fallback이 `value_a or value_b` 형태라 정확한 `0.0`을 missing처럼 취급할 수 있음 | 상관 0이 대체값 또는 None으로 바뀌어 보고됨 | `is None`/finite 검사를 명시적으로 사용하고 0은 유효값으로 보존 |
| S-30 | 10억 agent는 인원 10%지만 초기자본 52.63%이고 모두 45세 이상 | raw signed value가 소수 rich persona에 지배되고 wealth 효과와 age 효과가 분리되지 않음 | 원 90/10 asset design은 유지하되 initial-capital-normalized·1억-only(=rich-excluded alias)/10억-only·leave-one-rich-out와 `wealth_fragile`을 필수 보고; demographic causal claim 금지 |
| S-31 | fixed demographic slot에 behavioral source를 `rng.choices` 가중매칭하며 minimum score gate가 없고 현재 `match_score`가 3~16 | constructed persona를 실제 삼성전자 개인투자자 대표 표본처럼 과대해석할 수 있음 | source/slot/score hash를 freeze하고 low-score 포함 사실 공개; subgroup은 prompt enactment descriptive, external calibration 전 population representativeness 주장 금지 |
| S-32 | persona prompt 100개 중 A001만 줄바꿈이 0개이고 문장 두 개가 붙어 있어 나머지 99개와 형식이 다름 | 의미 field가 같아도 A001 한 명에게만 비의도 formatting treatment가 생길 수 있음 | frozen structured field에서 100개 전체 serialization을 `render_persona_v1`로 재생성; canonical parse/round-trip 100/100, DB↔prompt 모든 의미 field mismatch 0, 비-depth 값 변화 0, ordered agent-prompt map hash와 두 arm byte identity 검증 |
| S-33 | checkpoint runner 기본이 30명·fake on·bearish이고 six-condition launcher는 조건 생략 시 fake arm 4개까지 전부 실행 | 옵션 누락 한 번으로 연구질문과 다른 유료 실행 | real-only 전용 2-arm launcher만 paper entrypoint로 허용하고 unsafe runner/default 결과의 manifest 등록 거부 |
| S-34 | fake-off `validate_news_inputs()`는 `is_fake`/`synthetic_id` 표식만 보고 canonical baseline equality는 fake-on에서만 검사 | 표식 없는 합성/변경 row가 fake 0으로 통과; 실제 red-team mutation이 `pass` 재현 | 승인 clean bundle의 exact schema/file/canonical-row/ordered-ID hash equality를 fake-off의 주 gate로 사용 |
| S-35 | `NewsAgent` filter·audit는 `is_fake`만 보며 plain runner는 `off + use-fake-news-injection` 충돌을 허용하고 preflight도 호출하지 않음 | `synthetic_id`-only/private-marker row나 injection CSV가 off 경로에 남을 수 있음 | filter가 아니라 fail; paper CLI에서 injection/news override 제거, core fake predicate 단일화와 private metadata family reject |
| S-36 | child signature에는 news hash가 있지만 six-condition study invariant에는 없고 RN_COMM_OFF/RN_COMM_ON pair 비교가 없음 | 한 arm 완료 뒤 CSV가 바뀐 다른 arm도 같은 study처럼 묶일 수 있음 | parent pair gate가 동일 resolved object·processed/daily/public-row/ID/slot hash를 첫 API 전에 비교 |
| S-37 | `news_20260427_섹터_0032`는 09:11 timestamp인데 당일 코스피 종가·장중고·최종 개인 순매도와 삼성전자 224,500원 마감을 포함 | 확정 EOD/target-proxy leakage가 structural preflight를 통과 | immutable cutoff version을 다시 확보하거나 row quarantine; 모든 semantic 후보를 blinded reject/mask/allow manifest로 처리 |
| S-38 | raw news는 URL/source가 전부 있으나 processed/runtime가 이를 버리고, daily selection 기본 seed는 `None`이며 seed manifest가 없음 | 실제 기사 lineage와 raw→slot 재생성이 실행 artifact에서 끊김 | provenance sidecar·exact ordered slot map·non-null seed·algorithm/version hash 봉인; agent-visible에는 필요한 title/summary만 projection |
| S-39 | trading dates는 StockData와 daily-news의 교집합에서 만들어지고 validator도 그 결과 dates만 검사 | daily에서 하루를 빼면 45→44일로 조용히 축소된 실행이 완전한 run처럼 보일 수 있음 | 외부 승인 exact 45-date hash를 입력으로 사용하고 교집합 생성 금지; 누락 한 날짜도 호출 전 fail |
| S-40 | clean-base builder가 알려진 runtime table blacklist만 지우며 paper run 중 mutable `sim.db`에서 자동 build/rebuild 가능; 현재 기본 experiment base는 0 byte | 새 STB/LTB/fake/cache table이나 변조 LTB₀가 남고, 잘못된 existing base는 즉시 실행을 막음 | 별도 승인 단계의 allowed-table/schema/row/digest allowlist base만 허용; 본 실행 중 생성·rebuild 금지 |
| S-41 | resume에서 현재 state digest를 마지막 committed digest와 비교하지 않고 새 `resume_state_sha256`로 기록하며, 현 digest는 six dimensions를 누락 | DB/LTB 변조와 잘못된 resume가 정상으로 이어지고 news file도 hash 뒤 교체 가능 | canonical scientific hash-chain exact compare, six dimensions·lineage 포함, phase 전 bundle re-hash와 mismatch pause |
| S-42 | final fake-off integrity는 주로 `fake_visible`을 보고 read/search/influential/evidence/journal의 clean origin closure를 검사하지 않음 | 표식 제거 row·stale fake journal/DB·검색 오염이 완료 artifact에 남을 수 있음 | 모든 external-news root의 clean-registry closure와 fake/private field/count/ID 0인 `clean_news_closure_report` 필수 |
| S-43 | 기존 category 목표는 5/3/2이나 backfill slot이 이를 벗어나고 1 slot은 9건 | “매 slot 5종목·3섹터·2경제”라고 보고하면 사실과 다르고 날짜별 정보 mix가 크게 변함 | 기존 데이터를 provenance-safe pool로 재검토해 event별 target 10 real-news slot을 우선 재선정·봉인한다. 그래도 부족하면 actual count·shortage reason을 공개하고 run은 계속한다. category composition은 실제 resolved slot map을 공개하며, 5/3/2를 만족하지 못하면 quota 충족을 허위로 주장하지 않는다. |

### 27.4 AM·PM 체결·방향 분석에서 확인된 정상 부분과 최종 판정

코드의 **정상 실행 경로 자체**는 사용자가 설명한 거래 방식과 맞는다.

- `simulation.py:319-370`은 AM 공시가격으로 실제 시가, PM 공시가격으로 실제 종가를 사용한다.
- `agents/exchange_agent.py:68-102`는 유효 주문을 제출 수량 그대로 공시가격에 전량 체결한다.
- `simulation.py:148-151`과 decision 경로는 BUY/SELL only이며 HOLD를 정상 action으로 허용하지 않는다.
- 따라서 정상 fill만 들어온다는 전제에서는 BUY `+filled_quantity×executed_price`, SELL `-filled_quantity×executed_price`를 AM+PM 날짜별로 합한 현재 기본 산식이 사용자 목적에 맞다.
- 기준 commit의 runtime import/path 감사에서는 실제 `Individuals` target CSV를 agent prompt가 직접 읽는 경로를 발견하지 못했다. 다만 기사 속 개인수급 문구와 same-day feature의 semantic leakage는 별도 문제라 그대로 P0다.

결론은 **거래 실행 골격과 현재 baseline의 known-injection overlap 0은 확인됐지만, 현재 논문용 evaluator·persona repair·실뉴스-only 실행 경로는 NO-GO**다. 집계 문제뿐 아니라 marker 없는 변경 row 우회, unsafe fake-on default, RN_COMM_OFF/RN_COMM_ON news pair gate 부재, 확정 EOD article-version leakage가 존재한다. §19.8~§19.9의 단일 계약과 S-07~S-29 evaluator 수정, S-30 wealth sensitivity, S-31 해석 제한, S-32 persona canonical repair, S-33~S-43 real-only/base/resume/lineage gate를 구현한 뒤에만 유료 본 실행과 논문 표 생성을 허용한다.

이 표의 “P0 확정 수정”은 더 이상 선택지가 아니다. “원 설계 유지” 항목은 구현을 그대로 두더라도 결과 해석과 manifest에 제한을 명시한다. treatment 의미를 바꿀 수 있는 값은 본문이 아니라 sealed external approval record에서 관리한다.

---

## 28. 외부 승인 레지스터

이 문서는 이미 확정된 설계만 기록한다. 구현·입력·treatment semantics를 바꿀 수 있는 승인값은 대화와 별도 approval log에서 관리하고, P2 전에 sealed `StudySpec`/manifest의 명시값으로 옮긴다. 이 문서에는 미결정 선택지 목록을 남기지 않는다. placeholder·pending·null 값이 남은 manifest는 본 실행에 사용할 수 없다.

---

## 29. 최종 권고

이번 실험에서 계층 메모리는 “오래된 모든 것을 매번 prompt에 넣는 기능”이 아니다. 다음 원칙을 지켜야 한다.

1. **STB는 current-only `dim_1~dim_6`이고 이전 belief·거래 성과·portfolio state를 carry하지 않는다.**
2. **거래는 `previous LTB + current STB`를 분리된 두 block으로 사용해 Decision-Making을 하고, exchange가 actual `fill_t`를 확정한 뒤 LTB는 `previous LTB + current STB + decision/fill episode + eligible earlier price outcome`으로 재귀 갱신한 같은 여섯 축이다. 기존 `belief_history`는 이 next-event-visible LTB를 맡는다.**
3. **Frozen persona는 STB의 해석 렌즈, LTB의 read-only 일관성 제약, 거래의 행동 성향으로 반영하되 evidence로 세거나 memory가 수정하지 않는다.**
4. **뉴스·community의 raw/direct path는 제거하고 STB를 통해서만 거래에 전달한다. 시장가격·portfolio·constraint는 direct execution-state block으로 유지한다.**
5. **current fill은 ledger에 즉시 저장하고 same-turn post-fill LTB의 structured transaction episode로 한 번 해석한다. 가격 성찰은 다음 due event부터 별도로 `dim_6`에만 반영하며 실제 개인수급 평가는 항상 memory 밖 evaluator에서 구분한다.**
6. **HOLD 없이 BUY/SELL 방향과 수량을 강제 관찰하며 제약이 방향을 강제한 turn을 별도 표시한다.**
7. **Community는 실제 title/full-body 노출과 next-AM 경계를 지키며 `RN_COMM_ON`에서만 STB→거래 뒤 LTB 경로로 지속될 수 있다.**
8. **실뉴스-only는 marker 0이 아니라 승인된 immutable article-version bundle과 모든 downstream news-root closure로 증명한다. 현재 2026-04-27 EOD leakage row를 처리하기 전에는 실행하지 않는다.**

FUSE의 current short state→recursive long state 골격을 가져오되, 같은 signal의 same-turn double counting을 막기 위해 `previous LTB + current STB`로 먼저 Decision-Making을 하고 actual fill 뒤 `decision/fill episode`를 LTB updater의 별도 structured input으로 준다. FUSE의 별도 opinion state까지 복사하지 않는다. 삼성 baseline에는 이미 거래용 six-dimension belief가 있으므로 previous LTB와 current STB를 그 자리에 분리해 넣는 것이 가장 얇고 설명 가능한 adaptation이다. 필요한 core prompt는 current-only STB prompt와 post-trade `update_long_term_belief.txt` 두 개이며, 별도의 세 번째 fused-belief prompt는 만들지 않는다.

본 실행 전에는 **승인 real-news bundle exact identity·article-version 누출·RN_COMM_OFF/RN_COMM_ON news pair gate·downstream clean-ID closure, summary-only carryover와 downstream summary leakage, previous-LTB/current-STB Decision-Making→actual fill→post-fill LTB 갱신, current-fill/outcome 경계, 100-agent atomic commit, exact-schema/untrusted-text containment, persona depth 오염, fixed demographic/90:10 asset integrity와 rich-capital dominance sensitivity, Best5 원문·Depth0 경로, reasoning-off live 검증, strict 45-day-input/3-day-burn-in/42-day-metric evaluator, fee 불일치, 전용 100-agent launcher, response journal**을 함께 해결해야 한다. 하나라도 빠지면 개인 방향 변화, community treatment, 장기 belief 경로, 비용 통제, 재현성 중 하나 이상을 사후에 입증할 수 없다.

---

## 부록 A. Authoring spec과 resolved manifest 예시

### A.1 사람이 승인하는 `study_spec.json`

아래 파일만 사람이 작성·승인한다. exact 100 agent 목록·agent별 depth/자산/persona hash는 cohort registry에, exact 날짜별 decision-event/cutoff/execution-price 목록은 calendar-event registry에 둔다. resolver는 두 registry를 읽어 `required_agent_count=100`과 cohort assertion을 검사한다. `N/D/U`, start/end, 45/90/42, active 70, expected row/call 수는 여기에 쓰지 않는다.

```json
{
  "artifact_type": "study_spec",
  "study_id": "realnews_comm_ab_hmem_v1",
  "design_version": "2.0.0-stb-ltb",
  "baseline_commit": "8604f9aec041c9929e327a90cc9025b650e9fab6",
  "required_agent_count": 100,
  "cohort_registry_sha256": "<ordered-exact-100-agent-registry>",
  "persona_depth_manifest_sha256": "<sealed-agent-id-to-news-depth-assignment>",
  "persona_assignment_policy": "study-specific-frozen-db-map__prompt-is-deterministic-projection__no-independent-depth-edit",
  "persona_renderer_sha256": "<required-render_persona_v1-code-template-hash>",
  "cohort_assertions": {
    "depth_counts": {"0": 30, "1": 55, "2": 15},
    "initial_cash_counts": {"100000000": 90, "1000000000": 10}
  },
  "condition_treatments": {
    "RN_COMM_OFF": {"community_mode": "off", "news_treatment": "real_only"},
    "RN_COMM_ON": {"community_mode": "on", "news_treatment": "real_only"}
  },
  "paired_condition_groups": [["RN_COMM_OFF", "RN_COMM_ON"]],
  "treatment_diff_allowlist": ["community_mode"],
  "calendar_event_registry_sha256": "<ordered-date-and-decision-event-registry>",
  "burn_in_date_ids": ["2026-02-27", "2026-03-03", "2026-03-04"],
  "regime_policy_sha256": "<required>",
  "real_news_bundle_manifest_sha256": "<approved-clean-bundle>",
  "news_exposure_policy_sha256": "<slot-quota-search-lookback-and-cutoff-policy>",
  "community_policy": {
    "best_k": 5,
    "best_selection_policy": "top_k_or_fewer_available_no_forced_posting",
    "permissions_from_cohort_depth_map": true,
    "depth1_selective_read_cap": 5,
    "depth2_selective_read_cap": 10,
    "best_payload": "title_plus_full_frozen_body",
    "visibility": "next_approved_am_decision_event"
  },
  "context_window_policy": {
    "decision_historical_order_or_fill_direct_visibility": "forbidden",
    "community_public_author_private_portfolio_or_trade_visibility": "forbidden",
    "trade_memory_visibility": "current_actual_fill_only_via_same_turn_postfill_ltb_transaction_episode__due_horizon_records_only_via_ltb_dim6_price_outcome",
    "depth2_search_lookback": 7,
    "depth2_search_lookback_unit": "calendar_days",
    "depth2_search_top_k": 10,
    "news_category_targets": {"stock": 5, "sector": 3, "economy": 2},
    "market_feature_policy_sha256": "<MA5-MA20-20trading-day-volatility-and-as-of-definition>"
  },
  "memory_policy": {
    "version": "stb-ltb-decision-fill-postfill-v4",
    "cadence": "each_manifest_decision_event",
    "trade_belief_blocks": "previous_ltb_plus_current_stb_separate_blocks",
    "ltb_update_timing": "after-actual-fill-before-atomic-commit-next-event-visible",
    "current_transaction_episode_input": "committed_actual_decision_fill_prepost_portfolio_outcome_pending_once_same_turn_dim6_context",
    "price_outcome_input": "newly-visible-earlier-fill-outcomes-only_dim6_evidence",
    "outcome_horizons": ["next-decision-event", "same-subturn-plus-1-trading-date", "same-subturn-plus-5-trading-dates"]
  },
  "trade_policy": {
    "stock_code": "005930",
    "decision_space": ["buy", "sell"],
    "allow_hold": false,
    "max_single_trade_cash_ratio": 0.5,
    "fill_policy": "full_fill_at_event_reference_price",
    "commission_rate": 0.0,
    "commission_applies_to": [],
    "sell_tax_rate": 0.0,
    "fee_policy": "zero_fee_baseline_all_fee_amounts_must_be_zero",
    "target_direction_notional": "gross_signed_fill_value"
  },
  "model_policy": {
    "model": "qwen/qwen3.5-flash-02-23",
    "provider": "alibaba",
    "reasoning": {"effort": "none", "exclude": true},
    "allow_provider_fallbacks": false,
    "require_parameters": true,
    "reasoning_off_canary_required": true,
    "reasoning_off_success_contract": "returned_model_and_provider_match__reasoning_fields_empty__reasoning_tokens_exactly_zero"
  },
  "study_seed": 2,
  "seed_namespace": "study-agent-date-subturn-stage-logical-validation-attempt-v1",
  "retry_policy_sha256": "<stage-specific-policy>",
  "runtime_policy_sha256": "<worker-concurrency-rpm-tpm-policy>",
  "evaluation_policy_sha256": "<target-metric-sensitivity-mask-policy>"
}
```

`cohort_assertions`의 100·30/55/15·90/10은 이번 study가 요구하는 값을 확인하는 제약이다. agent 목록·depth·자산을 생성하는 두 번째 원천이 아니다. registry와 assertion이 다르면 registry를 자동 수정하지 않고 실패한다. `context_window_policy`는 historical order/fill과 author private portfolio/trade의 direct visibility를 금지하고, Depth 2 search 7 calendar days/top-10·MA5/MA20/20-day feature 같은 실제 노출 window를 명시적으로 freeze한다. 이 값은 **LTB 보존기간**이 아니며, canary 결과를 보고 몰래 튜닝하지 않고 변경 시 policy amendment와 양 arm 재실행이 필요하다.

필수 외부 registry의 최소 row 계약은 다음과 같다.

| registry | canonical ordered row | 제약 |
|---|---|---|
| cohort | `(ordinal, agent_id, news_depth, initial_cash, persona_hash, fixed_slot_hash)` | unique·연속 ordinal, current paper row 수 exact 100, **sealed DB-derived agent_id→news_depth map과 prompt parser의 depth/permission이 100/100 일치**, 임의 first-N/교체·depth 재배정 금지 |
| calendar-event | `(date, event_ordinal_in_date, decision_event_id, cutoff/window, execution-price field, feature-as-of, decision_enabled)` | date/event unique, timezone KST, 전체 순서 명시, stock/news 교집합으로 재생성 금지 |
| community-phase | `(phase_id, after_event_id, eligible permission policy, next_visible_event rule)` | decision event와 분리; memory update count에 미포함 |
| real-news slot | `(decision_event_id, slot_ordinal, news_id, visible_payload_hash)` | ordered safe IDs; target 10을 먼저 충족. 10 미만은 actual count/reason/approval hash가 있는 frozen shortage exception만 허용하며 임의 tolerance는 금지 |

현재 AM/PM calendar-event row의 **형식 예시**는 아래와 같다. 실제 asset에는 승인된 모든 거래일을 이 순서로 완전히 열거하며 resolver가 global ordinal을 부여한다.

```json
{
  "date": "2026-03-05",
  "timezone": "Asia/Seoul",
  "decision_events": [
    {
      "decision_event_id": "2026-03-05/AM",
      "event_ordinal_in_date": 1,
      "subturn": "AM",
      "news_window": {"start": "previous-trading-date 15:30 exclusive", "end": "2026-03-05 08:59 inclusive"},
      "market_feature_as_of": "2026-03-05 09:00",
      "execution_price_field": "actual_open",
      "consume_scheduled_community": true,
      "decision_enabled": true
    },
    {
      "decision_event_id": "2026-03-05/PM",
      "event_ordinal_in_date": 2,
      "subturn": "PM",
      "news_window": {"start": "2026-03-05 08:59 exclusive", "end": "2026-03-05 15:30 inclusive"},
      "market_feature_as_of": "2026-03-05 15:30",
      "execution_price_field": "actual_close",
      "consume_scheduled_community": false,
      "decision_enabled": true
    }
  ],
  "post_decision_phases": [
    {"phase_id": "2026-03-05/community", "after_event_id": "2026-03-05/PM", "next_visible_event_rule": "next-approved-AM"}
  ]
}
```

`previous-trading-date`와 `next-approved-AM`은 코드가 임의 날짜 교집합을 찾으라는 뜻이 아니라, **이 registry 안의 직전/다음 ordered row**를 resolver가 참조하라는 뜻이다.

### A.2 resolver가 생성하는 `resolved_study_manifest.json`

다음 큰 manifest는 authoring 파일이 아니라 **읽기 전용 resolver output 예시**다. 정규화된 입력 사본, exact registry hash, 파생 count/key-set/hash를 함께 담아 감사에 사용한다. 실행기·validator·분석기는 이 파일의 동일 SHA-256을 받아야 하며, 사람이 여기의 45/90/42 같은 파생값을 편집해서는 안 된다.

```json
{
  "artifact_type": "resolved_study_manifest",
  "source_study_spec_sha256": "<required>",
  "study_id": "realnews_comm_ab_hmem_v1",
  "design_version": "2.0.0-stb-ltb",
  "baseline_commit": "8604f9aec041c9929e327a90cc9025b650e9fab6",
  "conditions": [
    "RN_COMM_OFF",
    "RN_COMM_ON"
  ],
  "condition_treatments": {
    "RN_COMM_OFF": {"community_mode": "off", "news_treatment": "real_only"},
    "RN_COMM_ON": {"community_mode": "on", "news_treatment": "real_only"}
  },
  "treatment_diff_allowlist": ["community_mode"],
  "agent_count": 100,
  "agent_ids_sha256": "<required>",
  "fixed_slots_sha256": "<required>",
  "persona_db_sha256": "<required>",
  "persona_depth_manifest_sha256": "<required-and-identical-across-arms>",
  "persona_assignment_source": "sealed-study-specific-agent-id-to-news-depth-map",
  "persona_demographic_projection_sha256": "<required>",
  "persona_source_match_scores_sha256": "<required>",
  "persona_repair_manifest_sha256": "<required>",
  "persona_prompt_renderer": "render_persona_v1_nfc_lf_one-trailing-lf_fixed-sections",
  "persona_prompt_agent_hash_map_sha256": "<required-and-identical-across-arms>",
  "persona_prompt_canonical_roundtrip_count": 100,
  "persona_prompt_db_semantic_mismatch_count": 0,
  "persona_prompt_depth_permission_mismatch_count": 0,
  "persona_profile_policy": "frozen-read-only-not-evidence-v1",
  "depth_distribution": {"0": 30, "1": 55, "2": 15},
  "initial_cash_distribution": {"100000000": 90, "1000000000": 10},
  "total_initial_cash_krw": 19000000000,
  "large_cash_group_initial_capital_share": 0.5263157895,
  "large_cash_agent_ids_sha256": "<required>",
  "start_date": "2026-02-27",
  "end_date": "2026-05-04",
  "trading_days": 45,
  "trading_dates": [
    "2026-02-27", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06",
    "2026-03-09", "2026-03-10", "2026-03-11", "2026-03-12", "2026-03-13",
    "2026-03-16", "2026-03-17", "2026-03-18", "2026-03-19", "2026-03-20",
    "2026-03-23", "2026-03-24", "2026-03-25", "2026-03-26", "2026-03-27",
    "2026-03-30", "2026-03-31", "2026-04-01", "2026-04-02", "2026-04-03",
    "2026-04-06", "2026-04-07", "2026-04-08", "2026-04-09", "2026-04-10",
    "2026-04-13", "2026-04-14", "2026-04-15", "2026-04-16", "2026-04-17",
    "2026-04-20", "2026-04-21", "2026-04-22", "2026-04-23", "2026-04-24",
    "2026-04-27", "2026-04-28", "2026-04-29", "2026-04-30", "2026-05-04"
  ],
  "trading_dates_canonical_json_sha256": "398e367a68ea5231eb5a43db68c0681e1bcfd33c3003a9c2462fbe83ebf866d9",
  "evaluation_burn_in_days": 3,
  "evaluation_burn_in_dates": ["2026-02-27", "2026-03-03", "2026-03-04"],
  "evaluation_burn_in_dates_canonical_json_sha256": "6589ab20d5d3ef5a7105c254a957b20fef9f3a6c9487a330640a34fd0e0caaae",
  "primary_evaluation_days": 42,
  "primary_evaluation_dates": [
    "2026-03-05", "2026-03-06", "2026-03-09", "2026-03-10", "2026-03-11",
    "2026-03-12", "2026-03-13", "2026-03-16", "2026-03-17", "2026-03-18",
    "2026-03-19", "2026-03-20", "2026-03-23", "2026-03-24", "2026-03-25",
    "2026-03-26", "2026-03-27", "2026-03-30", "2026-03-31", "2026-04-01",
    "2026-04-02", "2026-04-03", "2026-04-06", "2026-04-07", "2026-04-08",
    "2026-04-09", "2026-04-10", "2026-04-13", "2026-04-14", "2026-04-15",
    "2026-04-16", "2026-04-17", "2026-04-20", "2026-04-21", "2026-04-22",
    "2026-04-23", "2026-04-24", "2026-04-27", "2026-04-28", "2026-04-29",
    "2026-04-30", "2026-05-04"
  ],
  "primary_evaluation_dates_canonical_json_sha256": "75a4c2f5ecf84557ce78de3b83637b854faddff04f43377798ea129ca3bd92ba",
  "date_hash_encoding": "sha256-utf8-compact-json-array-no-trailing-newline",
  "primary_target": "sign_of_005930_individuals_net_trading_value",
  "primary_prediction": "sign_of_resolved_cohort_daily_actual_fill_signed_value",
  "target_manifest_sha256": "<required>",
  "evaluator_version": "realnews-community-ab-evaluator-v1",
  "rq1_aggregation_version": "daily-sum-resolved-cohort-decision-events-filled-notional-v1",
  "rq2_aggregation_version": "agent-first-am-pm-sum-fixed-initial-cap-normalize-then-mean-v1",
  "paired_fill_key": ["agent_id", "date", "subturn"],
  "canonical_fill_fields": ["fill_status", "requested_quantity", "filled_quantity", "executed_price"],
  "accepted_fill_status": "filled",
  "full_fill_invariant": "requested_quantity_equals_filled_quantity",
  "wealth_sensitivity_version": "wealth_sensitivity_v1",
  "wealth_fragile_algorithm": "any_leave_one_rich_core_p3b_pass_diff_or_rq2_mean_sign_or_zero_transition_v1",
  "robust_p3b_gate": "core_p3b_pass_and_not_wealth_fragile",
  "wealth_sensitivity_outputs": ["raw_100", "one_eok_90_alias_rich_excluded", "ten_eok_10", "initial_capital_normalized_equal_agent", "leave_one_rich_out_10", "wealth_fragile"],
  "wealth_fragile_policy": "blocks_robust_claim_and_fake_news_extension_not_run_integrity",
  "decision_event_policy": "ordered-per-date-event-map-v1",
  "calendar_event_registry_sha256": "<required-from-study-spec>",
  "decision_turn_map_sha256": "<required>",
  "count_derivation_version": "derive-A-N-D-U-B-E-P-and-key-sets-v1",
  "resolved_counts": {
    "conditions": 2,
    "agents": 100,
    "trading_dates": 45,
    "burn_in_dates": 3,
    "primary_evaluation_dates": 42,
    "community_phase_keys_per_arm": 45,
    "decision_turns_per_agent": 90,
    "stb_updates_per_agent": 90,
    "ltb_updates_per_agent": 90,
    "ltb_states_per_agent_including_ltb0": 91,
    "agent_decision_keys_per_arm": 9000,
    "fills_per_arm": 9000,
    "active_community_agents": 70,
    "best_audience_agents": 100,
    "source": "computed-not-user-authored"
  },
  "expected_key_set_hashes": {
    "decision_events": "<required>",
    "stb": "<required-equal-logical-domain-to-ltb-transition>",
    "ltb_transitions": "<required-equal-logical-domain-to-stb>",
    "decisions": "<required>",
    "fills": "<required>",
    "community_phase_opportunities": "<required>",
    "community_schedules": "<required>",
    "news_slots": "<required>",
    "outcome_due_or_censored": "<required>"
  },
  "fake_news_mode": "off",
  "news_policy": "approved-real-news-exogenous-feed-target10-shortage-recorded-v2",
  "real_news_raw_snapshot_sha256": "<required-after-leakage-review>",
  "real_news_processed_sha256": "<required-after-leakage-review>",
  "real_news_daily_sha256": "<required-after-leakage-review>",
  "real_news_exact_schema_sha256": "<required>",
  "real_news_canonical_public_rows_sha256": "<required-and-identical-across-arms>",
  "real_news_ordered_clean_id_registry_sha256": "<required-and-identical-across-arms>",
  "real_news_exact_slot_map_sha256": "<required-and-identical-across-arms>",
  "target_real_news_articles_per_event": 10,
  "target_real_news_article_slots": 900,
  "real_news_slot_ordinal_policy": "each_decision_event_has_ordered_unique_safe_news_ids_up_to_target_10_with_actual_count_and_shortage_status",
  "real_news_shortage_policy": "reselect_safe_pool_first_then_continue_with_frozen_actual_count_and_shortage_record_no_duplicate_no_synthetic_no_unsafe_backfill",
  "news_shortage_exception_manifest_sha256": "<required-or-empty-manifest-hash>",
  "real_news_shortage_log_fields": ["exception_id", "news_coverage_status", "target_real_count", "selected_safe_count", "serialized_count", "delivered_real_count", "actual_real_count", "missing_real_count", "candidate_pool_digest", "candidate_pool_count", "selection_algorithm", "selection_seed", "selection_review_reason", "ordered_news_id_payload_hashes", "pair_bundle_hash", "approval"],
  "real_news_selection_seed": "<required-non-null-integer>",
  "real_news_selection_algorithm": "<required-version-and-code-sha256>",
  "known_injection_registry_sha256": "<required>",
  "known_injection_overlap": {"id": 0, "title": 0, "row_hash": 0},
  "external_news_id_closure_policy": "visible-read-search-influential-evidence-journal-subset-of-clean-registry-v1",
  "article_version_leakage_review_manifest_sha256": "<required>",
  "agent_visible_news_projection": "title-plus-summary-truncated-at-220-then-ellipsis-not-full-article-body",
  "news_version_manifest_sha256": "<required>",
  "information_mode": "pre_close_cutoff",
  "model": "qwen/qwen3.5-flash-02-23",
  "provider": "<pinned-after-canary>",
  "reasoning": {"effort": "none", "exclude": true},
  "seed": 2,
  "memory_policy_version": "stb-ltb-decision-fill-postfill-v4",
  "short_term_belief_schema": "current_only_dim_1_to_dim_6",
  "long_term_belief_schema": "previous_ltb_plus_current_stb_plus_current_decision_fill_episode_plus_eligible_earlier_price_outcome_to_dim_1_to_dim_6",
  "same_dimension_recursive_fusion": true,
  "dimension_transition_schema": "parent_stb_transaction_episode_price_outcome_before_after_hash_integration_evidence_v3",
  "ltb_evidence_policy": "current_stb_or_eligible_price_outcome_evidence_ids_plus_server_resolved_parent_chain_community_uses_claim_ids__current_transaction_episode_is_non_evidentiary_dim6_context",
  "ltb_transition_subset_policy": "same_dimension_subset_of_current_stb_or_dim6_eligible_price_outcome_evidence",
  "memory_retrieval": "none",
  "memory_decay": "none",
  "standalone_reflection_memory": "none",
  "dimension_internal_state": "single_text_no_stable_items",
  "drift_failure_policy": "pause_no_automatic_fallback",
  "canonical_trade_belief": "previous_ltb_plus_current_stb_separate_blocks",
  "initial_state": "ltb_0_only",
  "ltb0_generation": "deterministic_base_copy_no_api",
  "non_scientific_rendered_fields": ["belief_summary", "view_change"],
  "belief_summary_visibility": "never_agent_visible",
  "post_writing_view_change_exception": "only_stage_qualified_ltb_t_plus_deterministic_view_change_plus_committed_pm_fill_t",
  "human_log_renderer": "deterministic_from_applied_ltb_v1",
  "human_log_renderer_code_sha256": "<required>",
  "ltb_hash_namespaces": {"causal": "ltb_scientific_hash", "human": "human_log_hash"},
  "memory_update_cadence": "exactly-one-stb-and-one-ltb-update-per-manifest-decision-turn",
  "memory_count_invariant": "committed_stb_count_equals_committed_ltb_update_count_equals_resolved_U",
  "ltb_state_count_invariant": "committed_ltb_state_count_equals_resolved_U_plus_one_ltb0",
  "belief_link_mode": "current_external_evidence_to_stb_then_previous_ltb_plus_stb_to_decision_then_actual_fill_then_recursive_postfill_ltb_for_next_event",
  "ltb_trade_visibility": "next_decision_event",
  "fill_belief_visibility": "same_turn_post_fill_ltb_transaction_episode",
  "fill_fact_consumption": "once_at_same_turn_post_fill_ltb",
  "outcome_horizon_consumption": {
    "next_turn": "once_at_due_event",
    "h1_same_subturn_dplus1": "once_at_due_event",
    "h5_same_subturn_dplus5": "once_at_due_event"
  },
  "outcome_feedback_mode": "fill_episode_transaction_context_same_turn__price_outcomes_next_event_plus_same_subturn_Dplus1_plus_same_subturn_Dplus5__ltb_dim6_only",
  "price_outcome_horizons": ["next-decision-event", "same-subturn-plus-1-trading-date", "same-subturn-plus-5-trading-dates"],
  "decision_space": "buy_sell_only",
  "allow_hold": false,
  "max_single_trade_cash_ratio": 0.5,
  "execution_price": {"AM": "actual_open", "PM": "actual_close"},
  "fill_policy": "full_fill_at_announced_price",
  "commission_rate": 0.0,
  "commission_applies_to": [],
  "sell_tax_rate": 0.0,
  "fee_policy": "zero_fee_baseline_all_fee_amounts_must_be_zero",
  "target_direction_notional": "gross_signed_fill_value",
  "resolved_best5_audience": 100,
  "best_k": 5,
  "best_selection_policy": "top_k_or_fewer_available_no_forced_posting",
  "best_shortage_log_fields": ["available_post_count", "best_count", "actual_exposure_count"],
  "resolved_active_community_agents": 70,
  "best5_payload": "title_plus_full_frozen_body",
  "community_interpretation": "next_am_actual_exposure_exact_schema_v1",
  "community_claim_lineage": "community_claim_id_to_claim_sources_to_reader_source_exposure_ids_to_distinct_root_post_ids",
  "selected_best_overlap_policy": "serialize_body_once_preserve_two_source_exposures_two_claim_junctions_three_consumption_edges_one_distinct_root_support",
  "community_raw_body_visibility": {
    "selected_body": "pm_selected_read_reaction_and_next_am_interpretation",
    "best_only_body": "next_am_interpretation_only",
    "stb_or_ltb_registry": "never_raw_sanitized_claim_ids_hashes_polarity_only"
  },
  "empty_best_policy": "schedule_envelope_only_no_call_no_exposure",
  "final_day_best_policy": "nonempty_best_right_censored_empty_best_empty_no_exposure",
  "am_phase_dag_version": "exposure-interpretation-claim-stb-analysis-decision-fill-ltb-v2",
  "scientific_commit_scope": ["agent_exposure", "community_interpretation", "community_claim", "community_claim_source", "evidence_edge", "stb", "ltb", "analysis", "stance", "decision", "order", "fill", "portfolio", "ledger", "trace", "phase_call_consumption"],
  "per_run_workers": 30,
  "global_api_concurrency": 16,
  "offline_llm": false,
  "allow_provider_fallbacks": false,
  "paper_entrypoint": "scripts/09_run_realnews_community_ab.py",
  "child_argv_diff_allowlist": ["community_mode"],
  "sealed_output_namespace_policy": "new-or-exact-resume-no-symlink-no-current-no-legacy-v1",
  "clean_base_allowed_schema_sha256": "<required>",
  "clean_base_turn0_scientific_digest": "<required-and-identical-across-arms>",
  "clean_base_build_during_paper_run": false,
  "resume_digest_policy": "compare-current-to-last-committed-before-any-call-v1",
  "phase_input_rehash_policy": "code-prompt-persona-news-before-every-physical-attempt-v1",
  "code_tree_sha256": "<required>",
  "prompt_tree_sha256": "<required>",
  "base_db_sha256": "<required>"
}
```

위 JSON은 field 계약 예시다. `<required>`, `<freeze-...>`, `pending`, `TBD`, null은 **최종 full-run manifest schema에서 금지**하며 외부 승인 레지스터의 sealed 값과 canary 결과로 모두 치환해야 한다. 세 date hash는 표시된 배열을 UTF-8 compact JSON, trailing newline 없이 SHA-256한 값이다. 승인된 trading calendar가 한 날짜라도 달라지면 세 배열·개수·hash와 이 문서의 관련 수치를 함께 재검토한다.

## 부록 B. 단일 agent trace 예시

```text
D12 AM / T23
  current external evidence E-A041-T23 = current news N1203/N1204 only
  frozen persona P-A041 is an interpretation lens, not an evidence ID
  STB S-A041-T23 = P-A041-conditioned current external evidence → dim_1..dim_6
  market analysis receives previous LTB L-A041-T22 dims + STB S-A041-T23 dims
                    as separate blocks + AM price/portfolio/constraint execution state
  directional_stance=buy
  decision D-A041-T23 chooses BUY and quantity within allowed_actions
  full fill F-A041-T23 occurs at actual open
  transaction episode X-A041-T23 = D-A041-T23 + F-A041-T23 + pre/post portfolio + outcome_pending
  LTB L-A041-T23 = L-A041-T22 + S-A041-T23 + X-A041-T23
                      + eligible earlier price outcome R-A041-T23 under read-only persona hash P-A041
  human log derives belief_summary/view_change from L-A041-T23
  L-A041-T23 is visible_from_turn=T24; F-A041-T23 is not in S/analysis/decision,
  but is exactly once in post-fill L-A041-T23 as X-A041-T23

D12 PM / T24
  current external evidence includes PM news only
  STB S-A041-T24 = current external evidence only
  R-A041-T24 contains F-A041-T23's PM-close tentative mark + structured source/constraint snapshot
  analysis receives L-A041-T23 + S-A041-T24 + PM price/portfolio/constraint execution state
  directional_stance and decision choose BUY-or-SELL/quantity
  decision D-A041-T24 and full fill F-A041-T24 occur at actual close
  transaction episode X-A041-T24 = D-A041-T24 + F-A041-T24 + pre/post portfolio + outcome_pending
  LTB L-A041-T24 = L-A041-T23 + S-A041-T24 + X-A041-T24 + R-A041-T24
  F-A041-T24 is excluded from pre-fill stages but included once in L-A041-T24 as X-A041-T24;
  its price outcome becomes reflection-visible at T25

D12 post-PM community, `RN_COMM_ON` only
  A041 selected-reads posts C1308 and C1312; reader exposures X1308/X1312
  are consumed by PM reaction but are not trading-visible yet
  non-empty Best5 frozen title+full-body broadcast is scheduled for all 100
  Best actual exposure waits until D13 AM / T25 interpretation request

D13 AM / T25
  H1 (D+1 same-subturn) outcome for D12 AM fill and F-A041-T24's next-AM mark are observable
  and each newly due horizon record enters the eligible reflection packet R-A041-T25 exactly once
  selected bodies are consumed again by interpretation using X1308/X1312
  Best-only body creates reader exposure XB1315 when inserted in interpretation
  server validates claims Q1/Q2 and stores Q→claim-sources→X→distinct-root lineage
  current STB evidence receives sanitized Q IDs/text/claim-stance only
  raw community title/body is absent from both STB and LTB inputs
  STB S-A041-T25 may change dim_4/dim_5/dim_6 with dimension evidence Q1/Q2
  analysis/decision sees L-A041-T24 and S-A041-T25 as separate belief blocks,
  not human summaries or raw community text
  current AM decision/fill episode X-A041-T25 is staged after execution, then
  LTB L-A041-T25 = L-A041-T24 + S-A041-T25 + X-A041-T25 + R-A041-T25
  each integration evidence set is a valid STB or eligible-price-outcome subset;
  X-A041-T25 is mandatory non-evidentiary dim_6 context and its price outcome waits until T26
```

## 부록 C. 외부 근거

- [FUSE paper, ACL Anthology](https://aclanthology.org/2025.emnlp-main.1330/)
- [FUSE official repository](https://github.com/LiuYuHan31/FUSE)
- [FUSE `citizen.py` at inspected commit](https://github.com/LiuYuHan31/FUSE/blob/6ac7b856ffdc6706add42433f765ff632bbeaf4a/code/citizen.py)
- [FUSE `prompt.py` at inspected commit](https://github.com/LiuYuHan31/FUSE/blob/6ac7b856ffdc6706add42433f765ff632bbeaf4a/code/prompt.py)
- [TwinMarket paper, NeurIPS 2025](https://papers.neurips.cc/paper_files/paper/2025/file/5bf234ecf83cd77bc5b77a24ba9338b0-Paper-Conference.pdf)
- [TwinMarket official repository](https://github.com/FreedomIntelligence/TwinMarket)
- [FinMem paper](https://arxiv.org/abs/2311.13743)
- [FinMem official repository](https://github.com/pipiku915/FinMem-LLM-StockTrading)
- [FinMem current TSLA example config](https://github.com/pipiku915/FinMem-LLM-StockTrading/blob/main/config/tsla_gpt_config.toml)
- [TradingGPT paper](https://arxiv.org/abs/2309.03736)
- [Generative Agents paper, ACM DOI](https://doi.org/10.1145/3586183.3606763)
- [Generative Agents official repository](https://github.com/joonspk-research/generative_agents)
- [Reflexion paper](https://arxiv.org/abs/2303.11366)
- [OpenRouter reasoning-token controls](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
- [OpenRouter model metadata API](https://openrouter.ai/api/v1/models)
- [OpenRouter Qwen3.5-Flash model/API page](https://openrouter.ai/qwen/qwen3.5-flash-02-23/api)
- [OpenRouter provider routing and parameter enforcement](https://openrouter.ai/docs/guides/routing/provider-selection)
