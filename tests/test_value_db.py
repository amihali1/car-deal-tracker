import pytest

from tracker import db, value
from tracker.config import ConfigError, validate

from .conftest import EXAMPLE, make_cfg


def test_score_math(cfg):
    # fair 37000 + (15000-14695)*0.15 + 1500 premium = 38546; all-in 38699 + 0 fees + 21*0.5 -> 38710
    l = {"year": 2025, "miles": 14695, "price": 38699, "features": {"premium": True, "tow": True},
         "fees_included": True, "distance": 21}
    assert value.fair_value(l, cfg) == 38546
    assert value.all_in(l, cfg) == 38710
    assert value.score(l, cfg) == -164


def test_base_price_extrapolates_missing_years(cfg):
    fv = cfg["fair_value"]
    assert value.base_price(2025, fv) == 37000
    assert value.base_price(2028, fv) == 42000 + 2500  # from nearest listed year (2027)
    assert value.base_price(2022, fv) == 34000 - 2 * 2500
    assert value.base_price(None, fv) == 42000


def test_distance_lookup():
    d = {"atlanta": 5, "east point": 8, "online": 0}
    assert value.distance_for("East Point, GA", d) == 8
    assert value.distance_for("Atlanta, GA", d) == 5
    assert value.distance_for("Online", d) == 0
    assert value.distance_for("Denver, CO", d) is None


def test_distance_falls_back_to_state():
    d, s = {"atlanta": 5}, {"NC": 260}
    assert value.distance_for("Kernersville, NC", d, s) == 260
    assert value.distance_for("Atlanta, GA", d, s) == 5
    assert value.distance_for("Denver, CO", d, s) is None


def test_upsert_events_and_feature_merge(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    base = {"source": "carvana", "listing_id": "1", "year": 2025, "miles": 6000, "price": 36590,
            "features": {"tow": True, "premium": None}}
    key, ev, _ = db.upsert(conn, dict(base), today="2026-09-28")
    assert (key, ev) == ("carvana:1", "new")

    # Terse follow-up email: lower price, tow unknown, premium now known -> keeps tow, learns premium.
    key, ev, delta = db.upsert(conn, {**base, "price": 35990, "features": {"tow": None, "premium": True}},
                               today="2026-09-29")
    assert (ev, delta) == ("drop", -600)
    (row,) = db.all_listings(conn)
    assert row["features"] == {"tow": True, "premium": True} and row["price"] == 35990
    assert [h["price"] for h in db.history(conn, key)] == [36590, 35990]

    db.set_status(conn, key, "archived")
    _, ev, _ = db.upsert(conn, {**base, "price": 35990}, today="2026-10-01")
    assert ev == "relisted"


def test_max_price_and_max_year(cfg):
    cfg["criteria"].update(max_price=40000, max_year=2026)
    good = {"year": 2025, "trim_ok": True, "price": 39000, "miles": 1000, "body": "crew", "features": {"tow": True}}
    assert value.matches(good, cfg)[0]
    assert value.matches({**good, "price": 41000}, cfg) == (False, ["$41,000"])
    assert value.matches({**good, "year": 2027}, cfg) == (False, ["year 2027"])


def test_validate_reports_every_problem():
    bad = make_cfg(EXAMPLE)
    del bad["vehicle"]["model"]
    bad["features"]["tow"]["yes"] = []
    bad["body"]["want"] = ["quad"]
    with pytest.raises(ConfigError) as e:
        validate(bad)
    msg = str(e.value)
    assert "vehicle.model" in msg and "feature 'tow'" in msg and "'quad'" in msg
