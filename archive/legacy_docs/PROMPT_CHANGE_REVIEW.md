# Production 프롬프트 원본 대비 검토표

> 역사 문서: STB/LTB 분리 당시의 변경 검토 기록이다. 현재 실행·인수인계
> 정책은 루트의 네 정본 문서를 따르며, 삭제된 과거 prompt는 Git history에서만
> 확인한다.

상태: **통합 완료**. 최상위 `prompts/`가 현재 production 정본이며, 아래 표는 0720 기준 문구에서 달라진 이유를 기록한다.

## 그대로 복사된 4개

다음 파일은 0720 기준 문구를 그대로 유지한다.

- `initial_belief.txt`
- `news_agent_pre_search.txt`
- `news_agent_post_search.txt`
- `news_interpretation.txt`

## 최소 변형한 7개

| Production 파일 | 0720 원본 | 유지한 골격 | 꼭 바꾼 부분 |
|---|---|---|---|
| `community_reading.txt` | `../community_reading.txt` | select/react 구분, 선택·반응 원칙, JSON 구조 | RN의 server-owned post ID에 맞춰 ID를 string으로 명시하고, 독자에게 금지된 작성자 private portfolio/recent-trade 문구를 공개 프로필 기준으로 교체 |
| `community_thinking.txt` | `../community_thinking.txt` | Best/직접 읽은 글 구분, 분위기·공감·반대·불확실성 해석 | 실제 표시된 title-only 후보·독자 본인의 reaction·selected/Best full body를 노출 수준별로 분리하고, 자유로운 해석을 parser-safe claim JSON으로 직렬화하며 source exposure와 exact `supporting_quote`로 provenance를 기록 |
| `posting_decision.txt` | `../posting_decision.txt` | posting 여부·유형·제목·본문의 기존 3단계 | 요약문 대신 현재 post-fill LTB 6차원, 결정론적 view change, 실제 PM 체결만 입력하고 `will_post=false` 한-key object와 `will_post=true` 네-key object를 각각 유효한 strict JSON 예시로 분리 |
| `update_short_term_belief.txt` | `../update_belief.txt` | Belief 설명, 컨텍스트, Step 1~3, 6차원, 거래지시 금지 | 현재 뉴스·커뮤니티만 해석하고 `dimension_evidence`를 한 키 추가. 이전 LTB와 summary/change는 모델 입력·출력에서 제외 |
| `update_long_term_belief.txt` | `../update_belief.txt` | Belief 설명, 컨텍스트, Step 1~3, 6차원, 거래지시 금지 | 이전 장기 Belief·오늘 단기 Belief·체결·관찰 시점 도래 가격 결과를 통합하고 `integration_evidence`를 한 키 추가. summary/change는 서버 renderer가 만든다 |
| `market_analysis.txt` | `../market_analysis.txt` | Belief/시장/포트폴리오/뉴스 해석, 분석 목적, 7개 고려사항, 기존 9개 분석 키, 작성 원칙 | 기존 `today_belief` 슬롯에 봉인된 이전 LTB·현재 STB 여섯 차원만 함께 넣고 `directional_stance`, `evidence_references`만 추가 |
| `make_decision.txt` | `../make_decision.txt` | 모든 context 문단, 공시가 규칙, Step 1~3, 무효 출력 규칙, 예시 | 기존 `today_belief` 슬롯에 봉인된 이전 LTB·현재 STB 여섯 차원만 함께 넣고 `quantity`를 `requested_quantity`으로 교체 |

## 출력 키 계약

| 단계 | 정확한 최상위 JSON 키 |
|---|---|
| Short-Term Belief | `dim_1`~`dim_6`, `dimension_evidence` |
| Long-Term Belief | `dim_1`~`dim_6`, `integration_evidence` |
| 시장 분석 | 기존 9개 `market_view`, `valuation_view`, `technical_view`, `news_view`, `portfolio_view`, `key_risks`, `opportunity`, `caution`, `confidence` + `directional_stance`, `evidence_references` |
| 거래 결정 | `action`, `requested_quantity`, `reason`, `risk_control` |
| 커뮤니티 게시 | 미게시: `will_post`; 게시: `will_post`, `post_type`, `title`, `content` |
| 커뮤니티 선택 | `selected_post_ids` (string ID 배열) |
| 커뮤니티 반응 | `reactions` (`post_id` string, `reaction` enum) |
| 다음-AM 커뮤니티 해석 | `observed_sentiment`, `claims`, `agreement_disagreement`, `uncertainty` |

다음-AM `claims[]`의 정확한 내부 키는 `claim_text`, `claim_stance`,
`source_exposure_ids`, `supporting_quote`다. `supporting_quote`는 인용한
title-only 또는 full-body 노출에서 실제 허용된 문자열의 정확한 부분 문자열이어야
한다. 이 인용은 어떤 문자열이 실제로 보였는지를 기록하는 provenance이며
`claim_text`가 그 문자열에 의미적으로 포함되거나 함의되어야 한다는 제한이 아니다.
claim의 의미·신뢰도 판단은 agent에게 맡기며 server validator는 visible source
ownership, title-only/full-body 노출 수준, exact quote provenance, privacy만 검사한다.

기존 `belief_summary`, `view_change`는 LTB commit 뒤 서버가 만든 compatibility/human-log projection에만 존재한다. 이 중 결정론적 `view_change`만 같은 PM의 게시 여부 판단에 제한적으로 전달하며, 다음 거래의 분석·의사결정 입력에는 여섯 차원과 검증된 근거만 전달한다.

## 자동 검증

- production bundle 대상 텍스트 파일이 정확히 11개인지 검사한다. 과거 재현용 `update_belief.txt`는 bundle 대상에서 제외한다.
- 11개 전체 byte hash와 runtime-use 역할을 bundle v2 manifest에 자동 기록하고 run-local copy를 다시 검증한다.
- support prompt는 일반 `str.format`을 쓰지 않고 승인된 이름 슬롯만 source-only one-pass로 치환한다. 게시글 본문 속 `{...}`는 template token으로 재해석하지 않는다.
- Belief 프롬프트의 6개 동적 글자 제한 토큰은 누락·중복·오타가 있으면 로드 단계에서 실패한다.
- 실제 렌더링 시 봉인된 글자 제한이 치환되고, JSON payload의 중괄호는 그대로 보존되는지 검사한다.
- 모든 모델 출력은 정확한 키 집합, 타입, 글자 제한, 근거 ID를 통과해야만 기록된다.
