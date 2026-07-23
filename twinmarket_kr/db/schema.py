from __future__ import annotations


AGENTS_DDL = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    source_user_id TEXT NOT NULL,
    user_type TEXT NOT NULL,
    gender TEXT NOT NULL,
    age INTEGER NOT NULL,
    age_group TEXT NOT NULL,
    location TEXT NOT NULL,
    bh_disposition_effect_category TEXT NOT NULL,
    bh_lottery_preference_category TEXT NOT NULL,
    bh_total_return_category TEXT NOT NULL,
    bh_underdiversification_category TEXT NOT NULL,
    strategy TEXT NOT NULL,
    trad_pro INTEGER NOT NULL DEFAULT 0,
    fol_ind TEXT NOT NULL,
    ini_cash INTEGER NOT NULL,
    news_depth INTEGER NOT NULL DEFAULT 1,
    segment_key TEXT NOT NULL,
    match_score INTEGER NOT NULL,
    persona_prompt TEXT NOT NULL
);
"""

# ``rn_ab_v9`` retains every legacy market-analysis field, adds the typed
# directional stance/evidence references required by the RN study, stores
# deterministic human-log content separately from causal scientific hashes,
# and binds every eligible community-posting decision to a private trace.
# This version is written only by ``init_paper_sim_db``; the legacy simulation
# initializer deliberately does not materialize the paper tables.
SIM_SCHEMA_VERSION = "rn_ab_v9"


SIM_DDLS = [
    """
    CREATE TABLE IF NOT EXISTS belief_history (
        belief_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        turn INTEGER NOT NULL,
        date TEXT NOT NULL,
        dim_1 TEXT,
        dim_2 TEXT,
        dim_3 TEXT,
        dim_4 TEXT,
        dim_5 TEXT,
        dim_6 TEXT,
        belief_summary TEXT NOT NULL,
        view_change TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS portfolio_state (
        state_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        turn INTEGER NOT NULL,
        date TEXT NOT NULL,
        cash REAL NOT NULL,
        positions TEXT NOT NULL,
        total_value REAL NOT NULL,
        realized_pnl REAL NOT NULL,
        total_return_rate REAL NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS trade_log (
        log_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        turn INTEGER NOT NULL,
        date TEXT NOT NULL,
        action TEXT NOT NULL,
        stock_code TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        executed_price REAL,
        trade_value REAL,
        fee REAL NOT NULL,
        action_reason TEXT,
        risk_control TEXT,
        order_type TEXT,
        submitted_price REAL,
        status TEXT NOT NULL DEFAULT 'pending',
        filled_quantity INTEGER NOT NULL DEFAULT 0
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_system_messages (
        message_id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        turn INTEGER NOT NULL,
        date TEXT NOT NULL,
        message_type TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS StockData (
        date TEXT NOT NULL,
        stock_id TEXT NOT NULL,
        open_price REAL,
        high_price REAL,
        low_price REAL,
        close_price REAL NOT NULL,
        volume REAL,
        pct_chg REAL,
        volume_chg REAL,
        ma5 REAL,
        ma20 REAL,
        volatility_20d REAL,
        PRIMARY KEY (date, stock_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS TradingDetails (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        stock_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        trading_direction TEXT NOT NULL,
        price REAL NOT NULL,
        volume INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS community_posts (
        post_id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        anonymous_code TEXT NOT NULL,
        turn INTEGER NOT NULL,
        date TEXT NOT NULL,
        post_type TEXT NOT NULL,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        like_count INTEGER NOT NULL DEFAULT 0,
        unlike_count INTEGER NOT NULL DEFAULT 0,
        score INTEGER NOT NULL DEFAULT 0,
        is_best INTEGER NOT NULL DEFAULT 0
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS community_interactions (
        interaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        post_id INTEGER NOT NULL,
        turn INTEGER NOT NULL,
        date TEXT NOT NULL,
        reaction TEXT NOT NULL,
        UNIQUE(agent_id, post_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS community_logs (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        turn INTEGER NOT NULL,
        date TEXT NOT NULL,
        best_posts_seen TEXT,
        posts_read TEXT,
        community_thinking TEXT,
        UNIQUE(agent_id, turn)
    );
    """,
]


# Kept as a named DDL because an empty pre-v7 paper database can safely rebuild
# this one table without rewriting a scientific row.  See
# ``connection._upgrade_empty_analysis_contract_schema``.
PAPER_ANALYSES_DDL = """
    CREATE TABLE IF NOT EXISTS paper_analyses (
        analysis_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        turn INTEGER NOT NULL CHECK(turn > 0),
        date TEXT NOT NULL,
        subturn TEXT NOT NULL CHECK(subturn IN ('am', 'pm')),
        source_ltb_id TEXT NOT NULL REFERENCES paper_ltb_states(ltb_id),
        source_stb_id TEXT NOT NULL REFERENCES short_term_belief_history(stb_id),
        input_sha256 TEXT NOT NULL,
        response_sha256 TEXT NOT NULL,
        market_view TEXT NOT NULL,
        valuation_view TEXT NOT NULL,
        technical_view TEXT NOT NULL,
        news_view TEXT NOT NULL,
        portfolio_view TEXT NOT NULL,
        key_risks_json TEXT NOT NULL,
        opportunity_json TEXT NOT NULL,
        caution_json TEXT NOT NULL,
        confidence TEXT NOT NULL CHECK(confidence IN ('low', 'medium', 'high')),
        directional_stance TEXT NOT NULL CHECK(directional_stance IN ('buy', 'sell', 'uncertain')),
        evidence_references_json TEXT NOT NULL,
        scientific_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(run_id, condition_id, agent_id, event_id)
    );
    """


# These tables are intentionally additive.  Legacy simulations still use the
# original CSV/SQLite convention, while the RN Community AB path records its
# scientific state in a run/condition namespace with append-only keys.
PAPER_SIM_DDLS = [
    """
    CREATE TABLE IF NOT EXISTS rn_schema_meta (
        schema_key TEXT PRIMARY KEY,
        schema_value TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_initial_portfolios (
        initial_portfolio_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        cash REAL NOT NULL CHECK(cash >= 0),
        quantity INTEGER NOT NULL CHECK(quantity >= 0),
        scientific_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(run_id, condition_id, agent_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS short_term_belief_history (
        stb_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        turn INTEGER NOT NULL CHECK(turn > 0),
        date TEXT NOT NULL,
        dim_1 TEXT NOT NULL,
        dim_2 TEXT NOT NULL,
        dim_3 TEXT NOT NULL,
        dim_4 TEXT NOT NULL,
        dim_5 TEXT NOT NULL,
        dim_6 TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        dimension_evidence_json TEXT NOT NULL,
        input_sha256 TEXT NOT NULL,
        scientific_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(run_id, condition_id, agent_id, event_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_ltb_states (
        ltb_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        turn INTEGER NOT NULL CHECK(turn >= 0),
        visible_from_turn INTEGER NOT NULL CHECK(visible_from_turn > turn),
        date TEXT NOT NULL,
        parent_ltb_id TEXT REFERENCES paper_ltb_states(ltb_id),
        current_stb_id TEXT REFERENCES short_term_belief_history(stb_id),
        dim_1 TEXT NOT NULL,
        dim_2 TEXT NOT NULL,
        dim_3 TEXT NOT NULL,
        dim_4 TEXT NOT NULL,
        dim_5 TEXT NOT NULL,
        dim_6 TEXT NOT NULL,
        scientific_sha256 TEXT NOT NULL,
        belief_summary TEXT NOT NULL,
        view_change_json TEXT NOT NULL,
        human_log_renderer_version TEXT NOT NULL,
        human_log_renderer_sha256 TEXT NOT NULL,
        human_log_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(run_id, condition_id, agent_id, event_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS ltb_dimension_transitions (
        transition_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        turn INTEGER NOT NULL CHECK(turn > 0),
        parent_ltb_id TEXT NOT NULL REFERENCES paper_ltb_states(ltb_id),
        stb_id TEXT NOT NULL REFERENCES short_term_belief_history(stb_id),
        ltb_id TEXT NOT NULL UNIQUE REFERENCES paper_ltb_states(ltb_id),
        fill_id TEXT,
        transaction_episode_json TEXT NOT NULL,
        integration_evidence_json TEXT NOT NULL,
        integration_evidence_by_dimension_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(run_id, condition_id, agent_id, event_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_fill_ledger (
        fill_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        turn INTEGER NOT NULL CHECK(turn > 0),
        date TEXT NOT NULL,
        subturn TEXT NOT NULL CHECK(subturn IN ('am', 'pm')),
        stock_code TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('buy', 'sell')),
        requested_quantity INTEGER NOT NULL CHECK(requested_quantity > 0),
        filled_quantity INTEGER NOT NULL CHECK(filled_quantity = requested_quantity),
        executed_price REAL NOT NULL CHECK(executed_price > 0),
        fee_amount REAL NOT NULL DEFAULT 0 CHECK(fee_amount = 0),
        source_ltb_id TEXT NOT NULL REFERENCES paper_ltb_states(ltb_id),
        source_stb_id TEXT NOT NULL REFERENCES short_term_belief_history(stb_id),
        decision_id TEXT NOT NULL REFERENCES paper_decisions(decision_id),
        pre_portfolio_json TEXT NOT NULL,
        post_portfolio_json TEXT NOT NULL,
        scientific_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(run_id, condition_id, agent_id, event_id)
    );
    """,
    PAPER_ANALYSES_DDL,
    """
    CREATE TABLE IF NOT EXISTS paper_decisions (
        decision_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        turn INTEGER NOT NULL CHECK(turn > 0),
        date TEXT NOT NULL,
        subturn TEXT NOT NULL CHECK(subturn IN ('am', 'pm')),
        action TEXT NOT NULL CHECK(action IN ('buy', 'sell')),
        requested_quantity INTEGER NOT NULL CHECK(requested_quantity > 0),
        source_ltb_id TEXT NOT NULL REFERENCES paper_ltb_states(ltb_id),
        source_stb_id TEXT NOT NULL REFERENCES short_term_belief_history(stb_id),
        analysis_id TEXT NOT NULL REFERENCES paper_analyses(analysis_id),
        input_sha256 TEXT NOT NULL,
        response_sha256 TEXT NOT NULL,
        scientific_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(run_id, condition_id, agent_id, event_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS trade_outcomes (
        outcome_id TEXT PRIMARY KEY,
        fill_id TEXT NOT NULL REFERENCES paper_fill_ledger(fill_id),
        horizon TEXT NOT NULL CHECK(horizon IN ('next_turn', 'h1', 'h5')),
        available_from_event_id TEXT,
        observed_event_id TEXT,
        mark_price REAL,
        status TEXT NOT NULL CHECK(status IN ('pending', 'matured', 'right_censored')),
        scientific_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(fill_id, horizon)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS ltb_outcome_consumptions (
        consumption_id TEXT PRIMARY KEY,
        fill_id TEXT NOT NULL,
        horizon TEXT NOT NULL,
        transition_id TEXT NOT NULL REFERENCES ltb_dimension_transitions(transition_id),
        consumed_at_event_id TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(fill_id, horizon),
        FOREIGN KEY(fill_id, horizon) REFERENCES trade_outcomes(fill_id, horizon)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS observation_events (
        observation_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        turn INTEGER NOT NULL,
        date TEXT NOT NULL,
        stage TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(run_id, condition_id, event_id, stage)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_exposures (
        exposure_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        channel TEXT NOT NULL CHECK(channel IN ('news', 'community_best', 'community_selected')),
        root_id TEXT NOT NULL,
        body_sha256 TEXT NOT NULL,
        delivered_at TEXT,
        status TEXT NOT NULL CHECK(status IN ('scheduled', 'delivered', 'right_censored')),
        metadata_json TEXT NOT NULL,
        UNIQUE(run_id, condition_id, agent_id, event_id, channel, root_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_evidence_edges (
        edge_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        target_kind TEXT NOT NULL CHECK(target_kind IN ('stb', 'ltb')),
        target_id TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        source_id TEXT NOT NULL,
        dimension TEXT,
        UNIQUE(run_id, condition_id, agent_id, event_id, target_kind, target_id, source_kind, source_id, dimension)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS turn_belief_trace (
        trace_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        turn INTEGER NOT NULL,
        previous_ltb_id TEXT NOT NULL,
        stb_id TEXT NOT NULL,
        decision_id TEXT NOT NULL,
        fill_id TEXT NOT NULL,
        ltb_id TEXT NOT NULL,
        input_sha256 TEXT NOT NULL,
        scientific_sha256 TEXT NOT NULL,
        human_log_json TEXT NOT NULL,
        human_log_renderer_version TEXT NOT NULL,
        human_log_renderer_sha256 TEXT NOT NULL,
        human_log_sha256 TEXT NOT NULL,
        UNIQUE(run_id, condition_id, agent_id, event_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS community_post_trace (
        trace_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        condition_id TEXT NOT NULL CHECK(condition_id = 'RN_COMM_ON'),
        manifest_sha256 TEXT NOT NULL,
        phase_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        turn INTEGER NOT NULL CHECK(turn > 0),
        date TEXT NOT NULL,
        author_agent_id TEXT NOT NULL,
        eligibility_status TEXT NOT NULL CHECK(eligibility_status = 'eligible'),
        posting_status TEXT NOT NULL CHECK(posting_status IN ('posted', 'skipped')),
        post_id TEXT,
        ltb_id TEXT NOT NULL REFERENCES paper_ltb_states(ltb_id),
        ltb_sha256 TEXT NOT NULL,
        view_change_id TEXT NOT NULL,
        view_change_sha256 TEXT NOT NULL,
        fill_id TEXT NOT NULL REFERENCES paper_fill_ledger(fill_id),
        prompt_template_sha256 TEXT NOT NULL,
        prompt_values_sha256 TEXT NOT NULL,
        logical_call_id TEXT NOT NULL,
        accepted_response_sha256 TEXT NOT NULL,
        title_sha256 TEXT,
        body_sha256 TEXT,
        trace_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK(
            (
                posting_status = 'posted'
                AND post_id IS NOT NULL
                AND title_sha256 IS NOT NULL
                AND body_sha256 IS NOT NULL
            )
            OR
            (
                posting_status = 'skipped'
                AND post_id IS NULL
                AND title_sha256 IS NULL
                AND body_sha256 IS NULL
            )
        ),
        UNIQUE(run_id, condition_id, phase_id, author_agent_id),
        UNIQUE(run_id, condition_id, logical_call_id),
        UNIQUE(run_id, condition_id, post_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS phase_consumptions (
        consumption_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        condition_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        source_id TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(run_id, condition_id, agent_id, event_id, stage, source_kind, source_id)
    );
    """,
]


PAPER_RUNTIME_TABLES = (
    "paper_initial_portfolios",
    "short_term_belief_history",
    "paper_ltb_states",
    "ltb_dimension_transitions",
    "paper_fill_ledger",
    "paper_analyses",
    "paper_decisions",
    "trade_outcomes",
    "ltb_outcome_consumptions",
    "observation_events",
    "agent_exposures",
    "memory_evidence_edges",
    "turn_belief_trace",
    "community_post_trace",
    "phase_consumptions",
)


def create_agents_table_sql() -> str:
    return AGENTS_DDL


def create_sim_tables_sql() -> list[str]:
    """Legacy simulation schema only.

    The RN Community AB study has a separate sealed database initializer.  It
    must never materialize paper-study tables inside an exploratory/0720 run
    merely because that run initialized its normal simulation database.
    """
    return SIM_DDLS


def create_paper_sim_tables_sql() -> list[str]:
    """Schema for a new, run-scoped RN Community AB database."""
    return [*SIM_DDLS, *PAPER_SIM_DDLS]
