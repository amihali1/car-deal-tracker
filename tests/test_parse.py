from email.message import EmailMessage

from tracker import value
from tracker.parse import detect_source, parse_chunk, parse_email_bytes, parse_text
from tracker.profile import profile_for


def test_chunk_fields(cfg, profile):
    f = parse_chunk(
        "2025 Nissan Frontier PRO-4X Crew Cab 4x4. 14,695 mi. $38,699 was $39,499. "
        "Example Nissan of Marietta, Marietta, GA. Tow Package, Premium Package. No accidents. "
        "VIN 1N6ED1EK0SN000001. Price includes dealer fees.", profile)
    assert f["year"] == 2025 and f["trim_ok"] and f["body"] == "crew"
    assert f["price"] == 38699
    assert f["miles"] == 14695
    assert f["vin"] == "1N6ED1EK0SN000001"
    assert f["city"] == "Marietta, GA"
    assert f["features"] == {"tow": True, "premium": True}
    assert f["accident"] is False and f["fees_included"]


def test_skips_was_price_and_monthly_and_distance(profile):
    f = parse_chunk("2025 Nissan Frontier PRO-4X, 25 mi away, est. $612/mo, was $39,999 now $37,250, 8.2k miles", profile)
    assert f["price"] == 37250
    assert f["miles"] == 8200


def test_wrong_body_and_missing_required_feature(cfg, profile):
    f = parse_chunk("2025 Nissan Frontier PRO-4X King Cab, no tow package, $33,000, 5,000 miles", profile)
    assert f["body"] == "king" and f["features"]["tow"] is False
    ok, reasons = value.matches(f, cfg)
    assert not ok and reasons == ["King Cab"]
    f["body"] = "crew"
    ok, reasons = value.matches(f, cfg)
    assert not ok and reasons == ["no Tow"]


def test_multi_listing_text_splits_by_heading(profile):
    text = (
        "New matches for your saved search\n"
        "2025 Nissan Frontier PRO-4X Crew Cab\n$36,590 · 6,412 miles\nhttps://www.carvana.com/vehicle/4718284\n"
        "2026 Nissan Frontier PRO-4X Crew Cab\n$39,100 · 1,200 miles\nhttps://www.carvana.com/vehicle/4800000\n"
    )
    ls = parse_text(text, profile, "carvana")
    assert [(l["year"], l["price"], l["miles"], l["listing_id"]) for l in ls] == [
        (2025, 36590, 6412, "4718284"),
        (2026, 39100, 1200, "4800000"),
    ]
    assert all(l["fees_included"] and l["dealer"] == "Carvana" for l in ls)


def test_facebook_html_email(cfg, profile):
    msg = EmailMessage()
    msg["From"] = "Facebook <notification@facebookmail.com>"
    msg["Subject"] = "New listings for nissan frontier pro-4x"
    msg["Message-ID"] = "<abc@facebookmail.com>"
    msg.set_content("fallback")
    msg.add_alternative(
        "<html><head><style>.x{}</style></head><body>"
        "<table><tr><td><a href='https://www.facebook.com/marketplace/item/1234567890/?ref=email'>"
        "2025 Nissan Frontier Crew Cab PRO-4X</a></td></tr>"
        "<tr><td>$35,500 · 18K miles</td></tr><tr><td>Decatur, GA</td></tr>"
        "<tr><td><a href='https://www.facebook.com/marketplace/item/2222222222/'>2019 Nissan Frontier SV</a></td></tr>"
        "<tr><td>$18,000 · 80K miles</td></tr><tr><td>Dalton, GA</td></tr></table></body></html>",
        subtype="html",
    )
    meta, ls = parse_email_bytes(msg.as_bytes(), profile)
    assert meta["source"] == "facebook"
    assert len(ls) == 2
    a, b = ls
    assert (a["year"], a["price"], a["miles"], a["city"], a["listing_id"]) == (2025, 35500, 18000, "Decatur, GA", "1234567890")
    assert a["trim_ok"] and not b["trim_ok"]
    ok, reasons = value.matches(a, cfg)
    assert ok and "Tow unknown" in reasons


def test_detect_source():
    assert detect_source("Cars.com <alerts@email.cars.com>") == "cars.com"
    assert detect_source("CarGurus <deals@mail.cargurus.com>") == "cargurus"
    assert detect_source("someone@example.com") == "manual"


def test_search_radius_is_not_odometer(profile):
    # Real Cars.com "Your Search is Saved!" wording.
    f = parse_chunk("We've saved your search • Used 2025 Nissan Frontier PRO-4X • No max price • 200 miles from 30303", profile)
    assert f["miles"] is None and f["price"] is None
    f = parse_chunk("2025 Nissan Frontier PRO-4X, $36,000, 12,400 miles, within 50 miles of you", profile)
    assert f["miles"] == 12400


def test_carscom_saved_car_real_wording(profile):
    # Real Cars.com "we saved your car!" body, incl. footer that used to become the dealer.
    text = ("You'll now get updates on your saved 2026 Nissan Frontier. "
            "Bob, we saved your car. We'll notify you when there are updates for your saved car. "
            "2026 Nissan Frontier PRO-4X $37,998 13,133 mi. CarMax Naperville 3320 Odyssey Court "
            "Naperville, IL 60563 | (843) 256-4761 View details Find your next car "
            "Terms and Conditions or Privacy Statement. New Cars Used Cars")
    ls = [l for l in parse_text(text, profile, "cars.com") if l["price"]]
    assert len(ls) == 1
    l = ls[0]
    assert (l["year"], l["price"], l["miles"]) == (2026, 37998, 13133)
    assert l["dealer"] == "CarMax Naperville"
    assert l["city"] == "Naperville, IL"


