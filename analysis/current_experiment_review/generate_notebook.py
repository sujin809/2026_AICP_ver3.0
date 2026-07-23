#!/usr/bin/env python3
"""Generate the runnable companion notebook with nbformat."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import nbformat as nbf


HERE = Path(__file__).resolve().parent
NOTEBOOK_PATH = HERE / "current_experiment_audit.ipynb"


def markdown(text: str):
    return nbf.v4.new_markdown_cell(dedent(text).strip())


def code(text: str):
    return nbf.v4.new_code_cell(dedent(text).strip())


notebook = nbf.v4.new_notebook()
notebook["metadata"] = {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "version": "3"},
}
notebook["cells"] = [
    markdown(
        """
        # Current C00 pilot audit

        ## tl;dr

        This companion reproduces the integrity and metric checks used in the review of the only current result, C00:

        - state continuity and duplicate/missing `agent × date × subturn` records;
        - treatment delivery, belief coverage, and forced buy/sell mechanics;
        - validation metrics alongside the repository's simple baselines;
        - persona-population versus active-cohort coverage.

        The remaining five information-environment conditions are a proposed future factorial design, not observed experimental results in this notebook.
        """
    ),
    markdown(
        """
        ## Context & Methods

        ### Key Assumptions

        - The C00 root `outputs/logs/<run_id>/agent_turns.jsonl` file is the authoritative execution log.
        - Its matching `validation/outputs/<run_id>/summary_metrics.json` file is the authoritative source for reported direction metrics.
        - A valid continuous 63-day run has 30 agents × 63 dates × AM/PM = 3,780 unique agent-turn keys and a global turn sequence spanning 1–126.
        - Fake `read` and `selected` indicators are post-treatment mechanisms. They are described but never conditioned on as causal controls.
        """
    ),
    code(
        """
        from pathlib import Path
        import subprocess
        import sys
        import pandas as pd

        project_root = Path.cwd()
        if not (project_root / 'analysis' / 'current_experiment_review').exists():
            raise RuntimeError('Run this notebook from the repository root.')

        subprocess.run(
            [sys.executable, 'analysis/current_experiment_review/analyze_current_runs.py'],
            cwd=project_root,
            check=True,
        )
        artifact_dir = project_root / 'analysis/current_experiment_review/outputs'
        """
    ),
    markdown("## Data"),
    code(
        """
        audit = pd.read_csv(artifact_dir / 'run_integrity_audit.csv')
        benchmarks = pd.read_csv(artifact_dir / 'validation_benchmarks.csv')
        cohort = pd.read_csv(artifact_dir / 'persona_population_vs_active_cohort.csv')
        persona_descriptives = pd.read_csv(artifact_dir / 'c00_persona_behavior_descriptives.csv')
        log_inventory = pd.read_csv(artifact_dir / 'c00_log_inventory.csv')
        daily_decisions = pd.read_csv(artifact_dir / 'c00_daily_decision_summary.csv')
        chunk_boundaries = pd.read_csv(artifact_dir / 'c00_chunk_boundary_summary.csv')
        validation_by_chunk = pd.read_csv(artifact_dir / 'c00_validation_by_chunk.csv')

        audit[['condition', 'label', 'raw_rows', 'unique_agent_date_subturn_rows',
               'duplicate_key_rows', 'missing_key_rows', 'turn_max',
               'restart_date_count', 'fake_exposure_slots', 'blank_belief_rate',
               'fallback_decision_rows', 'fallback_sell_orders']]
        """
    ),
    markdown("## Results"),
    code(
        """
        metric_columns = ['condition', 'direction_match_rate', 'balanced_accuracy',
                          'buy_recall', 'sell_recall', 'daily_pearson',
                          'validation_overlap_days']
        audit[metric_columns].sort_values('condition')
        """
    ),
    markdown("### C00 log map"),
    code(
        """
        log_inventory.groupby(['surface', 'scope', 'category', 'grain', 'interpretation_use'], as_index=False).agg(
            files=('path', 'count'),
            total_bytes=('bytes', 'sum')
        ).sort_values(['surface', 'scope', 'category'])
        """
    ),
    code(
        """
        # Root-level sources are the joined reading path.  Chunk copies are useful for detecting restart artifacts.
        log_inventory.loc[log_inventory['scope'].eq('root'),
                          ['path', 'category', 'grain', 'interpretation_use', 'line_count']]
        """
    ),
    markdown("### State continuity and decision trace"),
    code(
        """
        chunk_boundaries
        """
    ),
    code(
        """
        daily_decisions[['date', 'turn', 'subturn', 'agent_rows', 'buy_orders', 'sell_orders',
                         'net_submitted_quantity', 'initial_portfolio_agents', 'blank_belief_rows',
                         'positive_news_sentiment', 'negative_news_sentiment', 'mixed_news_sentiment']]
        """
    ),
    code(
        """
        benchmarks.loc[
            benchmarks['benchmark'].isin(['always_buy', 'previous_day_market_return_direction']),
            ['condition', 'benchmark', 'direction_match_rate', 'balanced_accuracy',
             'buy_recall', 'sell_recall']
        ].sort_values(['condition', 'benchmark'])
        """
    ),
    code(
        """
        # These are not independent replications.  They expose the variability
        # concealed by a single 58-day aggregate while each chunk restarts state.
        validation_by_chunk
        """
    ),
    code(
        """
        cohort.loc[cohort['field'].isin(['age_group', 'ini_cash', 'strategy', 'news_depth'])]
        """
    ),
    code(
        """
        # Descriptive only: this is not a persona-effect estimate because C00 has
        # 30 homogeneous-capital agents and several sparse persona cells.
        persona_descriptives.loc[
            persona_descriptives['persona_field'].isin(['age_group', 'strategy', 'news_depth']),
            ['persona_field', 'persona_value', 'agent_count', 'buy_share', 'sell_share',
             'one_share_rate', 'blank_belief_rate']
        ]
        """
    ),
    markdown(
        """
        ## Takeaways

        Interpret any numerical difference only after the execution-integrity table is clean.  In particular, a repeated local turn range or a reset to the initial portfolio means a multi-day trajectory, memory effect, return path, and aggregate-flow comparison cannot be treated as the intended continuous experiment.  Missing belief text and incomplete fake delivery also bound which embedding or rubric analyses are usable.
        """
    ),
]

nbf.write(notebook, NOTEBOOK_PATH)
print(f"Wrote {NOTEBOOK_PATH}")
