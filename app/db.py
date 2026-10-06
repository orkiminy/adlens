"""SQLite storage: campaigns, events (impressions, clicks, conversions, holdout exposures), segments."""
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    advertiser_id TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    creative      TEXT    NOT NULL,           -- headline shown in the ad
    landing_url   TEXT    NOT NULL,
    bid_cpm       REAL    NOT NULL,           -- max price per 1,000 impressions (USD)
    budget        REAL    NOT NULL,           -- total budget (USD)
    spend         REAL    NOT NULL DEFAULT 0,
    geos          TEXT,                       -- comma list, NULL = any
    devices       TEXT,                       -- comma list, NULL = any
    interests     TEXT,                       -- comma list, NULL = any
    segment_id    INTEGER,                    -- first-party audience, NULL = any
    freq_cap      INTEGER NOT NULL DEFAULT 3, -- max impressions per user per 24h
    holdout_pct   INTEGER NOT NULL DEFAULT 10 -- % of users kept as a control group
);

-- One row per tracked event. 'eligible_control' = a holdout user who WOULD have
-- won this campaign's auction but was not shown the ad ("ghost ad"), used for lift.
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    type          TEXT    NOT NULL CHECK (type IN ('impression','click','conversion','eligible_control')),
    user_id       TEXT    NOT NULL,
    campaign_id   INTEGER,
    advertiser_id TEXT    NOT NULL,
    price         REAL    NOT NULL DEFAULT 0, -- cost of this impression (USD)
    value         REAL    NOT NULL DEFAULT 0  -- conversion value (USD)
);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id, advertiser_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_campaign ON events(campaign_id, type);
CREATE INDEX IF NOT EXISTS idx_events_freq ON events(user_id, campaign_id, type, ts);

-- First-party audience segments (DMP-style): users who did EVENT_TYPE for
-- ADVERTISER_ID within LOOKBACK_DAYS.
CREATE TABLE IF NOT EXISTS segments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    advertiser_id TEXT    NOT NULL,
    event_type    TEXT    NOT NULL CHECK (event_type IN ('impression','click','conversion')),
    lookback_days REAL    NOT NULL DEFAULT 30
);
"""


class DB:
    """Thin thread-safe wrapper around one SQLite connection."""

    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.conn.executescript(SCHEMA)

    def execute(self, sql: str, params=()):
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def query(self, sql: str, params=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None
