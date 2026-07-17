from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(*args, **kwargs) -> bool:  # type: ignore[no-redef]
        return False


PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "outputs"
PROMPT_DIR = PROJECT_ROOT / "prompts"
LOG_DIR = OUTPUT_DIR / "logs"

SYS_1000_DB = DATA_DIR / "sys_1000.db"
SYS_1000_CSV = DATA_DIR / "sys_1000.csv"
FIXED_SLOTS_CSV = DATA_DIR / "fixed_slots.csv"
STOCK_DATA_CSV = DATA_DIR / "stock_data.csv"
TRADING_DAYS_CSV = DATA_DIR / "trading_days.csv"
SAMSUNG_NEWS_RAW_PKL = DATA_DIR / "samsung_news_raw.pkl"
FAKE_NEWS_BEARISH_PKL = DATA_DIR / "fake_news_bearish_phase_review.pkl"
FAKE_NEWS_BULLISH_PKL = DATA_DIR / "fake_news_bullish_phase_review.pkl"
PROCESSED_NEWS_CSV = OUTPUT_DIR / "processed_news.csv"
DAILY_NEWS_SELECTION_CSV = OUTPUT_DIR / "daily_news_selection.csv"
PROCESSED_NEWS_INJECTION_CSV = OUTPUT_DIR / "processed_news_injection.csv"
DAILY_NEWS_SELECTION_INJECTION_CSV = OUTPUT_DIR / "daily_news_selection_injection.csv"
PROCESSED_NEWS_INJECTION_BEARISH_CSV = OUTPUT_DIR / "processed_news_injection_bearish.csv"
DAILY_NEWS_SELECTION_INJECTION_BEARISH_CSV = OUTPUT_DIR / "daily_news_selection_injection_bearish.csv"
PROCESSED_NEWS_INJECTION_BULLISH_CSV = OUTPUT_DIR / "processed_news_injection_bullish.csv"
DAILY_NEWS_SELECTION_INJECTION_BULLISH_CSV = OUTPUT_DIR / "daily_news_selection_injection_bullish.csv"
SYS_100_DB = OUTPUT_DIR / "sys_100.db"
SIM_DB = OUTPUT_DIR / "sim.db"

STOCK_CODE = "005930"
COUNTERSIDE_USER_ID = "COUNTERSIDE"
COMMISSION_RATE = 0.0005
CIRCUIT_BREAKER = 0.30
N_WARMUP = 3
N_TRANSITION = 4
INI_CASH_SMALL = 100_000_000
INI_CASH_LARGE = 1_000_000_000
MIN_ORDER_UNIT = 1
RANDOM_SEED = 2
NEWS_DEPTH2_RATIO = 0.30
NEWS_DEPTH0_COUNT = 15
MAX_SINGLE_TRADE_CASH_RATIO = 0.50
SIMULATION_CONCURRENCY = 8
MARKET_CLOSE_TIME = "15:30"
ORDER_CUTOFF_TIME = "15:30"

BELIEF_LIMITS = {
    "dim_1": 150,
    "dim_2": 100,
    "dim_3": 100,
    "dim_4": 100,
    "dim_5": 100,
    "dim_6": 100,
}

EXPERIMENT_START_DATE = ""
EXPERIMENT_END_DATE = ""

load_dotenv(PROJECT_ROOT / ".env")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o")
OPENROUTER_EMBED_MODEL = os.getenv("OPENROUTER_EMBED_MODEL", "")


# ===== Community Settings =====
ENABLE_COMMUNITY: bool = True
ENABLE_COMMUNITY_POSTING: bool = True
ENABLE_COMMUNITY_READING: bool = True

COMMUNITY_DEPTH1_READ_LIMIT: int = 5
COMMUNITY_DEPTH2_READ_LIMIT: int = 10
COMMUNITY_BEST_POST_COUNT: int = 5

BADGE_TOP_RETURN_PERCENTILE: int = 20
BADGE_TOP_ASSET_PERCENTILE: int = 20
BADGE_INFLUENCER_PERCENTILE: int = 20

# If no separate community model is configured, keep every LLM call on the
# primary model rather than silently falling back to a billable OpenAI model.
OPENROUTER_COMMUNITY_MODEL: str = os.getenv("OPENROUTER_COMMUNITY_MODEL", OPENROUTER_MODEL)


def ensure_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PROMPT_DIR.mkdir(parents=True, exist_ok=True)
