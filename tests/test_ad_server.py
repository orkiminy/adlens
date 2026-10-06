from app.ad_server import DAY, AdRequest, in_holdout, serve
from app.db import DB


def add_campaign(db, **kw):
    c = dict(advertiser_id="adv", name="c", creative="hi", landing_url="https://x", bid_cpm=10.0,
             budget=100.0, geos=None, devices=None, interests=None, segment_id=None, freq_cap=3, holdout_pct=0)
    c.update(kw)
    cur = db.execute(
        """INSERT INTO campaigns (advertiser_id, name, creative, landing_url, bid_cpm, budget,
           geos, devices, interests, segment_id, freq_cap, holdout_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        tuple(c.values()),
    )
    return cur.lastrowid


def test_highest_bid_wins_and_pays_second_price():
    db = DB()
    add_campaign(db, advertiser_id="a", bid_cpm=10.0)
    hi = add_campaign(db, advertiser_id="b", bid_cpm=20.0)
    ad = serve(db, AdRequest("u1", ts=0))
    assert ad["campaign_id"] == hi
    assert ad["clearing_cpm"] == 10.01  # pays runner-up + $0.01, not its own $20 bid


def test_targeting_filters_geo_device_interests():
    db = DB()
    add_campaign(db, geos="NY", devices="mobile", interests="running")
    assert serve(db, AdRequest("u1", geo="CA", device="mobile", interests=("running",), ts=0)) is None
    assert serve(db, AdRequest("u1", geo="NY", device="desktop", interests=("running",), ts=0)) is None
    assert serve(db, AdRequest("u1", geo="NY", device="mobile", interests=("cooking",), ts=0)) is None
    assert serve(db, AdRequest("u1", geo="NY", device="mobile", interests=("cooking", "running"), ts=0))


def test_frequency_cap_resets_after_24h():
    db = DB()
    add_campaign(db, freq_cap=2)
    assert serve(db, AdRequest("u1", ts=0)) and serve(db, AdRequest("u1", ts=10))
    assert serve(db, AdRequest("u1", ts=20)) is None
    assert serve(db, AdRequest("u1", ts=DAY + 21)) is not None


def test_budget_exhaustion_stops_delivery():
    db = DB()
    add_campaign(db, bid_cpm=1000.0, budget=0.012)  # pays the $5.01 floor -> ~$0.005 per impression
    served = sum(serve(db, AdRequest(f"u{i}", ts=0)) is not None for i in range(20))
    assert 0 < served < 20


def test_holdout_is_deterministic_and_logs_ghost_ads():
    db = DB()
    add_campaign(db, holdout_pct=50)
    users = [f"u{i}" for i in range(200)]
    for u in users:
        assert in_holdout(u, "adv", 50) == in_holdout(u, "adv", 50)
        serve(db, AdRequest(u, ts=0))
    controls = db.one("SELECT COUNT(*) AS n FROM events WHERE type = 'eligible_control'")["n"]
    imps = db.one("SELECT COUNT(*) AS n FROM events WHERE type = 'impression'")["n"]
    assert controls + imps == 200
    assert 60 < controls < 140  # roughly half


def test_segment_targeting_requires_prior_event():
    db = DB()
    seg = db.execute("INSERT INTO segments (name, advertiser_id, event_type, lookback_days) VALUES ('clickers','adv','click',7)").lastrowid
    cid = add_campaign(db, segment_id=seg)
    assert serve(db, AdRequest("u1", ts=100)) is None
    db.execute("INSERT INTO events (ts, type, user_id, campaign_id, advertiser_id) VALUES (50,'click','u1',?, 'adv')", (cid,))
    assert serve(db, AdRequest("u1", ts=100))["campaign_id"] == cid
    assert serve(db, AdRequest("u1", ts=50 + 8 * DAY)) is None  # outside the 7-day lookback
