"""Measurement: delivery metrics, multi-touch attribution, and incrementality (lift) tests."""
import math
from collections import defaultdict

from .db import DB

DAY = 86_400


def campaign_metrics(db: DB) -> list[dict]:
    """Impressions, clicks, CTR, spend, eCPM, and last-touch conversions per campaign."""
    rows = db.query(
        """
        SELECT c.id, c.name, c.advertiser_id, c.budget, c.spend AS spend,
               SUM(e.type = 'impression') AS impressions,
               SUM(e.type = 'click')      AS clicks
        FROM campaigns c LEFT JOIN events e ON e.campaign_id = c.id
        GROUP BY c.id ORDER BY c.id
        """
    )
    last_touch = attribute(db, model="last_touch")
    for r in rows:
        imps, clicks = r["impressions"] or 0, r["clicks"] or 0
        conv = last_touch.get(r["id"], {"conversions": 0.0, "value": 0.0})
        spend = r["spend"]
        r.update(
            spend=round(spend, 2),
            impressions=imps,
            clicks=clicks,
            ctr=round(clicks / imps, 4) if imps else 0.0,
            ecpm=round(spend / imps * 1000, 2) if imps else 0.0,
            conversions=round(conv["conversions"], 2),
            cpa=round(spend / conv["conversions"], 2) if conv["conversions"] else None,
            roas=round(conv["value"] / spend, 2) if spend else None,
        )
    return rows


def _touchpoints(db: DB, lookback_days: float):
    """Yield (conversion, [touchpoints before it within the lookback window])."""
    conversions = db.query("SELECT * FROM events WHERE type = 'conversion' ORDER BY ts")
    for conv in conversions:
        touches = db.query(
            """
            SELECT campaign_id, type, ts FROM events
            WHERE user_id = ? AND advertiser_id = ? AND type IN ('impression','click')
              AND ts <= ? AND ts >= ? ORDER BY ts
            """,
            (conv["user_id"], conv["advertiser_id"], conv["ts"], conv["ts"] - lookback_days * DAY),
        )
        yield conv, touches


def attribute(db: DB, model: str = "last_touch", lookback_days: float = 7) -> dict[int, dict]:
    """Split each conversion's credit across campaigns.

    last_touch : last click wins; if no click, last impression (standard industry default)
    first_touch: first touchpoint gets full credit
    linear     : credit split evenly across all touchpoints
    """
    credit = defaultdict(lambda: {"conversions": 0.0, "value": 0.0})
    for conv, touches in _touchpoints(db, lookback_days):
        if not touches:
            continue  # organic conversion, no ad touched it
        if model == "last_touch":
            clicks = [t for t in touches if t["type"] == "click"]
            shares = {(clicks or touches)[-1]["campaign_id"]: 1.0}
        elif model == "first_touch":
            shares = {touches[0]["campaign_id"]: 1.0}
        elif model == "linear":
            shares = defaultdict(float)
            for t in touches:
                shares[t["campaign_id"]] += 1 / len(touches)
        else:
            raise ValueError(f"unknown model: {model}")
        for cid, share in shares.items():
            credit[cid]["conversions"] += share
            credit[cid]["value"] += share * conv["value"]
    return dict(credit)


def lift(db: DB, advertiser_id: str, window_days: float = 7) -> dict:
    """Incrementality: conversion rate of exposed users vs. holdout ("ghost ad") users.

    Measured per advertiser, matching the advertiser-level holdout in the ad server.
    Treatment = users who saw any of the advertiser's ads; control = holdout users
    who would have seen one. A user converts if they convert for the advertiser
    within WINDOW_DAYS after their first exposure / first eligibility.
    Reports a two-proportion z-test and a 95% confidence interval.
    """
    if not db.one("SELECT 1 FROM campaigns WHERE advertiser_id = ?", (advertiser_id,)):
        raise ValueError("advertiser not found")

    def group(event_type: str) -> tuple[int, int]:
        r = db.one(
            """
            WITH firsts AS (
                SELECT user_id, MIN(ts) AS t0 FROM events
                WHERE advertiser_id = ? AND type = ? GROUP BY user_id
            )
            SELECT COUNT(*) AS n,
                   SUM(EXISTS (SELECT 1 FROM events c WHERE c.type = 'conversion'
                               AND c.user_id = f.user_id AND c.advertiser_id = ?
                               AND c.ts > f.t0 AND c.ts <= f.t0 + ?)) AS conv
            FROM firsts f
            """,
            (advertiser_id, event_type, advertiser_id, window_days * DAY),
        )
        return r["n"] or 0, r["conv"] or 0

    n_t, c_t = group("impression")
    n_c, c_c = group("eligible_control")
    base = {"advertiser_id": advertiser_id, "treatment_users": n_t, "control_users": n_c}
    if not n_t or not n_c:
        return {**base, "note": "not enough users in both groups yet"}

    p_t, p_c = c_t / n_t, c_c / n_c
    diff = p_t - p_c
    pooled = (c_t + c_c) / (n_t + n_c)
    se_pooled = math.sqrt(pooled * (1 - pooled) * (1 / n_t + 1 / n_c)) if 0 < pooled < 1 else 0
    z = diff / se_pooled if se_pooled else 0.0
    p_value = math.erfc(abs(z) / math.sqrt(2))  # two-sided
    se = math.sqrt(p_t * (1 - p_t) / n_t + p_c * (1 - p_c) / n_c)
    lo, hi = diff - 1.96 * se, diff + 1.96 * se
    return {
        **base,
        "treatment_cvr": round(p_t, 4), "control_cvr": round(p_c, 4),
        "relative_lift": round(diff / p_c, 3) if p_c else None,
        "relative_lift_95ci": [round(lo / p_c, 3), round(hi / p_c, 3)] if p_c else None,
        "incremental_conversions": round(diff * n_t, 1),
        "z": round(z, 2), "p_value": round(p_value, 4),
        "significant_at_95": p_value < 0.05,
    }
