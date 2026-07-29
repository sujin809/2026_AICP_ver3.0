# RN source-input candidates (local NO-GO audit)

`input_candidates/` is a deterministic inventory of the current local source
files. It is deliberately not a run input directory and cannot substitute for
a sealed StudySpec or RunBundle.

This directory is now retained as a historical archive of that source audit.
The runtime real-news source of truth is
`preparation/rn_ab_sealed_v1/news.json`, pinned by
`preparation/rn_ab_sealed_v1/study_spec.json`. Do not refresh this archived
audit's hashes merely because current source files have changed.

The commands below are preserved to document how the historical audit was
created. `validate` intentionally compares its recorded source hashes with the
current source files and may now fail after an approved source refresh. Do not
rewrite the archived audit to make that check pass.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3.12 \
  scripts/11_build_rn_ab_input_candidates.py build

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3.12 \
  scripts/11_build_rn_ab_input_candidates.py validate
```

The build reads and hashes the declared stock, macro, processed/daily/raw news,
target, fake-injection, fixed-slot, source-persona, and repaired persona
snapshot files. It checks that their bytes did not change during the build.
It makes non-runtime candidates for:

- the documented 2026-02-27–2026-05-04 calendar and AM/PM event shell;
- the frozen 100-person cohort and D0/D1/D2 map;
- historical open/close price and stage-input projections;
- the evaluator-only `Individuals` target projection;
- the known bearish/bullish fake-export closure; and
- legacy real-news inventory and its leakage quarantine ledger.

The output has three intentional hard stops:

1. It never invents `observed_at`, `last_modified_at`, a cutoff-version hash,
   or an article snapshot from the legacy news files.
2. `news_20260427_섹터_0032` is always recorded as `decision: quarantine`.
   Semantic matches are also quarantined pending an independent immutable
   article-version review; no automatic `allow` row is produced.
3. Every candidate and the top-level audit state
   `execution_authorized: false` and `run_eligible: false`. The command makes
   zero network requests and zero paid API calls.

`SOURCE_INPUT_CANDIDATE_AUDIT.json` binds source byte hashes to the eight
candidate files. `SOURCE_INPUT_CANDIDATE_SHA256SUMS.txt` verifies their bytes.
If any declared source changes after creation, `validate` fails rather than
silently reusing the candidate.

The candidate inventory may reveal source-count or event-coverage differences
from older design notes. Those are findings, not permission to take an
intersection, fill a missing slot, or manufacture an approved clean bundle.