def test_cargurus_mileage_label_and_radius_footer(profile):
    # Real CarGurus layout: odometer is "Mileage: N" (no "mi"), and the last vehicle's chunk
    # runs into the footer's "Search radius: 200 miles".
    f = parse_chunk(
        "2025 Nissan Frontier PRO-4X Crew Cab 4WD $35,950 $36,950 Includes dealer fees | $2,921 Below "
        "CarGurus Instant Market Value of $37,822 Mileage: 8,265 | Franklin, TN View details View all listings "
        "Your search criteria Zip code: 30303 Search radius: 200 miles Years: 2025 -", profile)
    assert (f["price"], f["miles"], f["city"]) == (35950, 8265, "Franklin, TN")
    assert f["fees_included"]


def test_radius_with_colon_is_not_odometer(profile):
    f = parse_chunk("2026 Nissan Frontier PRO-4X $36,000 radius: 200 miles", profile)
    assert f["miles"] is None


def test_price_above_title_layout_and_similar_trim(profile):
    # CarGurus "just for you": each price sits above its title. Splitting at titles alone gave every
    # car the next one's price, and a PRO-X's price landed on a PRO-4X.
    text = ("We noticed you were checking this car out. $35,298 Includes dealer fees "
            "2025 Nissan Frontier PRO-4X Crew Cab 4WD Mileage: 15,700 miles Location: Lexington, NC View this vehicle "
            "We think you'll like these, too $38,734 Includes dealer fees 2025 Nissan Frontier PRO-4X Crew Cab 4WD "
            "Kernersville, NC $30,648 Includes dealer fees 2025 Nissan Frontier PRO-X Crew Cab RWD Statesville, NC")
    ls = parse_text(text, profile, "cargurus")
    assert [(l["price"], l["miles"], l["trim_ok"]) for l in ls] == [
        (35298, 15700, True), (38734, None, True), (30648, None, False)]
    assert ls[1]["city"] == "Kernersville, NC"


def test_title_trim_beats_text_elsewhere_in_chunk(profile):
    f = parse_chunk("2025 Nissan Frontier PRO-X Crew Cab RWD $30,648. Similar to your PRO-4X search", profile)
    assert f["trim_ok"] is False
    assert parse_chunk("2025 Nissan Frontier Crew Cab PRO-4X $35,500", profile)["trim_ok"] is True


# --- a different car, to prove nothing is Frontier-specific ---------------------------------

def test_other_vehicle_profile(crv):
    p = profile_for(crv)
    text = ("Price drop! 2024 Honda CR-V Hybrid Sport Touring AWD $34,250 18,400 mi Moonroof, leather "
            "Smyrna Honda, Smyrna, GA VIN: 7FARS6H92RE000123\n"
            "2023 Honda CR-V EX-L FWD $27,900 22,000 miles Roswell, GA\n"
            "2024 Honda CR-V Hybrid Sport-L $33,100 9,800 miles Athens, GA")
    a, b, c = parse_text(text, p, "cars.com")
    assert (a["year"], a["price"], a["miles"], a["trim_ok"], a["vin"]) == (2024, 34250, 18400, True, "7FARS6H92RE000123")
    assert a["features"] == {"awd": True, "sunroof": True} and a["dealer"] == "Smyrna Honda"
    assert b["trim_ok"] is False and b["features"]["awd"] is False
    assert c["trim_ok"] and c["features"]["awd"] is None

    ok, reasons = value.matches(a, crv)
    assert ok and "body unknown" not in reasons  # no body styles configured -> not asked about
    ok, reasons = value.matches(c, crv)
    assert ok and "AWD unknown" in reasons
    # $800 moonroof bonus, AWD is a must-have so it has no dollar value
    assert value.fair_value(a, crv) == 33500 - round((18400 - 15000) * 0.15) + 800


def test_feature_patterns_match_whole_words_only(crv):
    p = profile_for(crv)
    assert p.features_of("2024 Honda CR-V Sport Touring AWD")["awd"] is True
    assert p.features_of("2024 Honda CR-V Sport Touring, Hawdon Motors")["awd"] is None
    assert p.features_of("2024 Honda CR-V Sport Touring FWD")["awd"] is False


def test_unlabelled_17_chars_are_not_a_vin_without_prefix(crv):
    p = profile_for(crv)
    f = parse_chunk("2024 Honda CR-V Sport Touring $34,000 ref ABCDEFGH123456789", p)
    assert f["vin"] is None


def test_no_trims_configured_accepts_any_trim(crv):
    crv["vehicle"]["trims"] = []
    p = profile_for(crv)
    assert parse_chunk("2024 Honda CR-V LX $25,000 30,000 miles", p)["trim_ok"] is True


def test_make_is_optional_in_heading_but_model_is_not(profile):
    assert parse_chunk("Used 2026 Frontier PRO-4X $36,000", profile)["year"] == 2026
    # Same trim name on a different model (Titan PRO-4X) must not count as the wanted car.
    (titan,) = parse_text("2026 Nissan Titan PRO-4X $52,000", profile)
    assert titan["trim_ok"] is False
