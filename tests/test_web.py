import json
import threading
import urllib.request
from http.server import HTTPServer

import pytest

from tracker import db, value, web


@pytest.fixture
def conn(cfg):
    return db.connect(cfg["db_path"])


def _car(**kw):
    base = {"source": "cars.com", "year": 2025, "miles": 12526, "price": 34000, "dealer": "Used Nissan",
            "city": "Morrow, GA", "trim_ok": True}
    return {**base, **kw}


def test_same_car_under_other_dealer_text_is_one_listing(conn):
    k1, ev1, _ = db.upsert(conn, _car(), today="2026-10-01")
    k2, ev2, delta = db.upsert(conn, _car(dealer=None, price=33500, source="cargurus"), today="2026-10-03")
    assert k1 == k2 and (ev1, ev2, delta) == ("new", "drop", -500)
    assert len(db.all_listings(conn)) == 1


def test_merge_duplicates_folds_history(conn):
    db.upsert(conn, _car(key="a"), today="2026-10-01")
    db.upsert(conn, _car(key="b", price=33000, features={"tow": True}), today="2026-10-04")
    assert db.merge_duplicates(conn) == [("a", "b")]
    (row,) = db.all_listings(conn)
    assert (row["key"], row["price"], row["features"], row["last_seen"]) == ("a", 33000, {"tow": True}, "2026-10-04")
    assert [h["price"] for h in db.history(conn, "a")] == [34000, 33000]


def test_passed_listing_stays_hidden_and_silent(conn):
    key, _, _ = db.upsert(conn, _car(), today="2026-10-01")
    db.update_fields(conn, key, {"status": "passed"})
    _, ev, _ = db.upsert(conn, _car(price=30000), today="2026-10-02")
    assert ev == "passed"
    assert db.all_listings(conn)[0]["status"] == "passed"


def test_expire_stale_skips_starred(conn):
    a, _, _ = db.upsert(conn, _car(), today="2026-09-01")
    b, _, _ = db.upsert(conn, _car(miles=20000), today="2026-09-01")
    db.update_fields(conn, b, {"starred": True})
    assert db.expire_stale(conn, 30, today="2026-10-08") == [a]


def test_update_fields_logs_price_and_seen(conn):
    key, _, _ = db.upsert(conn, _car(), today="2026-10-01")
    row = db.update_fields(conn, key, {"price": 33000, "features": {"tow": True}, "source": "hacked"}, today="2026-10-05")
    assert (row["price"], row["features"], row["source"], row["last_seen"]) == (33000, {"tow": True}, "cars.com", "2026-10-05")
    row = db.update_fields(conn, key, {"features": {"premium": True}}, today="2026-10-05")
    assert row["features"] == {"tow": True, "premium": True}
    row = db.update_fields(conn, key, {"seen": True}, today="2026-10-07")
    assert row["last_seen"] == "2026-10-07"


def test_rank_penalizes_unknowns(cfg):
    fv, r = cfg["fair_value"], cfg["ranking"]
    known = _car(features={"tow": True}, distance=120, body="crew")
    unknown = _car(features={"tow": None}, distance=None, body=None)
    s_known, f_known = value.rank(known, cfg)
    s_unknown, f_unknown = value.rank(unknown, cfg)
    assert f_known == [] and set(f_unknown) == {"Tow?", "distance?"}
    assert s_known - s_unknown == r["unknown_feature"] + round((r["unknown_distance"] - 120) * fv["trip_per_mile"])
    s_king, f_king = value.rank(_car(features={"tow": False}, distance=120, body="king"), cfg)
    assert f_king == ["no Tow", "King Cab"] and s_known - s_king == r["missing_feature"] + r["wrong_body"]


