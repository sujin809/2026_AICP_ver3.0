# Multi-view belief embedding analysis plan

## Why the embedding space needs explicit axes

A two-dimensional map of all belief texts is not, by itself, evidence of misinformation susceptibility. It can be driven by repeated prompt style, market regime, date-specific vocabulary, missing text, or persona wording. The analysis therefore treats embedding as a measurement layer for **pre-specified contrasts and trajectories**, not as an automatic clustering result.

The current pilot uses `intfloat/multilingual-e5-small`, normalized vectors, the same `query:` prefix for beliefs and claims, and cosine-based comparisons. The small model is appropriate for a reproducible pilot, but all paper claims must be robust to at least one alternative multilingual sentence embedding model or to human rubric coding.

## Primary embedding views

### 1. Event-targeted claim axis

For every injected bullish article, use its paired bearish article from the same factual anchor. Define the event-specific direction as relative similarity to the bullish claim versus its paired bearish claim. Measure:

- belief position before exposure;
- immediate movement at the exposed turn;
- next-turn movement relative to the same pre-exposure belief;
- matched non-injection movement for the same agent, subturn, and regime.

This is the primary embedding analysis because it is aligned with the manipulation. Absolute sentiment or unsupervised clusters are secondary.

### 2. Sequential trajectory

For each `agent × injected phase`, connect pre-exposure, exposed, and next-turn beliefs. Report immediate movement and persistence separately. Community posts created after PM trading are linked only to the next AM or later; same-turn effects are not assigned.

### 3. Event response geometry

Use the paired difference vector `exposed belief − pre-exposure belief`, rather than the raw belief vector. Project response vectors with PCA only for visualization and compute cosine similarity between event-centroid response vectors. This asks whether different events induce similar **directions of change**, rather than whether their texts share vocabulary.

### 4. Persona-conditioned response

Join response trajectories to the stored modeled-persona fields:

- age group and gender;
- value versus technical strategy;
- news depth 0/1/2;
- ordinary versus influencer user type;
- disposition-effect, lottery-preference, total-return, and under-diversification categories.

For every group, report agent count, valid exposure count, mean immediate claim-axis movement, positive-movement share, persistence, semantic displacement, and action alignment. Groups with fewer than five agents are explicitly marked fragile. These are properties of the modeled personas, not estimates of susceptibility in real demographic populations.

### 5. Belief–action alignment

Cross the embedding claim-axis movement with the observed buy/sell action:

- bullish movement + buy;
- bullish movement + sell;
- bearish movement + buy;
- bearish movement + sell.

This separates language reception from trading translation. Because buy/sell is forced, the paper must repeat this analysis with active trades only or with an explicit no-trade decision.

## Secondary diagnostic views

### Embedding separability audit

Compute cosine silhouette scores for event, regime, event predictability, phase, and persona labels on the response-difference vectors. A low score is informative: it means a visually attractive map should not be interpreted as natural clusters. Clustering is retained only as hypothesis generation.

### Unsupervised response clusters

If used, select the number of clusters through silhouette stability over seeds and report cluster composition across event and persona axes. Do not name clusters as psychological types without blind text review.

## Required robustness checks

- Compare claim-axis results with the blind reception/stance rubric.
- Recompute after removing boilerplate or concatenating belief summary with decision/risk text.
- Repeat with raw belief summaries and with change-only text if a reliable change extraction is available.
- Repeat with at least one alternative multilingual embedding model.
- Compare cosine and centered-dot-product results.
- Bootstrap at agent and event/run level, not agent-turn level alone.
- Report the missing-belief rate by condition and persona group.

## Paper priority

1. Primary: paired-claim trajectory and condition contrasts.
2. Primary mechanism: persistence and belief–action alignment.
3. Heterogeneity: event and modeled-persona contrasts with uncertainty.
4. Diagnostic: response-space PCA and event-centroid similarity.
5. Exploratory only: unsupervised clusters and broad UMAP maps.
