# Production 프롬프트 묶음

> 역사 문서: 단일 `update_belief.txt`에서 STB/LTB 전용 prompt로 이행할 때의
> 검토 기록이다. 현재 실행 정본은 최상위 `prompts/`의 STB/LTB 전용 파일이며,
> 삭제된 과거 prompt는 Git history에서만 확인한다.

최상위 `prompts/`가 production prompt의 유일한 편집 정본이다. 0720 기준 10개 역할 중 기존 `update_belief.txt`의 역할만 `update_short_term_belief.txt`와 `update_long_term_belief.txt`로 분리했으므로, 현재 production bundle은 총 **11개**다.

`update_belief.txt`는 과거 실행 재현용 compatibility 파일이며 production bundle hash에는 들어가지 않는다. 과거 run의 `preparation/**/prompts/` 복사본은 재현 artifact이므로 수정하지 않는다.

| Production 파일 | 0720 기준 파일 | 현재 상태 |
|---|---|---|
| `community_reading.txt` | 같은 파일의 0720 버전 | 기존 골격을 유지하고 RN string post ID·공개 프로필 privacy 문장만 수정 |
| `community_thinking.txt` | 같은 파일의 0720 버전 | 기존 해석 골격을 유지하고 title-only/full-body 경계와 exact supporting quote를 가진 next-AM claim JSON 계약으로 수정 |
| `initial_belief.txt` | 같은 파일의 0720 버전 | 문구 유지 |
| `news_agent_pre_search.txt` | 같은 파일의 0720 버전 | 2026-07-28 확정된 D2 설계 변경에 따라 검색 키워드 상한만 `3~8개` → `0~5개`로 수정. 0개는 "이번 턴에는 검색하지 않는다"는 유효한 판단이며, 값은 `news_agent.DEPTH2_MAX_KEYWORDS`와 공유한다 |
| `news_agent_post_search.txt` | 같은 파일의 0720 버전 | 문구 유지 |
| `news_interpretation.txt` | 같은 파일의 0720 버전 | 문구 유지 |
| `posting_decision.txt` | 같은 파일의 0720 버전 | 기존 골격과 게시/미게시 conditional JSON을 유지하되, 입력은 현재 post-fill LTB 6차원·결정론적 view change·실제 PM 체결로 제한하고 JSON 예시를 strict하게 수정 |
| `update_short_term_belief.txt` | `update_belief.txt` | 기존 3단계 Belief 골격·6차원을 유지하고, current-only 뉴스·커뮤니티 입력과 근거 키만 최소 추가 |
| `update_long_term_belief.txt` | `update_belief.txt` | 기존 updater 골격에서 장기 Belief 통합 역할만 분리한 새 파일 |
| `market_analysis.txt` | 같은 파일의 0720 버전 | 기존 9개 분석 키를 유지하고 `directional_stance`, `evidence_references`만 추가 |
| `make_decision.txt` | 같은 파일의 0720 버전 | 기존 흐름을 유지하고 출력 수량 키만 `requested_quantity`으로 명확화 |

## 공통 원칙

- 뉴스가 실제인지 가짜인지, community ON/OFF인지 같은 조건은 프롬프트에 쓰지 않는다. 차이는 봉인된 입력 뉴스·커뮤니티 묶음과 실험 설정에서만 생긴다.
- 새 프롬프트 문체나 새로운 투자 판단 단계를 만들지 않는다. 기존 텍스트를 기준으로, 실제 입력·출력 계약상 불가피한 문장만 바꾼다.
- STB/LTB의 기존 `{dim_1_limit}`~`{dim_6_limit}` 표기는 그대로 둔다. 실행 시 이 여섯 리터럴 토큰만 봉인된 숫자로 치환하며, 일반 문자열 포맷팅은 사용하지 않는다.
- 기존 시장 분석·결정 프롬프트의 `{persona_prompt}` 등 이름 있는 슬롯은 원문을 보존한다. 실행기는 단계별 고정 allowlist의 슬롯만 봉인된 payload로 치환하며, JSON 중괄호 전체에 일반 포맷팅을 적용하지 않는다.
- JSON 응답은 정확한 키 집합 하나만 허용한다. Markdown·코드블록·추가 키는 허용하지 않는다.
- 커뮤니티 글과 해석의 의미·신뢰도 판단은 agent에게 맡긴다. validator는 실제
  visible source ownership, title-only/full-body 노출 수준, exact quote provenance,
  privacy만 검사하며 `claim_text`와 인용문의 의미적 함의나 진실성을 판정하지 않는다.
- 기존 `belief_summary`는 모델 prompt/response에서 제외한다. `view_change`는 LTB commit 뒤 서버가 결정론적으로 만든 projection만 게시 여부 판단 입력으로 허용하며, 모델 출력이나 다음 거래의 분석·결정 입력으로는 허용하지 않는다.
- `news_agent_pre_search`는 0720 원문 계승 대상에서 빠졌다. RN이 D2 검색을 서버
  결정론적 registry로 대체하던 설계가 2026-07-28에 agent 키워드 검색으로 바뀌면서
  이 프롬프트가 실제 실행 역할이 되었기 때문이다. 상한 값은 코드 상수와 한 곳에서
  관리하며, 프롬프트 문구만 바꾸고 코드 상수를 두면 조용히 어긋난다.
- 11개 파일은 core/support 구분과 무관하게 하나의 bundle v2 canonical hash와 run-local manifest로 자동 봉인한다. `RNPromptBundle.load_production()`과 봉인 스크립트는 모두 이 최상위 디렉터리를 읽으며, 사용자가 hash를 수동 계산하거나 입력하지 않는다.
- 실제 호출은 core 4단계와 조건부 community posting/select/reaction/next-AM interpretation이다. `initial_belief`는 deterministic LTB0가, 기존 news pre/post/interpretation helper는 sealed D2 registry와 current-only STB가 대체하므로 추가 LLM call을 만들지 않는다. 이 runtime-use 상태도 bundle manifest에 기록한다.

## 검토 방법

`tests/test_rn_ab_prompt_registry.py`가 최상위 11개 production 파일의 전체 집합,
STB/LTB 역할 경계, exact 출력 schema, bundle hash와 run-local sealing을
검증한다. 같은 이름의 별도 편집본을 두지 않는다.
