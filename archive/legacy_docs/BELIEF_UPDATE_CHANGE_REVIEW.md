# 기존 Belief 업데이트 프롬프트 변경 검토

> 역사 문서: 과거 단일 belief updater와 현재 STB/LTB updater를 비교한 검토
> 기록이다. 단일 updater와 prompt는 production에서 제거되었으며 Git history로만
> 보존한다.

비교 기준은 같은 디렉터리의 과거 재현용 `update_belief.txt`다. Production STB/LTB는 이 파일을 새 문체로 다시 쓴 것이 아니다. 원문 문단 순서와, 역할 경계와 충돌하지 않는 문장은 그대로 유지한다. 바꾼 줄은 아래의 입력 시점·출력 계약·역할 분리 때문에 불가피한 것만이다.

## 원문에서 그대로 유지한 골격과 문장

1. `당신은 아래 페르소나를 가진 한국 삼성전자 개인투자자입니다.` 도입문
2. `【Belief란 무엇이며 왜 업데이트하는가】`
3. `【오늘의 컨텍스트】`
4. Step 1: 새 정보 확인
5. Step 2: 기존 Belief와 비교
6. Step 3: 여섯 차원 정리
7. 기존 `dim_1`~`dim_6`의 한국어 정의와 `{dim_1_limit}`~`{dim_6_limit}` 표기
8. 거래 행동 지시를 쓰지 않는다는 마무리 원칙

`tests/test_rn_ab_prompt_registry.py`는 위의 영향을 받지 않는 원문 줄이 두 파일에서 같은 순서로 남아 있는지 검증한다. 따라서 역할 경계와 무관한 문장 재작성은 테스트에서 실패한다.

## 두 파일에 공통으로 바꿔야 하는 부분

- 원본의 `{persona_prompt}`와 `{today_context}` 이름 슬롯은 남길 수 없다. RN belief prompt는 일반 `str.format()`을 쓰지 않고, 실행 시 한 번만 삽입되는 봉인 JSON payload를 사용한다. 대신 원문의 페르소나 도입문은 유지하고, `persona`와 컨텍스트가 아래 입력 JSON에 있다는 사실만 명시한다.
- 기존 `belief_summary`, `view_change`는 모델 출력 키에서 제거한다. 두 값은 자유문장 모델 출력이면 다음 단계의 숨은 상태가 되므로, LTB commit 뒤 서버가 이전/새 6차원과 검증된 integration evidence에서 결정론적으로 만든 사람용 log projection으로만 남긴다.
- 각 단계에는 기존 여섯 차원 외에 근거 배열 한 개만 추가한다. exact JSON object, 추가 키 금지, Markdown 금지, 차원별 `support`/`contradict` 배열은 파싱·재시작 안전성을 위한 출력 계약이다.

## Short-Term Belief에서만 바꾼 부분

- 현재 뉴스와 커뮤니티 주장만 현재 증거로 받는다.
- 이는 **그날 뉴스·커뮤니티에 대한 임시·현재 관점**이며, 이전 Long-Term Belief를 대체하거나 수정하지 않는다.
- Depth 2에만 제공되는 최근 검색 결과도 현재 증거로 명시한다. 제공되지 않은 가격·기술지표·portfolio·체결·과거 성과는 만들지 않는다.
- 이전 STB/LTB, 시장 가격·포트폴리오·과거 체결 결과는 입력으로 받지 않으며 추정하지 않는다.
- 여섯 차원에 대해 `dimension_evidence`를 추가해, 각 support/contradict 근거 ID를 명시한다.
- 모델 출력은 여섯 차원과 `dimension_evidence`뿐이다. 기존 `belief_summary`, `view_change` 호환 필드는 LTB가 서버 검증·commit된 뒤 결정론적으로 만든 사람용 log projection에만 남긴다.

## Long-Term Belief에서만 바꾼 부분

- 이전 Long-Term Belief와 오늘의 Short-Term Belief를 다음 거래용 관점으로 통합한다.
- 이는 **이전 장기 관점 + 오늘의 임시 STB + 실제 체결 경험**을 통합한 지속적 관점이며, 같은 turn의 거래에는 다시 보이지 않고 다음 거래부터 사용한다.
- 원문의 “오늘의 거래에 앞서”와 “오늘 거래에 임할”은 post-fill 시점과 모순되므로, 같은 골격을 유지한 “다음 거래에 앞서”와 “다음 거래에 임할”로만 바꾼다.
- 이번 체결은 경험 맥락으로, 관찰 시점이 도래한 과거 가격 결과는 dim_6의 검증 근거로만 사용한다.
- `sanitized_evidence_registry`에 있는 검증된 ID만 인용한다. raw 뉴스/커뮤니티 본문이나 미래 결과를 되살리거나 새 ID를 만들 수 없다.
- 여섯 차원에 대해 `integration_evidence`를 추가한다.
- 모델 출력은 여섯 차원과 `integration_evidence`뿐이다. 서버가 확정된 이전/새 LTB와 integration evidence에서 기존 JSON 호환용 `belief_summary`, `view_change`를 결정론적으로 만든다.

## 파싱 안전성

- 두 프롬프트는 여섯 개의 `{dim_n_limit}` 토큰을 각각 한 번씩만 가져야 한다.
- 실행기는 `str.format()`을 쓰지 않고, Belief 프롬프트에서는 정확히 그 여섯 토큰만 봉인된 정수로 치환한다. 기존 시장 분석·결정 프롬프트의 이름 있는 삽입 슬롯도 일반 포맷팅 없이 단계별 고정 allowlist로만 봉인된 payload에서 채운다.
- 키 누락·추가, 빈 문자열, 비JSON 출력, 차원별 글자 제한 초과, 근거 ID 위조는 모두 실패 처리한다.
- 모델이 summary/change key를 보내면 추가 key로 거부한다. response journal에는 scientific 여섯 차원과 evidence만 남기며, human log는 별도 결정론 renderer hash로 감사한다.
