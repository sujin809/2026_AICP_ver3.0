# 공용 프롬프트 묶음

이 디렉터리는 0720 기준 `prompts/`의 10개 역할을 검토할 수 있게 옮긴 작업본이다. 기존 `update_belief.txt`만 역할을 분리해 `update_short_term_belief.txt`와 `update_long_term_belief.txt`가 되었으므로, 공용본은 총 **11개**다.

루트 `prompts/`는 아직 변경하지 않는다. 이 공용본을 확인·승인한 뒤에만 통합한다.

| 공용 파일 | 0720 기준 파일 | 현재 상태 |
|---|---|---|
| `community_reading.txt` | `../community_reading.txt` | 기존 골격을 유지하고 RN string post ID·공개 프로필 privacy 문장만 수정 |
| `community_thinking.txt` | `../community_thinking.txt` | 기존 해석 골격을 유지하고 title-only/full-body 경계와 exact supporting quote를 가진 next-AM claim JSON 계약으로 수정 |
| `initial_belief.txt` | `../initial_belief.txt` | 바이트 단위 동일 복사 |
| `news_agent_pre_search.txt` | `../news_agent_pre_search.txt` | 바이트 단위 동일 복사 |
| `news_agent_post_search.txt` | `../news_agent_post_search.txt` | 바이트 단위 동일 복사 |
| `news_interpretation.txt` | `../news_interpretation.txt` | 바이트 단위 동일 복사 |
| `posting_decision.txt` | `../posting_decision.txt` | 기존 골격과 게시/미게시 conditional JSON을 유지하되, 입력은 현재 post-fill LTB 6차원·결정론적 view change·실제 PM 체결로 제한하고 JSON 예시를 strict하게 수정 |
| `update_short_term_belief.txt` | `../update_belief.txt` | 기존 3단계 Belief 골격·6차원을 유지하고, current-only 뉴스·커뮤니티 입력과 근거 키만 최소 추가 |
| `update_long_term_belief.txt` | `../update_belief.txt` | 기존 updater 골격에서 장기 Belief 통합 역할만 분리한 새 파일 |
| `market_analysis.txt` | `../market_analysis.txt` | 기존 9개 분석 키를 유지하고 `directional_stance`, `evidence_references`만 추가 |
| `make_decision.txt` | `../make_decision.txt` | 기존 흐름을 유지하고 출력 수량 키만 `requested_quantity`으로 명확화 |

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
- 11개 파일은 core/support 구분과 무관하게 하나의 bundle v2 canonical hash와 run-local manifest로 자동 봉인한다. 사용자가 hash를 수동 계산하거나 입력하지 않는다.
- 실제 호출은 core 4단계와 조건부 community posting/select/reaction/next-AM interpretation이다. `initial_belief`는 deterministic LTB0가, 기존 news pre/post/interpretation helper는 sealed D2 registry와 current-only STB가 대체하므로 추가 LLM call을 만들지 않는다. 이 runtime-use 상태도 bundle manifest에 기록한다.

## 검토 방법

```text
diff -u prompts/community_reading.txt prompts/common/community_reading.txt
diff -u prompts/community_thinking.txt prompts/common/community_thinking.txt
diff -q prompts/initial_belief.txt prompts/common/initial_belief.txt
diff -q prompts/news_agent_pre_search.txt prompts/common/news_agent_pre_search.txt
diff -q prompts/news_agent_post_search.txt prompts/common/news_agent_post_search.txt
diff -q prompts/news_interpretation.txt prompts/common/news_interpretation.txt
diff -u prompts/posting_decision.txt prompts/common/posting_decision.txt
diff -u prompts/update_belief.txt prompts/common/update_short_term_belief.txt
diff -u prompts/update_belief.txt prompts/common/update_long_term_belief.txt
diff -u prompts/market_analysis.txt prompts/common/market_analysis.txt
diff -u prompts/make_decision.txt prompts/common/make_decision.txt
```
