# Source and chart notes

## Primary source scope

- Source checkout: `/private/tmp/aicp_off_result_20260721`
- Branch / pushed commit: `off_result_20260721` / `56057320474a59c3c60e0554d7a066110193b331`
- Code recorded at run start: `8604f9aec041c9929e327a90cc9025b650e9fab6`
- Run directory: `outputs/logs/paper_0721/c00_commoff_fakeoff`
- Validation directory: `validation/outputs/c00_commoff_fakeoff_2026-05-04`
- Sealed time coverage: 2026-02-27 through 2026-05-04, 45 trading days
- Grain: 30 agents × 45 dates × AM/PM = 2,700 agent-turns; aggregate validation grain is trading date
- Excluded from analysis: the partial 2026-05-06 chunk outside `run_complete.json`'s sealed scope

## Metric definitions

- Direction match: share of dates where the sign of aggregate simulated signed value flow equals the sign of actual individual-investor net value flow.
- Buy recall: share of actual individual net-buy dates predicted as net buy.
- Sell recall: share of actual individual net-sell dates predicted as net sell.
- Balanced accuracy: arithmetic mean of buy recall and sell recall.
- Pearson/Spearman: daily association between simulated and actual signed value flows. These are descriptive and serial dependence is not removed.
- Order intensity: selected quantity divided by the feasible maximum for the selected direction.
- Disposition gap: per-agent probability of sell on unrealized-gain turns minus probability of sell on unrealized-loss turns, restricted to turns where both directions are feasible.

## Chart contract: C00 versus simple baselines

- Source: recomputed C00 fills plus repository validation baselines.
- Primary grain: trading date.
- Time coverage: 45 sealed trading days.
- Units: rates from 0 to 1.
- Comparison question: does C00 improve direction metrics beyond mechanically simple rules?
- Baselines: always buy; previous-day market-return direction.
- Uncertainty: C00 direction-match Wilson 95% interval is 0.455–0.730. Baseline deltas are single-run descriptives, not confirmatory estimates.
- Identifiers: `c00_commoff_fakeoff`, branch `off_result_20260721`.

## Chart contract: phase and burn-in sensitivity

- Source: fills aggregated separately for AM, PM, and AM+PM and joined to actual daily individual flow.
- Primary grain: trading date.
- Time coverage: 45 days before exclusion and 40 days after excluding the first five.
- Units: balanced accuracy rate.
- Comparison question: is the apparent signal stable to information timing and initial capital deployment?
- Baseline: 0.5 balanced accuracy reference.
- Uncertainty: sensitivity is diagnostic; conditions share dates and are not independent replications.
- Identifiers: phase names `AM only`, `PM only`, `AM+PM`.

## Chart contract: decision-time price-only baselines

- Source: unique per-date AM/PM `context.market_features` values from `agent_turns.jsonl`, aggregate phase-specific C00 fills, and actual daily individual flow.
- Primary grain: trading date.
- Time coverage: 45 sealed days; a 40-day skip-five sensitivity is retained in the companion table.
- Units: direction-match and balanced-accuracy rates.
- Comparison question: does the agent pipeline add signal beyond price movements already visible in its prompt?
- AM rule: predict individual net buy when the opening gap versus previous close is negative, and net sell when positive.
- PM rule: predict individual net buy when current-day close return is negative, and net sell when positive.
- Baseline context: the PM rule is contemporaneous and supports only a reconstruction comparison; the AM opening-gap rule is the cleaner prospective comparator.
- Uncertainty: paired exact p-values are diagnostic because dates are serially ordered and the window was not independently replicated.
- Identifiers: `AM opening-gap contrarian`, `PM current-return contrarian`.

## Chart contract: correlation sensitivity

- Source: daily simulated and actual signed value flow.
- Primary grain: trading date.
- Time coverage: skip 0, 1, 3, 5, or 10 initial dates.
- Units: Pearson and Spearman coefficients.
- Comparison question: is the reported size association robust to the synchronized initial deployment?
- Baseline: zero correlation.
- Uncertainty: naive correlation p-values are not used as confirmatory evidence because observations are time ordered.
- Identifiers: AM+PM only.

## Portable HTML build status

- Artifact manifest generation succeeded: `analysis/paper_0721_c00_review/artifact.json`.
- The canonical `report:deliver` builder completed data packaging and static-chart rendering.
- Final verification failed at the 1440px desktop viewport with `horizontal_overflow`.
- The failure screenshot shows report content contained within the reading column but a page-wide horizontal scrollbar. The bundled builder CSS sets the sticky `.portable-page-header` to `width:100vw` while a vertical scrollbar is present, reproducing the same scrollbar-width overflow seen in the earlier report attempt.
- The plugin/runtime was not patched. `LATEST_C00_REVIEW.md` and the fully executed notebook are the verified deliverables; `artifact.json` remains ready to rebuild after the runtime CSS issue is fixed.
