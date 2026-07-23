# API Model Usage Notice

## 핵심 원칙

일반·legacy API 호출은 `.env`의 `OPENROUTER_MODEL`을 사용할 수 있다. 그러나 **RN Community AB paper path는 `.env`를 모델 선택 권한으로 쓰지 않는다.** 허용 모델은 import-safe 코드 상수 `twinmarket_kr.rn_model_pin.RN_PAPER_MODEL`의 `qwen/qwen3.5-flash-02-23` 하나이며, `twinmarket_kr.rn_ab.call_policy.RN_PAPER_MODEL`은 그 값을 재노출한다. sealed `StudySpec.model_policy.model`도 이 값과 정확히 같아야 한다. `config.PAPER_REASONING_DISABLED_MODEL`은 호환 alias일 뿐이다.

따라서 `.env`에서 `OPENROUTER_MODEL`을 바꿔도 RN paper model은 바뀌지 않는다. 이 실험에서는 해당 모델의 reasoning 기능이 **명시적으로 작동하지 않도록** 요청 전에 강제·검증한다. OpenRouter에서 실제 reasoning-off 스위치는 아래의 `effort: "none"`이며, 응답에서 reasoning을 숨기는 것만으로는 허용되지 않는다.

```json
{
  "model": "qwen/qwen3.5-flash-02-23",
  "temperature": 0.2,
  "response_format": {
    "type": "json_object"
  },
  "reasoning": {
    "effort": "none",
    "exclude": true
  }
}
```

여기서 `reasoning.effort = "none"`이 reasoning 생성 자체를 끄는 값이다. `exclude = true`만 있으면 이미 생성된 reasoning을 응답에서 감출 뿐이므로 단독 사용은 금지한다.

## 주의사항

- RN paper path에서는 `RN_PAPER_MODEL` 외 모델을 선택하거나 대체하지 않는다. `.env` 값·기본값·fallback으로도 우회할 수 없다.
- 실행 속도가 느리더라도 더 빠른 모델, 더 저렴한 모델, 기본 모델 등으로 자동 변경하지 않는다.
- RN model 문자열은 `RN_PAPER_MODEL` 한 곳에서만 관리한다. sealed StudySpec과 strict client는 그 값과의 정확한 일치를 검증한다.
- RN 모델 변경은 `.env` 수정이 아니라 code pin·StudySpec·prompt/runtime provenance를 함께 새로 봉인하고, 양 arm을 새 run으로 실행하는 amendment다.
- fallback 로직을 추가하더라도 다른 모델로 전환하는 방식은 사용하지 않는다.
- API 실패, 속도 저하, rate limit 문제가 발생해도 허가 없이 모델을 바꾸지 않는다.
- 현재 RN 모델에서 reasoning effort를 `none` 이외로 설정하거나 reasoning 설정을 제거한 요청은 전송 전에 실패해야 한다.

## 운영 기준

RN 시뮬레이션의 뉴스 해석·belief·판단 API 요청은 sealed `StudySpec`과 code pin을 단일 기준으로 사용한다. `.env`는 API key와 legacy/exploratory 기본값에만 사용한다.

성능이나 속도보다 실험 조건의 일관성이 우선이다. 따라서 실행 시간이 길어지더라도 요청된 API 모델만 사용해야 한다.

## RN reasoning-off 코드 강제

