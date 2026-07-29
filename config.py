from __future__ import annotations

import os
from pathlib import Path

from twinmarket_kr.rn_model_pin import RN_PAPER_MODEL

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
EXPERIMENT_BASE_DB = OUTPUT_DIR / "experiment_base_sim.db"

STOCK_CODE = "005930"
COUNTERSIDE_USER_ID = "COUNTERSIDE"

# ----- 미사용(dead) 상수 -----
# 아래 네 값은 실행 경로 어디에서도 읽지 않는다. 여기 숫자를 바꿔도 실험은
# 바뀌지 않으므로(FUSE_MEMORY_DESIGN P1의 "false control"), 실제 값이 어디서
# 오는지 함께 적어 둔다. 지우지 않는 이유는 설계 문서가 이 이름들을 참조하기
# 때문이다.
#
# COMMISSION_RATE: 실제 수수료 0은 stages.py 의 계약 검증이 fail-closed 로
#   강제한다(0이 아니면 StageContractError). 설계 문서 M-23 처방대로 config 도
#   0.0 으로 맞춰 두어 config 와 런타임이 다른 정책을 주장하지 않게 한다.
# N_WARMUP: RN burn-in(3거래일)은 봉인된 registry/study_spec 에서 온다.
# N_TRANSITION, CIRCUIT_BREAKER: 현재 어느 경로에서도 쓰지 않는다.
COMMISSION_RATE = 0.0
CIRCUIT_BREAKER = 0.30
N_WARMUP = 3
N_TRANSITION = 4

INI_CASH_SMALL = 100_000_000
INI_CASH_LARGE = 1_000_000_000
MIN_ORDER_UNIT = 1
RANDOM_SEED = 2
# 100명 기준 depth 분포는 D2 15 / D1 55 / D0 30 이다.
# 봉인된 persona_snapshot(preparation/rn_ab_persona_snapshot_v1/)과 sys_100.db가
# 이미 이 분포이므로, 재생성 경로도 같은 값을 내도록 맞춘다.
NEWS_DEPTH2_RATIO = 0.15
NEWS_DEPTH0_COUNT = 30
MAX_SINGLE_TRADE_CASH_RATIO = 0.50
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

# 미사용(dead). 실험 구간은 CLI 인자(--start-date/--end-date) 또는 RN 봉인
# calendar registry 에서 결정된다. 여기에 날짜를 적어도 반영되지 않는다.
EXPERIMENT_START_DATE = ""
EXPERIMENT_END_DATE = ""

load_dotenv(PROJECT_ROOT / ".env")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# RN paper execution is deliberately pinned to this one model.  The strict RN
# call path rejects any other model before constructing an HTTP request.
PAPER_OPENROUTER_MODEL = RN_PAPER_MODEL
PAPER_REASONING_DISABLED_MODEL = PAPER_OPENROUTER_MODEL
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", PAPER_OPENROUTER_MODEL)
OPENROUTER_EMBED_MODEL = os.getenv("OPENROUTER_EMBED_MODEL", "")
OPENROUTER_MAX_RETRIES = int(os.getenv("OPENROUTER_MAX_RETRIES", "6"))
OPENROUTER_RETRY_MAX_DELAY = float(os.getenv("OPENROUTER_RETRY_MAX_DELAY", "30"))
OPENROUTER_GLOBAL_CONCURRENCY = int(os.getenv("OPENROUTER_GLOBAL_CONCURRENCY", "16"))
OPENROUTER_REQUIRE_PARAMETERS = os.getenv("OPENROUTER_REQUIRE_PARAMETERS", "true").strip().lower() in {
    "1",
    "true",
    "yes",
}
OPENROUTER_ALLOW_FALLBACKS = os.getenv("OPENROUTER_ALLOW_FALLBACKS", "true").strip().lower() in {
    "1",
    "true",
    "yes",
}
OPENROUTER_PROVIDER_ORDER = [
    item.strip()
    for item in os.getenv("OPENROUTER_PROVIDER_ORDER", "").split(",")
    if item.strip()
]
OPENROUTER_SLOT_DIR = OUTPUT_DIR / ".openrouter_slots"
OPENROUTER_AUDIT_LOG = Path(
    os.getenv("OPENROUTER_AUDIT_LOG", str(OUTPUT_DIR / "openrouter_calls.jsonl"))
)
SIMULATION_CONCURRENCY = int(os.getenv("SIMULATION_CONCURRENCY", "30"))


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
