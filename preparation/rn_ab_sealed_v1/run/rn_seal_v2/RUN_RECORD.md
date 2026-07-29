# RN Community AB Run Record

이 파일은 수동 일지가 아니라 `RUN_RECORD.json`에서 렌더한 preflight artifact index입니다.

## 상태

- 실행 상태: **PRE-FLIGHT COMPLETE / PAID EXECUTION NOT AUTHORIZED**
- integrity 상태: sealed input·baseline HEAD·초기 DB base 검증 통과
- network requests: 0
- paid API calls: 0
- validator/report: `not_generated_due_to_preflight_only_no_execution`

## 고정 식별자

- run_id: `rn_seal_v2`
- baseline commit / checked-out HEAD: `76737603bda78abe20966f660c1cea6e7e38c904` / `76737603bda78abe20966f660c1cea6e7e38c904`
- resolved manifest SHA-256: `0919f0c2782f16ebd23aba4f9b20766f037bd84314100a9108f0c8c4b0b773ec`
- source snapshot: [`source_hashes.json`](source_hashes.json) (`4472a9ae8cb67dffdfab96a588bc3674f6b36dc01d602e56530d38f1eb434d77`)
- source tree SHA-256: `5aecb3fea1cfb487fc9f1eaebd082735d06477903addee2bc4f9dfdf2060c90d`
- dependency-lock tree SHA-256: `b7044d3f9a2336019282fb524729fc64bace73fb5735c0b2125f4d622e45879f`
- prompt bundle: [`inputs/prompts`](inputs/prompts) (`c84982aad964fb2b17153b41a2b7cf8866bc4923c51148f7b450b6758ff50085`)
- persona snapshot: [`inputs/persona`](inputs/persona) (`7de05577a86536d72a565e9df63575e0ec5911eb81c311438e7b4b431adc4a8f`)

## 실행 정책

- 모델/공급자/reasoning-off policy는 sealed `study_spec.json`과 resolved manifest에서만 읽습니다.
- RN strict request는 `reasoning: {"effort": "none", "exclude": true}`를 요청 직전에 강제합니다.
- 수수료/매도세/fee amount는 RN trade policy의 0원 baseline으로 봉인됩니다.
- live reasoning-off canary, D2 search registry, 승인된 community prompt/journal adapter가 아직 없으므로 이 record는 GO 승인이 아닙니다.

## Arm base artifacts

| Condition | Community | DB | Response journal | Initial portfolios | LTB₀ | Base digest |
|---|---|---|---|---:|---:|---|
| RN_COMM_OFF | off | [RN_COMM_OFF/paper_run.sqlite](RN_COMM_OFF/paper_run.sqlite) | [RN_COMM_OFF/response_journal.sqlite](RN_COMM_OFF/response_journal.sqlite) | 100 | 100 | `5bd869128b0f60bd771ca300319c02d9e62e58f9169b583a8678381779aaf04e` |
| RN_COMM_ON | on | [RN_COMM_ON/paper_run.sqlite](RN_COMM_ON/paper_run.sqlite) | [RN_COMM_ON/response_journal.sqlite](RN_COMM_ON/response_journal.sqlite) | 100 | 100 | `7ec824fd6031b6894510f4b9af6d9d010ff2db1c62b08a7d7116c769e3d7f495` |

## Frozen inputs

- real-news bundle SHA-256: `a6fb61900c27071b2a79781478592d99d914482fbba0f4ecaafa73edcb8ab707`
- article-version leakage review: [`article_version_leakage_review_manifest.json`](article_version_leakage_review_manifest.json) (`e67119227cb05591fa2e0a71ab9061a7dc18469d72436e36f574fa3e805c3620`)
- known-injection closure SHA-256: `9fe9a772368c0b9e841906f6967af5c0b0e1a9ee1ab98c059ce6a1f35a175c90` (RN baseline fake count is 0)
- runtime input byte hashes are in `RUN_RECORD.json > runtime_inputs`; execution reopens only those copies under `inputs/runtime/`.

## 다음 단계

이 preflight는 유료 실행을 시작하지 않았습니다. live canary 및 모든 P0 gate가 승인된 뒤에도 새 실행 factory가 이 sealed source/input state와 정확히 일치하는지 다시 확인해야 합니다.
