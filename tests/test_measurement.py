import pytest
from fastapi.testclient import TestClient

from app import measurement
from app.db import DB
from app.main import create_app


def ev(db, ts, type_, user, campaign, adv="adv", value=0.0):
    db.execute("INSERT INTO events (ts, type, user_id, campaign_id, advertiser_id, value) VALUES (?,?,?,?,?,?)",
               (ts, type_, user, campaign, adv, value))


@pytest.fixture
def journey():
    """One user: sees campaign 1, clicks campaign 2, sees campaign 3, then converts for $90."""
    db = DB()
    for cid in (1, 2, 3):
        db.execute("INSERT INTO campaigns (id, advertiser_id, name, creative, landing_url, bid_cpm, budget) "
                   "VALUES (?, 'adv', 'c', 'x', 'y', 1, 1)", (cid,))
    ev(db, 10, "impression", "u", 1)
    ev(db, 20, "click", "u", 2)
    ev(db, 30, "impression", "u", 3)
    ev(db, 40, "conversion", "u", None, value=90.0)
    return db


def test_last_touch_prefers_last_click(journey):
    credit = measurement.attribute(journey, "last_touch")
    assert credit == {2: {"conversions": 1.0, "value": 90.0}}


def test_first_touch(journey):
    assert measurement.attribute(journey, "first_touch") == {1: {"conversions": 1.0, "value": 90.0}}


def test_linear_splits_evenly(journey):
    credit = measurement.attribute(journey, "linear")
    assert set(credit) == {1, 2, 3}
    assert sum(c["conversions"] for c in credit.values()) == pytest.approx(1.0)
    assert credit[1]["value"] == pytest.approx(30.0)


def test_lookback_window_excludes_old_touches(journey):
    ev(journey, 40 + 30 * 86_400, "conversion", "u", None)  # converts again a month later
    credit = measurement.attribute(journey, "last_touch", lookback_days=7)
    assert credit[2]["conversions"] == 1.0  # second conversion has no recent touch -> organic


def test_lift_detects_real_effect():
    db = DB()
    db.execute("INSERT INTO campaigns (advertiser_id, name, creative, landing_url, bid_cpm, budget) VALUES ('adv','c','x','y',1,1)")
    # 1000 exposed users convert at 20%, 1000 holdout users at 10%
    for i in range(1000):
        ev(db, 0, "impression", f"t{i}", 1)
        ev(db, 0, "eligible_control", f"c{i}", 1)
        if i % 5 == 0:
            ev(db, 100, "conversion", f"t{i}", None)
        if i % 10 == 0:
            ev(db, 100, "conversion", f"c{i}", None)
    r = measurement.lift(db, "adv")
    assert r["treatment_cvr"] == 0.2 and r["control_cvr"] == 0.1
    assert r["relative_lift"] == 1.0
    assert r["significant_at_95"]
    lo, hi = r["relative_lift_95ci"]
    assert lo < 1.0 < hi


def test_api_end_to_end_with_conversion_pixel():
    client = TestClient(create_app(":memory:"))
    client.post("/campaigns", json=dict(advertiser_id="shop", name="Spring sale", creative="20% off",
                                        landing_url="https://shop.example", bid_cpm=20, budget=50, holdout_pct=0))
    ad = client.get("/ad", params={"user_id": "u1", "ts": 0}).json()
    assert ad["creative"] == "20% off"
    assert client.get(ad["click_url"] + "&ts=5").json()["redirect"] == "https://shop.example"
    pixel = client.get("/track/conversion.gif", params={"user_id": "u1", "advertiser_id": "shop", "value": 40, "ts": 60})
    assert pixel.headers["content-type"] == "image/gif"
    report = client.get("/report/campaigns").json()[0]
    assert (report["impressions"], report["clicks"], report["conversions"]) == (1, 1, 1.0)
    assert report["roas"] > 1
    assert "AdLens" in client.get("/dashboard").text
