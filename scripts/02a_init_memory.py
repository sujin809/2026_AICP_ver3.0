#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import config
from twinmarket_kr.agents.memory_agent import MemoryAgent, load_agents_from_sys100


def main() -> None:
    agents = load_agents_from_sys100(config.SYS_100_DB)
    memory = MemoryAgent(config.SIM_DB)
    memory.init_portfolio_t000(agents)
    print(f"initialized portfolio_state t000 for {len(agents)} agents in {config.SIM_DB}")


if __name__ == "__main__":
    main()
