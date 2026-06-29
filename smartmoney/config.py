from pathlib import Path

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
SUBGRAPH_URL = (
    "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw"
    "/subgraphs/activity-subgraph/0.0.4/gn"
)
WS_RTDS = "wss://ws-live-data.polymarket.com"
WS_CLOB = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

HOME = Path.home() / ".smartmoney"
DB_PATH = HOME / "smartmoney.db"
REPORTS_DIR = HOME / "reports"
HEARTBEAT_PATH = HOME / "archiver.heartbeat"

TOP_N_PER_PERIOD = 500
LEADERBOARD_PERIODS = ("1d", "7d", "30d", "all")
DISCOVERY_MIN_NOTIONAL_USD = 5000.0
MIN_TRADES_FOR_SCORING = 5

SCORE_WEIGHTS = {
    "roi": 0.40,
    "sharpe": 0.20,
    "max_dd": 0.15,
    "avg_hold": 0.15,
    "early_entry": 0.10,
}
FOLLOWABILITY_WEIGHTS = {
    "followable_roi": 0.50,
    "slippage_inv": 0.20,
    "liquidity": 0.15,
    "avg_hold": 0.15,
}

# Star thresholds: <50→0, 50-65→1, 65-80→2, 80-90→3, 90-95→4, ≥95→5
STAR_BUCKETS = (50, 65, 80, 90, 95)

# Followable-ROI delays in seconds. Add 5/10/30 once archiver has 2+ weeks of ticks.
FOLLOW_DELAYS_SEC = (60, 300, 900)

# Early-entry lookahead: how far after their buy do we measure price drift
EARLY_ENTRY_LOOKAHEAD_SEC = 15 * 60
EARLY_ENTRY_THRESHOLD_PCT = 0.01  # price must move >1% in their direction

MARKET_IMPACT_LOOKAHEAD_SEC = 60
MARKET_IMPACT_MAX_BONUS = 5.0

CONFIDENCE_TARGET_TRADES = 1000  # trades count that yields full 1.0 confidence
CONFIDENCE_MAX = 1.2

LOOKBACK_DAYS = 90  # ROI / Sharpe / MaxDD lookback window

ARCHIVER_TOP_TOKENS = 200
ARCHIVER_FLUSH_ROWS = 1000
ARCHIVER_FLUSH_SECS = 2
ARCHIVER_BOOK_SAMPLE_SECS = 5
ARCHIVER_WS_PING_SECS = 5

REPORT_TOP_N = 50

POLYMARKET_ANALYTICS_WALLET_URL = "https://polymarketanalytics.com/wallet/{address}"

HOME.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
