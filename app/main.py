"""AdLens API: serve ads, track events (pixels), build audiences, and report measurement."""
import base64
import html
import os
import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from . import measurement
from .ad_server import AdRequest, serve
from .db import DB

# 1x1 transparent GIF, the classic tracking pixel
PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


class CampaignIn(BaseModel):
    advertiser_id: str
    name: str
    creative: str
    landing_url: str
    bid_cpm: float = Field(gt=0)
    budget: float = Field(gt=0)
    geos: list[str] | None = None
    devices: list[str] | None = None
    interests: list[str] | None = None
    segment_id: int | None = None
    freq_cap: int = Field(3, ge=1)
    holdout_pct: int = Field(10, ge=0, le=50)


class SegmentIn(BaseModel):
    name: str
    advertiser_id: str
    event_type: str = Field(pattern="^(impression|click|conversion)$")
    lookback_days: float = 30


def create_app(db_path: str | None = None) -> FastAPI:
    db = DB(db_path or os.environ.get("ADLENS_DB", ":memory:"))
    app = FastAPI(title="AdLens", description="A mini ad server with first-party audiences and measurement.")
    app.state.db = db

    def join(xs):
        return ",".join(xs) if xs else None

    @app.post("/campaigns")
    def create_campaign(c: CampaignIn):
        cur = db.execute(
            """INSERT INTO campaigns (advertiser_id, name, creative, landing_url, bid_cpm, budget,
               geos, devices, interests, segment_id, freq_cap, holdout_pct)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (c.advertiser_id, c.name, c.creative, c.landing_url, c.bid_cpm, c.budget,
             join(c.geos), join(c.devices), join(c.interests), c.segment_id, c.freq_cap, c.holdout_pct),
        )
        return {"id": cur.lastrowid}

    @app.get("/campaigns")
    def list_campaigns():
        return db.query("SELECT * FROM campaigns")

    @app.post("/segments")
    def create_segment(s: SegmentIn):
        cur = db.execute(
            "INSERT INTO segments (name, advertiser_id, event_type, lookback_days) VALUES (?,?,?,?)",
            (s.name, s.advertiser_id, s.event_type, s.lookback_days),
        )
        return {"id": cur.lastrowid}

    @app.get("/ad")
    def get_ad(user_id: str, geo: str | None = None, device: str | None = None,
               interests: str | None = None, ts: float | None = None):
        ad = serve(db, AdRequest(user_id, geo, device,
                                 tuple(i.strip() for i in interests.split(",")) if interests else (), ts))
        if not ad:
            return Response(status_code=204)  # no fill
        ad["click_url"] = f"/track/click?user_id={user_id}&campaign_id={ad['campaign_id']}"
        return ad

    @app.get("/track/click")
    def track_click(user_id: str, campaign_id: int, ts: float | None = None):
        camp = db.one("SELECT advertiser_id, landing_url FROM campaigns WHERE id = ?", (campaign_id,))
        if not camp:
            raise HTTPException(404, "campaign not found")
        db.execute(
            "INSERT INTO events (ts, type, user_id, campaign_id, advertiser_id) VALUES (?, 'click', ?, ?, ?)",
            (ts or time.time(), user_id, campaign_id, camp["advertiser_id"]),
        )
        return {"redirect": camp["landing_url"]}

    @app.get("/track/conversion.gif")
    def track_conversion(user_id: str, advertiser_id: str, value: float = Query(0, ge=0),
                         ts: float | None = None):
        """Advertiser's site fires this pixel on purchase/sign-up (web analytics style)."""
        db.execute(
            "INSERT INTO events (ts, type, user_id, advertiser_id, value) VALUES (?, 'conversion', ?, ?, ?)",
            (ts or time.time(), user_id, advertiser_id, value),
        )
        return Response(content=PIXEL, media_type="image/gif")

    @app.get("/report/campaigns")
    def report_campaigns():
        return measurement.campaign_metrics(db)

    @app.get("/report/attribution")
    def report_attribution(model: str = "last_touch", lookback_days: float = 7):
        try:
            return measurement.attribute(db, model, lookback_days)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/report/lift/{advertiser_id}")
    def report_lift(advertiser_id: str, window_days: float = 7):
        try:
            return measurement.lift(db, advertiser_id, window_days)
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        return render_dashboard(db)

    return app


def render_dashboard(db: DB) -> str:
    e = html.escape
    camps = measurement.campaign_metrics(db)
    models = {m: measurement.attribute(db, m) for m in ("last_touch", "first_touch", "linear")}

    def table(headers, rows):
        head = "".join(f"<th>{e(h)}</th>" for h in headers)
        body = "".join("<tr>" + "".join(f"<td>{e(str(v))}</td>" for v in r) + "</tr>" for r in rows)
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    delivery = table(
        ["Campaign", "Advertiser", "Impressions", "Clicks", "CTR", "Spend ($)", "eCPM ($)", "Conv. (last touch)", "CPA ($)", "ROAS"],
        [[c["name"], c["advertiser_id"], c["impressions"], c["clicks"], f"{c['ctr']:.2%}", c["spend"],
          c["ecpm"], c["conversions"], c["cpa"] or "-", c["roas"] or "-"] for c in camps],
    )
    attribution = table(
        ["Campaign", "Last touch", "First touch", "Linear"],
        [[c["name"]] + [round(models[m].get(c["id"], {}).get("conversions", 0), 1)
                         for m in ("last_touch", "first_touch", "linear")] for c in camps],
    )
    lifts = []
    for adv in sorted({c["advertiser_id"] for c in camps}):
        r = measurement.lift(db, adv)
        if "note" in r:
            lifts.append([adv, r["treatment_users"], "-", r["control_users"], "-", "-", "-", "-", r["note"]])
            continue
        ci = r["relative_lift_95ci"]
        lifts.append([adv, r["treatment_users"], f"{r['treatment_cvr']:.2%}", r["control_users"],
                      f"{r['control_cvr']:.2%}",
                      f"{r['relative_lift']:+.0%}" if r["relative_lift"] is not None else "-",
                      f"{ci[0]:+.0%} to {ci[1]:+.0%}" if ci else "-",
                      r["p_value"], "yes" if r["significant_at_95"] else "no"])
    lift_table = table(["Advertiser", "Exposed users", "Exposed CVR", "Holdout users", "Holdout CVR",
                        "Lift", "95% CI", "p-value", "Significant"], lifts)

    return f"""<!doctype html><html><head><meta charset="utf-8"><title>AdLens dashboard</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;color:#1a1a1a;background:#fafafa}}
h1{{margin-bottom:.2rem}} p.sub{{color:#666;margin-top:0}}
table{{border-collapse:collapse;margin:1rem 0 2rem;background:#fff;font-size:14px}}
th,td{{border:1px solid #ddd;padding:6px 10px;text-align:right}} th{{background:#f0f0f0}}
td:first-child,th:first-child{{text-align:left}}
</style></head><body>
<h1>AdLens</h1><p class="sub">Delivery, attribution, and incrementality for every campaign.</p>
<h2>Delivery</h2>{delivery}
<h2>Attribution (7-day lookback): who gets credit?</h2>{attribution}
<h2>Incrementality: did the ads cause conversions?</h2>{lift_table}
</body></html>"""


app = create_app()
