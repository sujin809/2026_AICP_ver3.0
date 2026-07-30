> **ARCHIVED — 이 문서는 현행 정책이 아니다.**
>
> 2026-07-30에 `preparation/`에서 `archive/legacy_docs/`로 격리했다. 근거:
>
> - 여기서 규정하는 `prepare_preflight_bundle()`,
>   `public_author_profile_registry.json`, `community_post_truth_policy.json`은
>   제거된 RN 전용 runtime(`twinmarket_kr.rn_ab`)의 산출물이며 활성 Python
>   코드 참조가 0이다.
> - 아래 "Public author profile" 절은 균일 상수 `public_badges`와
>   `public_reliability_score`를 규정하고 portfolio·최근 거래 노출을
>   금지하지만, 현행 baseline은 badge를 아예 두지 않고(§12.9) D2에게는
>   후보 보드 시점에 동결한 portfolio 요약·최근 체결 3건을 제공한다.
>
> 현행 community 노출 정책의 정본은 `ARCHITECTURE.md` §12와
> `EXPERIMENT_DESIGN.md` §8이다. 이 파일은 과거 설계 provenance로만 읽는다.

# RN generated preflight input contract

Contract version: `rn-generated-preflight-inputs-v1`

`prepare_preflight_bundle()` creates `inputs/generated/` after the StudySpec,
cohort, persona snapshot, calendar, stage inputs, and clean real-news bundle
have all passed their existing validators. The generated directory contains:

| File | Version | Deterministic source |
|---|---|---|
| `depth2_recent_search_registry.json` | `rn-depth2-recent-search-v1` | reviewed clean-news bundle + each event's sealed cutoff |
| `public_author_profile_registry.json` | `rn-public-author-profile-registry-v1` | frozen D1/D2 cohort membership + uniform neutral public-only policy |
| `community_post_truth_policy.json` | `rn-community-post-truth-policy-v1` | compatibility-named public-claim/ledger-boundary and privacy contract |
| `generated_input_manifest.json` | `rn-generated-preflight-inputs-v1` | resolved-study and artifact hashes |

## Depth-2 search projection

For each approved decision event, the generator:

1. starts from articles already present in the clean, leakage-reviewed
   real-news bundle;
2. excludes every article assigned to the current event;
3. rejects articles published before the seven-calendar-day window, published
   after the event cutoff, or observed after the event cutoff;
4. orders eligible articles by publication timestamp descending and then
   article ID ascending; and
5. keeps at most ten article IDs with their sealed payload hashes.

An empty first-event result is a valid deterministic search result, not an
invented article or an unavailable-data placeholder.

## Public author profile

Every D1/D2 posting-capable cohort member receives the same deliberately
uninformative public projection:

```json
{
  "schema_version": "rn-public-profile-v1",
  "public_badges": ["registered-community-member"],
  "public_direction": "neutral",
  "public_reliability_score": 50
}
```

It is a study policy value, not an estimate of actual investment skill.
Agent ID is only the registry join key and is removed from reader-facing
projections by the community service. Portfolio, cash, holdings, recent
trades, fills, private belief, free-form action reasons, and stable author
identity are prohibited.

## Public-claim and ledger boundary

Community post content is not required to match private state and never
overrides the run-local fill ledger. The server does not judge the truth,
reliability, or semantic entailment of a post or interpretation claim. It
checks only visible-source ownership, exact supporting-quote provenance, and
privacy boundaries. The physical filename and schema retain `truth_policy`
for compatibility; they do not denote a semantic truth filter. The exact
prohibited field list and public-profile allowlist are hash-pinned in the
generated policy.

## Provenance and authorization

All JSON files use exact schemas, reject duplicate/non-finite JSON values, and
carry canonical content hashes. `RNRunContext.load()` regenerates the expected
values from its run-local sealed inputs and rejects any drift.

The manifest explicitly records:

- `human_approval_claimed=false`
- `network_requests=0`
- `paid_api_calls=0`
- `execution_authorized=false`

These fields describe preparation provenance. They do not introduce a manual
approval gate, and they do not bypass the separate live reasoning-off canary
or journal-aware provider requirements.
