import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address           TEXT PRIMARY KEY,
    first_seen_ts     INTEGER NOT NULL,
    last_ingested_ts  INTEGER,
    source            TEXT,
    display_name      TEXT,
    active            INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS markets (
    condition_id      TEXT PRIMARY KEY,
    slug              TEXT,
    question          TEXT,
    end_date_ts       INTEGER,
    resolution        TEXT,
    outcomes_json     TEXT,
    yes_token_id      TEXT,
    no_token_id       TEXT,
    liquidity_usd     REAL,
    last_refreshed_ts INTEGER
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id      TEXT PRIMARY KEY,
    wallet        TEXT NOT NULL,
    condition_id  TEXT NOT NULL,
    token_id      TEXT NOT NULL,
    side          TEXT NOT NULL,
    size_shares   REAL NOT NULL,
    price         REAL NOT NULL,
    notional_usd  REAL NOT NULL,
    ts            INTEGER NOT NULL,
    tx_hash       TEXT,
    ingested_ts   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_wallet_ts ON trades(wallet, ts);
CREATE INDEX IF NOT EXISTS idx_trades_market_ts ON trades(condition_id, ts);
CREATE INDEX IF NOT EXISTS idx_trades_token_ts ON trades(token_id, ts);

CREATE TABLE IF NOT EXISTS positions (
    wallet            TEXT NOT NULL,
    token_id          TEXT NOT NULL,
    shares            REAL NOT NULL,
    avg_cost          REAL NOT NULL,
    realized_pnl_usd  REAL NOT NULL,
    last_updated_ts   INTEGER NOT NULL,
    PRIMARY KEY (wallet, token_id)
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    token_id  TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    price     REAL NOT NULL,
    source    TEXT NOT NULL,
    PRIMARY KEY (token_id, ts, source)
);
CREATE INDEX IF NOT EXISTS idx_snap_token_ts ON price_snapshots(token_id, ts);

CREATE TABLE IF NOT EXISTS scores (
    wallet              TEXT NOT NULL,
    as_of_date          TEXT NOT NULL,
    score               REAL NOT NULL,
    stars               INTEGER NOT NULL,
    followability       REAL,
    roi_raw             REAL,
    roi_pct             REAL,
    sharpe_raw          REAL,
    sharpe_pct          REAL,
    max_dd_raw          REAL,
    max_dd_pct          REAL,
    avg_hold_secs       REAL,
    avg_hold_pct        REAL,
    early_entry_raw     REAL,
    early_entry_pct     REAL,
    market_impact_bonus REAL,
    confidence_mult     REAL,
    trade_count         INTEGER NOT NULL,
    followable_roi_1m   REAL,
    followable_roi_5m   REAL,
    followable_roi_15m  REAL,
    slippage_raw        REAL,
    liquidity_raw       REAL,
    notional_usd_total  REAL,
    PRIMARY KEY (wallet, as_of_date)
);

CREATE TABLE IF NOT EXISTS report_runs (
    run_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of_date           TEXT UNIQUE NOT NULL,
    generated_ts         INTEGER NOT NULL,
    top_n_addresses_json TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