def test_http_api_roundtrip(cfg, conn):
    key, _, _ = db.upsert(conn, _car(), today="2026-10-01")
    httpd = HTTPServer(("127.0.0.1", 0), web.make_handler(cfg, conn, token=""))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    try:
        assert call(f"/api/listings/{key}", {"features": {"tow": True}, "price": "$33,100"}) == {"ok": True}
        added = call("/api/listings", {"text": "2026 Nissan Frontier PRO-4X Crew Cab $36,500 9,000 miles Atlanta, GA",
                                       "features": {"tow": True, "premium": None}})
        assert added["ok"] and added["event"] == "new"
        state = call("/api/state")
        rows = {l["key"]: l for l in state["listings"]}
        assert rows[key]["features"] == {"tow": True} and rows[key]["price"] == 33100
        assert rows[added["key"]]["features"] == {"tow": True, "premium": None} and rows[added["key"]]["body"] == "crew"
        ui = state["config"]
        assert ui["vehicle"]["label"] == "Frontier PRO-4X" and ui["features"]["tow"]["required"]
        assert ui["body"]["styles"]["crew"] == "Crew Cab" and "_profile" not in ui
        assert [l["rank"] for l in state["listings"]] == [1, 2]
        assert urllib.request.urlopen(base + "/").read().startswith(b"<!doctype html>")
    finally:
        httpd.shutdown()


def test_token_required_when_set(cfg, conn):
    httpd = HTTPServer(("127.0.0.1", 0), web.make_handler(cfg, conn, token="s3cret"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/api/state")
        assert e.value.code == 401
        req = urllib.request.Request(base + "/api/state", headers={"Cookie": "tracker_token=s3cret"})
        assert urllib.request.urlopen(req).status == 200
    finally:
        httpd.shutdown()


def test_flag_remove_and_report_log(conn):
    key, _, _ = db.upsert(conn, _car(email_id="<m1@cars.com>"), today="2026-10-01")
    rid = db.flag(conn, key, "Not a PRO-4X", "listing says PRO-X", today="2026-10-08")
    (rep,) = db.reports(conn, "open")
    assert rep["id"] == rid and rep["email_id"] == "<m1@cars.com>" and rep["snapshot"]["price"] == 34000
    # Later alerts update a flagged truck silently, and never bring it back.
    _, ev, _ = db.upsert(conn, _car(price=33000), today="2026-10-09")
    assert ev == "flagged"
    db.update_fields(conn, key, {"status": "removed"})
    _, ev, _ = db.upsert(conn, _car(price=32000), today="2026-10-10")
    assert ev == "removed" and db.all_listings(conn)[0]["status"] == "removed"
    assert db.reports(conn, "open")  # report survives removal for the parser fix
    with pytest.raises(ValueError):
        db.update_fields(conn, key, {"status": "bogus"})


def test_price_only_listing_keyed_by_price_and_matched(cfg, conn):
    from tracker import cli
    # Two different price-only trucks must not share a row...
    cli.ingest(cfg, conn, [{"source": "autotrader", "year": 2025, "price": 36831, "trim_ok": True},
                           {"source": "autotrader", "year": 2025, "price": 39308, "trim_ok": True}])
    assert sorted(r["key"] for r in db.all_listings(conn)) == ["autotrader:2025:$36831", "autotrader:2025:$39308"]
    # ...and a price-only mention of a known truck folds into it.
    db.upsert(conn, _car(price=36611, miles=14833))
    cli.ingest(cfg, conn, [{"source": "cars.com", "year": 2025, "price": 36611, "trim_ok": True}])
    assert len(db.all_listings(conn)) == 3


def test_http_flag_then_restore_closes_report(cfg, conn):
    key, _, _ = db.upsert(conn, _car(), today="2026-10-01")
    httpd = HTTPServer(("127.0.0.1", 0), web.make_handler(cfg, conn, token=""))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    try:
        assert call(f"/api/listings/{key}/flag", {"reason": "Wrong price", "note": "it's $36k"})["ok"]
        st = call("/api/state")
        assert st["listings"][0]["status"] == "flagged" and st["listings"][0]["reports"] == ["Wrong price: it's $36k"]
        assert st["reports"][0]["car"].startswith("2025 · 12,526 mi · $34,000")
        call(f"/api/listings/{key}", {"status": "active"})
        assert call("/api/state")["reports"][0]["status"] == "false-alarm"
    finally:
        httpd.shutdown()
