from datetime import datetime, timedelta
from email.message import EmailMessage

import pytest

from tracker import cli, db, notify
from tracker.parse import parse_email_bytes
from tracker.profile import profile_for


@pytest.fixture
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "push", lambda cfg, title, body, **kw: sent.append((title, body, kw)) or True)
    return sent


def _event(score, n=0):
    l = {"year": 2025, "miles": 10000 + n, "price": 36000 + n, "dealer": f"D{n}", "url": f"https://x/{n}", "distance": 20}
    return ("new", l, score, [])


def test_few_alerts_sent_individually(cfg, pushes):
    cli.send_alerts(cfg, [_event(100, 1), _event(900, 2)])
    assert [p[0] for p in pushes] == ["Frontier PRO-4X: new", "Frontier PRO-4X: new - good deal"]
    assert pushes[1][2]["priority"] == "high"


def test_burst_bundled_into_one_push(cfg, pushes):
    events = [_event(s, i) for i, s in enumerate([100, 50, 1200, -300, 700])]
    cli.send_alerts(cfg, events)
    assert len(pushes) == 1
    title, body, kw = pushes[0]
    assert title == "Frontier PRO-4X: 5 updates"
    assert body.splitlines()[0].startswith("[new] 2025 Frontier PRO-4X 10,002 mi")  # best score first
    assert "+2 more" in body
    assert kw["url"] == "https://x/2" and kw["priority"] == "high"


def test_failure_throttled_then_recovery(tmp_path, pushes):
    conn = db.connect(str(tmp_path / "t.db"))
    t0 = datetime(2026, 9, 28, 11)
    cli.report_health({}, conn, ["vpn: [AUTHENTICATIONFAILED]"], now=t0)
    cli.report_health({}, conn, ["vpn: [AUTHENTICATIONFAILED]"], now=t0 + timedelta(hours=3))
    assert [p[0] for p in pushes] == ["Car tracker: scan failed"]

    cli.report_health({}, conn, ["still broken"], now=t0 + timedelta(hours=24))
    assert len(pushes) == 2

    cli.report_health({}, conn, [], now=t0 + timedelta(hours=27))
    cli.report_health({}, conn, [], now=t0 + timedelta(hours=30))
    assert pushes[-1][0] == "Car tracker: recovered" and len(pushes) == 3


def test_unreadable_flag_only_for_mail_naming_the_model(profile):
    def mail(subject, body):
        m = EmailMessage()
        m["From"] = "CarGurus <alerts@mail.cargurus.com>"
        m["Subject"] = subject
        m["Message-ID"] = "<1@x>"
        m.set_content(body)
        return parse_email_bytes(m.as_bytes(), profile)

    meta, listings = mail("New matches for Nissan Frontier", "Click to see your new matches")
    assert meta["mentions_model"] and listings == []

    meta, _ = mail("Your weekly market report", "Prices are changing")
    assert not meta["mentions_model"]


def test_dry_run_scan_does_not_mark_emails(cfg, monkeypatch):
    from tracker import mailscan
    conn = db.connect(cfg["db_path"])
    m = EmailMessage()
    m["From"] = "Cars.com <alerts@cars.com>"
    m["Subject"] = "New match"
    m["Message-ID"] = "<dry@x>"
    m.set_content("2025 Nissan Frontier PRO-4X $36,000 10,000 mi")
    monkeypatch.setattr(mailscan, "fetch_account", lambda *a, **k: iter([m.as_bytes()]))
    cfg["imap"] = {"accounts": [{"user": "u@x"}], "lookback_days": 1}
    listings, _, _ = mailscan.scan(cfg, conn, dry_run=True)
    assert len(listings) == 1 and not db.email_seen(conn, "u@x", "<dry@x>")
    mailscan.scan(cfg, conn)
    assert db.email_seen(conn, "u@x", "<dry@x>")


def test_unknown_distance_never_high_priority(cfg, pushes):
    l = {"year": 2026, "miles": 13133, "price": 37998, "dealer": "CarMax Naperville", "distance": None}
    cli.send_alerts(cfg, [("new", l, 983, ["distance unknown"])])
    assert pushes[0][0] == "Frontier PRO-4X: new" and pushes[0][2]["priority"] == "default"
    assert "distance unknown" in pushes[0][1]


def test_alert_titles_follow_the_configured_car(crv, pushes):
    l = {"year": 2024, "miles": 9000, "price": 33000, "dealer": "D", "distance": 10, "features": {"awd": True}}
    cli.send_alerts(crv, [("new", l, 100, [])])
    title, body, _ = pushes[0]
    assert title == "CR-V Hybrid: new" and body.startswith("2024 CR-V Hybrid 9,000 mi $33,000 @ D AWD")
    assert profile_for(crv).ntfy_tag == "car"
