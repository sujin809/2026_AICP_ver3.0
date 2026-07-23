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
