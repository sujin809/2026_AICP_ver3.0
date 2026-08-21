#!/usr/bin/env bash
# RN_COMM_OFF + RN_COMM_ON paired arms, ver5 cohort (momentum/contrarian axis),
# 45 trading days (2026-02-27 .. 2026-05-04), 100 agents, 삼성전자 005930.
# run ID: rn_ab_ver5_45day_20260820
#
# Re-run this exact script after an interruption; 08 hands the same immutable
# signature to each 05 arm, so completed events replay from the journal without
# re-billing.
set -euo pipefail
cd "$(dirname "$0")/.."

export TWINMARKET_SYS_100_DB="$PWD/data/sys_100_ko_ver5.db"
export TWINMARKET_SEALED_PROFILE="rn_ab_ver5_v1"
unset TWINMARKET_OFFLINE_LLM

exec .venv/bin/python scripts/08_run_six_conditions.py \
  --conditions RN_COMM_OFF RN_COMM_ON \
  --real-profile-root   "$PWD/preparation/rn_ab_ver5_v1" \
  --experiment-base-db  "$PWD/outputs/experiment_base_ver5.db" \
  --output-root         "$PWD/outputs/logs/rn_ab_ver5_45day_20260820" \
  --reasoning-off-canary-audit "$PWD/preparation/reasoning_off_canary_20260730.jsonl" \
  --allow-paid-api \
  "$@"
