"""Traffic simulator with known ground truth.

Creates synthetic users, runs them through the live API (ad requests, clicks,
conversion pixels) over simulated days, then checks whether the measured lift
recovers the TRUE effect we built in. That is the point of the project:
attribution says who gets credit; a holdout test says what the ads actually caused.

Usage:  python simulate.py [--users 2000] [--days 14] [--seed 7]
"""
import argparse
import json
import random

from fastapi.testclient import TestClient

from app.main import create_app

DAY = 86_400
GEOS = ["NY", "CA", "TX", "IL"]
DEVICES = ["mobile", "desktop"]
INTERESTS = ["running", "fitness", "travel", "cooking", "tech"]

# Ground truth per advertiser: daily baseline conversion probability and how
# much an ad exposure multiplies it (1.0 = ads do nothing).
TRUTH = {
    "stridewear": {"base": 0.010, "ad_multiplier": 1.6, "value": 80.0},
    "wanderly":   {"base": 0.008, "ad_multiplier": 1.0, "value": 300.0},  # ads have NO real effect
}
CTR = {True: 0.03, False: 0.008}  # click rate if the ad matches the user's interests or not


def setup(client: TestClient):
    camps = [
        dict(advertiser_id="stridewear", name="Stridewear - Running prospecting", creative="New trail shoes, 20% off",
             landing_url="https://example.com/stride", bid_cpm=30.0, budget=2000, interests=["running", "fitness"],
             holdout_pct=20),
        dict(advertiser_id="stridewear", name="Stridewear - Broad reach", creative="Gear up for spring",
             landing_url="https://example.com/stride-broad", bid_cpm=15.0, budget=1000, holdout_pct=20),
        dict(advertiser_id="wanderly", name="Wanderly - Travel deals", creative="Fly to Lisbon from $299",
             landing_url="https://example.com/wanderly", bid_cpm=25.0, budget=2000, interests=["travel"],
             holdout_pct=20),
    ]
    ids = [client.post("/campaigns", json=c).json()["id"] for c in camps]
    # First-party audience: people who clicked a Stridewear ad in the last 14 days -> retargeting
    seg = client.post("/segments", json=dict(name="Stridewear clickers", advertiser_id="stridewear",
                                              event_type="click", lookback_days=14)).json()["id"]
    ids.append(client.post("/campaigns", json=dict(
        advertiser_id="stridewear", name="Stridewear - Retargeting", creative="Still thinking about it?",
        landing_url="https://example.com/stride-rt", bid_cpm=40.0, budget=1000, segment_id=seg,
        freq_cap=2, holdout_pct=20)).json()["id"])
    return ids


def run(users: int, days: int, seed: int):
    rng = random.Random(seed)
    with TestClient(create_app(":memory:")) as client:  # one event loop for all requests (much faster)
        return _run(client, rng, users, days)


def _run(client: TestClient, rng: random.Random, users: int, days: int):
    setup(client)

    population = [dict(id=f"u{i}", geo=rng.choice(GEOS), device=rng.choice(DEVICES),
                       interests=rng.sample(INTERESTS, 2)) for i in range(users)]
    exposed = {}  # (user, advertiser) -> True once they saw any ad from that advertiser

    t0 = 1_767_225_600  # Jan 1, 2026
    for day in range(days):
        for u in population:
            for session in range(rng.randint(0, 3)):
                ts = t0 + day * DAY + rng.uniform(0, DAY)
                r = client.get("/ad", params=dict(user_id=u["id"], geo=u["geo"], device=u["device"],
                                                  interests=",".join(u["interests"]), ts=ts))
                if r.status_code == 204:
                    continue
                ad = r.json()
                exposed[(u["id"], ad["advertiser_id"])] = True
                relevant = ad["advertiser_id"] == "stridewear" and set(u["interests"]) & {"running", "fitness"} \
                    or ad["advertiser_id"] == "wanderly" and "travel" in u["interests"]
                if rng.random() < CTR[bool(relevant)]:
                    client.get("/track/click", params=dict(user_id=u["id"], campaign_id=ad["campaign_id"], ts=ts + 5))
            # End of day: does the user convert with each advertiser? (ground truth model)
            for adv, truth in TRUTH.items():
                p = truth["base"] * (truth["ad_multiplier"] if exposed.get((u["id"], adv)) else 1.0)
                if rng.random() < p:
                    client.get("/track/conversion.gif", params=dict(
                        user_id=u["id"], advertiser_id=adv, value=truth["value"],
                        ts=t0 + day * DAY + DAY - 1))

    report = {
        "campaigns": client.get("/report/campaigns").json(),
        "attribution": {m: client.get("/report/attribution", params={"model": m}).json()
                        for m in ("last_touch", "first_touch", "linear")},
        "lift": [client.get(f"/report/lift/{adv}").json() for adv in TRUTH],
        "truth": {adv: f"ads multiply conversion rate by {t['ad_multiplier']}x" for adv, t in TRUTH.items()},
    }
    with open("dashboard.html", "w") as f:
        f.write(client.get("/dashboard").text)
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=2000)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rep = run(a.users, a.days, a.seed)
    print(json.dumps({"lift": rep["lift"], "truth": rep["truth"]}, indent=2))
    print("\nDashboard written to dashboard.html")
