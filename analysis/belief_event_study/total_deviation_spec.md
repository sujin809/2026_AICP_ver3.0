# Belief deviation total-index specification

## 1. Purpose

The paper should report both interpretable subdimensions and one compact total-deviation measure. The total is not intended to replace the subdimensions. It is a presentation and ranking device for comparing events, personas, phases, and experimental conditions.

Two index versions are kept separate:

- **PODI (Provisional Observed Deviation Index)**: an exploratory index computed from fields already present in the completed community-on plus bullish-fake run.
- **BMDI (Behavioral Misinformation Deviation Index)**: the planned paper index, constructed after blind rubric annotation and validation.

PODI must not be described as a validated psychological scale or as a causal treatment effect.

## 2. Provisional index used in the pilot analysis

PODI is a 0–100 equal-weight average of four components, each bounded to 0–1.

| Component | Operational definition | Interpretation |
|---|---|---|
| Semantic displacement | Upper-tail empirical percentile of consecutive-belief embedding distance relative to non-injection turns | The belief text changed unusually strongly |
| Claim-direction movement | Upper-tail empirical percentile of the change in relative similarity to the paired bullish rather than bearish claim | The belief moved toward the injected bullish claim |
| Confidence escalation | Positive change in structured news and market confidence, using ordered anchors only | Expressed confidence increased |
| Belief-action translation | One when a positive bullish claim shift coincides with a buy action, otherwise zero | The directional text shift translated into the constrained action |

The primary score is:

`PODI = 100 × mean(semantic, claim direction, confidence escalation, action translation)`

Only observations with all four primary components are included. The analysis also reports:

- a targeted-heavy scheme: 10%, 40%, 25%, 25%;
- a text-only scheme: 50%, 50%, 0%, 0%;
- every component separately.

Important: PODI is an **incremental turn-level deviation score**. A low score in the turn after exposure means little additional movement occurred in that next turn; it does not prove that the prior belief change was reversed. Persistence is measured separately with the claim-axis trajectory.

## 3. Planned paper index after annotation

BMDI should use blind 0–4 coding of the actual belief, decision reason, risk-control text, and community text. The current rubric is defined in `belief_deviation_rubric.md`.

The recommended paper structure has three 0–100 subindices:

1. **Reception deviation**: injected-claim reception, directional stance, unsupported claim repetition, and evidence grounding.
2. **Epistemic amplification**: epistemic confidence, source confidence, and unjustified certainty escalation.
3. **Behavioral translation**: position conviction, belief-action consistency, and active trading response.

The primary total is the equal-weight mean of the three subindices. Alternative weighting should be reported as sensitivity analysis, not selected after observing results.

## 4. Validation requirements

- Conditions and exposure labels are hidden from annotators.
- At least two human annotators code a stratified sample spanning conditions, events, regimes, and personas.
- Ordinal dimensions report weighted Cohen's kappa; continuous aggregated subindices report ICC.
- An LLM judge can scale annotation only after agreement with human coding is demonstrated on the held-out audit sample.
- The final paper reports total BMDI and all subindices, including null or contradictory components.
- Statistical inference is clustered or hierarchical at agent and event/run level. Agent-turn rows are not treated as independent experimental replications.

## 5. Action-space caveat

The current buy/sell-only environment mechanically forces a direction. One-share trades may behave like a no-trade proxy, but quantity is also affected by cash and holdings constraints. Therefore:

- position conviction must be coded primarily from text, not quantity alone;
- active versus proxy-no-trade behavior must be separated in sensitivity analysis;
- the rerun should preferably use a two-stage `trade/no-trade` then `buy/sell` decision, or restore an explicit hold action.