- `twinmarket_kr/llm/client.py`의 `_enforce_paper_reasoning_off()`가 일반 `OpenRouterClient.chat()`에도 적용된다. 현재 Qwen paper model이면 호출자가 reasoning field를 생략해도, `extra_body`를 만들기 직전에 반드시 `"reasoning": {"effort": "none", "exclude": true}`를 삽입한다. 다른 effort 또는 hide-only 설정을 넘기면 HTTP 요청 전 `ReasoningPolicyError`로 거부한다.
- `twinmarket_kr/rn_ab/call_policy.py`의 `OPENROUTER_REASONING_OFF = {"effort": "none", "exclude": True}`는 RN strict path의 같은 설정을 단일 상수로 둔 것이다. `StrictCallPolicy.as_request_policy()`는 이를 final request body의 `reasoning` field로 넣는다.
- `twinmarket_kr/llm/client.py`의 `OpenRouterClient.chat_strict_reasoning_off()`는 policy model이 `config.PAPER_REASONING_DISABLED_MODEL`(= `RN_PAPER_MODEL`)과 정확히 일치하지 않으면 HTTP 요청을 만들기 전에 `UnexpectedModelError`를 낸다.
- 같은 메서드는 final request body를 `StrictCallPolicy.as_request_policy()`로만 만들고, `reasoning: {"effort": "none", "exclude": true}`, 단일 provider, fallback 금지, parameter 필수와 정확히 같지 않으면 요청 전에 실패한다. 호출자가 reasoning/provider/fallback 값을 전달할 인자는 없다.
- RN strict 호출은 위 예시의 `temperature: 0.2`와 `response_format: {"type": "json_object"}`도 정확히 요구한다. 생략·변경하면 HTTP 요청 전에 실패하며, `finish_reason != "stop"`인 응답도 JSON처럼 보여도 commit하지 않는다.
- `twinmarket_kr/rn_ab/stage_adapter.py`의 현재 `rn-trusted-system-v2` `RN_TRUSTED_SYSTEM_INSTRUCTION`은 모든 RN strict 호출에 별도 `system` role로 봉인되어 전달된다. news·community 원문은 `user` payload 안의 tagged source-data로 system/task instruction과 분리된다. exact source 복사는 `supporting_quote` 같은 quote field에만 요구한다. 그 밖의 interpretation field는 agent의 판단이며 literal source text나 server truth verdict를 요구하지 않는다. Server는 visible source ownership·exact quote provenance·privacy만 검사한다.
- `twinmarket_kr.rn_ab.execution.build_live_stage_models()`는 각 arm의 물리 요청을 전역 `openrouter_calls.jsonl`이 아니라 sealed run 아래 `RN_COMM_OFF/openrouter_attempts.jsonl`, `RN_COMM_ON/openrouter_attempts.jsonl`에 기록한다. 각 행에는 `run_id`, `condition_id`, resolved manifest SHA-256이 포함된 audit context가 있어 다른 run/arm 로그를 섞어 증명할 수 없다.
- OpenRouter가 응답을 반환한 사실과 실험이 그 응답을 채택한 사실은 같은 상태가 아니다. 각 HTTP 결과는 먼저 `audit_event="provider_attempt"`, `status="provider_returned"`로 기록되며, 이 단계에서는 `accepted_response_sha256`가 반드시 `null`이다. exact JSON 파싱·stage schema 검증·durable response journal 저장이 모두 성공한 뒤에만 별도 `audit_event="experiment_acceptance"`, `status="accepted"` 행을 append한다.
- provider-attempt에는 malformed JSON도 잃지 않도록 provider가 반환한 message content의 UTF-8 SHA-256를 항상 남기며, duplicate key·NaN·non-object·깨진 JSON이면 canonical JSON SHA-256는 `null`로 둔다. acceptance 행은 strict JSON object일 때만 존재할 수 있고, canonical accepted-response SHA-256과 원래 provider-attempt 행 전체의 SHA-256을 함께 기록한다. 최종 handoff는 이 accepted-response SHA-256이 committed journal digest와 logical-call ID별로 정확히 일치해야만 통과한다. 따라서 schema가 깨진 provider 응답 뒤 같은 logical-call ID를 재시도해 성공해도, 앞 응답은 provider-returned 실패 이력으로만 남고 중복 성공으로 계산되지 않는다.
- core STB/analysis/decision/LTB뿐 아니라 같은 strict model adapter를 사용하는 community posting/read/reaction/interpretation 호출도 동일한 acceptance 결합을 거친다. 재시작 시 journal에는 accepted response가 있으나 acceptance 행 flush 전에 프로세스가 종료된 경우에는, run-local provider 행의 동일 response SHA-256을 찾아 acceptance 결합만 복구하며 새 API 요청을 만들지 않는다.
- `twinmarket_kr.rn_ab.execution.validate_run_local_reasoning_audits()`는 두 arm의 run-local audit만 읽고, 요청·반환 모델, provider, final request body, 빈 reasoning field, `reasoning_tokens=0`을 모두 fail-closed로 검증한다. 전역 legacy audit log로 대체할 수 없다.
- offline stub도 이 경로에서는 거부한다. experiment-accepted 응답에는 reasoning token 0, 빈 reasoning field, `finish_reason="stop"`, 위 temperature/JSON response-format 및 exact response digest 결합이 audit에 남아야 하며, 누락이나 불일치는 NO-GO다. 현재는 유료 live canary를 실행하지 않았으므로 audit path/validator가 구현됐더라도 실험 승인 증거는 아직 없다.
