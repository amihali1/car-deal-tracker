"""New cars: condition + MSRP parsing, their own fair value, opt-in via criteria.conditions."""
import copy
from email.message import EmailMessage

import pytest

from tracker import db, value
from tracker.config import ConfigError, validate
from tracker.parse import parse_chunk, parse_email_bytes, parse_text, search_condition

from .conftest import EXAMPLE, make_cfg

NEW_FV = {"base": {"2025": 40000, "2026": 41500}, "msrp_discount": 0.10}


@pytest.fixture
def both(tmp_path):
    raw = copy.deepcopy(EXAMPLE)
    raw["criteria"]["conditions"] = ["used", "new"]
    raw["fair_value"]["new"] = NEW_FV
    return make_cfg(raw, tmp_path)


def test_condition_word_before_title_stays_with_its_car(profile):
    text = ("Newly Listed New 2026 Nissan Frontier PRO-4X Crew Cab 12 miles MSRP $46,555 $41,737 See This Car "
            "Reduced Price Used 2025 Nissan Frontier PRO-4X 9,120 miles $36,000 See This Car")
    a, b = parse_text(text, profile)
    assert (a["condition"], a["year"], a["price"], a["msrp"], a["miles"]) == ("new", 2026, 41737, 46555, 12)
    assert (b["condition"], b["year"], b["price"], b["msrp"]) == ("used", 2025, 36000, None)


def test_msrp_after_the_number_is_not_the_price(profile):
    f = parse_chunk("2026 Nissan Frontier PRO-4X $45,995 MSRP $39,995 Example Nissan, Dalton, GA", profile)
    assert (f["price"], f["msrp"], f["condition"]) == (39995, 45995, "new")


def test_msrp_alone_is_the_asking_price(profile):
    f = parse_chunk("New 2026 Nissan Frontier PRO-4X MSRP: $47,120", profile)
    assert (f["price"], f["msrp"]) == (47120, 47120)


def test_condition_label_and_original_msrp(profile):
    assert parse_chunk("2026 Nissan Frontier PRO-4X Condition: New $42,000", profile)["condition"] == "new"
    f = parse_chunk("2025 Nissan Frontier PRO-4X 14,000 mi $36,500 Original MSRP $46,000", profile)
    assert f["msrp"] is None and f["condition"] is None and f["price"] == 36500
    assert parse_chunk("Certified Pre-Owned 2025 Nissan Frontier PRO-4X $36,500", profile)["condition"] == "used"


def test_new_cars_are_opt_in(cfg, both):
    l = {"year": 2026, "trim_ok": True, "condition": "new", "price": 41000, "features": {"tow": True}, "body": "crew"}
    assert value.matches(l, cfg) == (False, ["new"])
    assert value.matches({**l, "condition": None}, cfg)[0]  # unknown condition counts as used
    ok, reasons = value.matches({**l, "distance": 30}, both)
    assert ok and reasons == []  # no "miles unknown" for a new car


def test_new_fair_value(both):
    l = {"year": 2026, "condition": "new", "price": 40000, "features": {"tow": True, "premium": True}}
    assert value.fair_value(l, both) == 41500 + 1500  # table + package values
    assert value.fair_value({**l, "msrp": 46555}, both) == 41900  # MSRP already prices the packages
    assert value.fair_value({**l, "year": 2027}, both) == 41500 + 2500 + 1500  # extrapolated by per_year
    used = {**l, "condition": "used", "miles": 15000}
    assert value.fair_value(used, both) == 39500 + 1500


def test_new_car_skips_unknown_miles_penalty(both):
    l = {"year": 2026, "condition": "new", "price": 40000, "features": {"tow": True}, "distance": 0,
         "fees_included": True}
    assert value.rank(l, both) == (1500, [])
    assert value.rank({**l, "condition": "used"}, both)[1] == ["miles?"]


def test_config_needs_new_fair_value_when_new_wanted():
    raw = copy.deepcopy(EXAMPLE)
    raw["criteria"]["conditions"] = ["used", "new"]
    raw["fair_value"].pop("new", None)
    with pytest.raises(ConfigError, match="fair_value.new.base"):
        validate(raw)
    raw["criteria"]["conditions"] = ["certified"]
    with pytest.raises(ConfigError, match="conditions"):
        validate(raw)


def test_new_stock_without_odometer_keeps_one_row_per_price(both):
    conn = db.connect(both["db_path"])
    base = {"source": "cars.com", "year": 2026, "dealer": "Example Nissan", "condition": "new", "features": {}}
    k1, e1, _ = db.upsert(conn, {**base, "price": 41737})
    k2, e2, _ = db.upsert(conn, {**base, "price": 43210})
    assert k1 != k2 and e1 == e2 == "new"
    k3, _, _ = db.upsert(conn, {**base, "price": 41000, "miles": 5})
    k4, _, _ = db.upsert(conn, {**base, "price": 42000, "miles": 12})
    assert k3 != k4  # exact odometer under 1,000 mi, not one "0" bucket
    row = conn.execute("SELECT condition, msrp FROM listings WHERE key = ?", (k1,)).fetchone()
    assert row["condition"] == "new" and row["msrp"] is None


def test_saved_search_condition_fills_unlabelled_listings(profile):
    msg = EmailMessage()
    msg["From"] = "Cars.com <email@emalert.cars.com>"
    msg["Subject"] = 'Inventory Alert: 2+ new listings matching your search, "New Nissan Frontier for Sale"'
    msg["Message-ID"] = "<n1@x>"
    msg.set_content("2026 Nissan Frontier PRO-4X $41,995 Dalton, GA\n"
                    "Used 2025 Nissan Frontier PRO-4X 8,000 mi $36,000\n"
                    "New Cars Used Cars Sell Your Car")
    _, (a, b) = parse_email_bytes(msg.as_bytes(), profile)
    assert a["condition"] == "new" and b["condition"] == "used"
    assert search_condition("Shippable • 200 mi of 37404 • Used • Min year: 2025") == "used"
    assert search_condition("New Cars Used Cars Sell Your Car") is None
    assert search_condition('"New and Used Nissan Frontier for Sale"') is None
