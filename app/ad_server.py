"""Ad decisioning: targeting, budget pacing, frequency capping, second-price auction, holdout groups."""
import hashlib
import time
from dataclasses import dataclass

from .db import DB

DAY = 86_400
FLOOR_CPM = 5.00  # reserve price per 1,000 impressions (stands in for open-market competition)


@dataclass
class AdRequest:
    user_id: str
    geo: str | None = None
    device: str | None = None
    interests: tuple[str, ...] = ()
    ts: float | None = None  # lets the simulator replay time


def in_holdout(user_id: str, advertiser_id: str, pct: int) -> bool:
    """Deterministic assignment: a user is always in the same group for an advertiser.

    The holdout is per advertiser (not per campaign) so control users never see
    ANY of that advertiser's ads, which keeps the control group clean.
    """
    h = int(hashlib.sha256(f"{user_id}:{advertiser_id}".encode()).hexdigest(), 16)
    return h % 100 < pct


def _matches(allowed: str | None, value) -> bool:
    if not allowed:
        return True
    options = {a.strip() for a in allowed.split(",")}
    if isinstance(value, (tuple, list, set)):
        return bool(options & set(value))
    return value in options


def in_segment(db: DB, segment_id: int, user_id: str, now: float) -> bool:
    seg = db.one("SELECT * FROM segments WHERE id = ?", (segment_id,))
    if not seg:
        return False
    hit = db.one(
        "SELECT 1 FROM events WHERE user_id = ? AND advertiser_id = ? AND type = ? AND ts >= ? LIMIT 1",
        (user_id, seg["advertiser_id"], seg["event_type"], now - seg["lookback_days"] * DAY),
    )
    return hit is not None


def eligible_campaigns(db: DB, req: AdRequest, now: float) -> list[dict]:
    """Campaigns this user may see right now (targeting + budget + frequency cap)."""
    out = []
    for c in db.query("SELECT * FROM campaigns WHERE spend < budget"):
        if not (_matches(c["geos"], req.geo) and _matches(c["devices"], req.device)
                and _matches(c["interests"], req.interests)):
            continue
        if c["segment_id"] and not in_segment(db, c["segment_id"], req.user_id, now):
            continue
        seen = db.one(
            "SELECT COUNT(*) AS n FROM events WHERE type = 'impression' AND user_id = ? AND campaign_id = ? AND ts >= ?",
            (req.user_id, c["id"], now - DAY),
        )["n"]
        if seen >= c["freq_cap"]:
            continue
        out.append(c)
    return out


def serve(db: DB, req: AdRequest) -> dict | None:
    """Run a second-price auction. Returns the winning ad, or None (no fill).

    If this user is in the winning advertiser's holdout group, we log an
    'eligible_control' event (a "ghost ad") and drop all of that advertiser's
    campaigns from the auction, so lift can be measured against users who would
    have seen the ad but did not.
    """
    now = req.ts if req.ts is not None else time.time()
    candidates = sorted(eligible_campaigns(db, req, now), key=lambda c: c["bid_cpm"], reverse=True)

    while candidates:
        winner = candidates[0]
        if in_holdout(req.user_id, winner["advertiser_id"], winner["holdout_pct"]):
            db.execute(
                "INSERT INTO events (ts, type, user_id, campaign_id, advertiser_id) VALUES (?, 'eligible_control', ?, ?, ?)",
                (now, req.user_id, winner["id"], winner["advertiser_id"]),
            )
            candidates = [c for c in candidates if c["advertiser_id"] != winner["advertiser_id"]]
            continue

        runner_up = candidates[1]["bid_cpm"] if len(candidates) > 1 else FLOOR_CPM
        clearing_cpm = min(winner["bid_cpm"], max(runner_up, FLOOR_CPM) + 0.01)
        price = round(clearing_cpm / 1000, 6)

        db.execute(
            "INSERT INTO events (ts, type, user_id, campaign_id, advertiser_id, price) VALUES (?, 'impression', ?, ?, ?, ?)",
            (now, req.user_id, winner["id"], winner["advertiser_id"], price),
        )
        db.execute("UPDATE campaigns SET spend = spend + ? WHERE id = ?", (price, winner["id"]))
        return {
            "campaign_id": winner["id"],
            "advertiser_id": winner["advertiser_id"],
            "creative": winner["creative"],
            "landing_url": winner["landing_url"],
            "clearing_cpm": round(clearing_cpm, 4),
        }
    return None
