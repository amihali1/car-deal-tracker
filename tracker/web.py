"""Phone-friendly dashboard: ranked listings, price history, quick edits. Standard library only.

    python -m tracker serve [--port 8095]

Optional WEB_TOKEN in .env: when set, every request needs it once (open /?token=...), after
which a cookie keeps you signed in. Meant to sit behind Tailscale, not on the open internet.
"""
import json
import logging
import secrets
from datetime import date, datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import db, value
from .cli import ingest
from .config import ROOT, secret
from .parse import detect_source, parse_text
from .profile import profile_for

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
COOKIE = "tracker_token"


def listing_view(row, hist, cfg, today):
    ranking = cfg["ranking"]
    r = dict(row)
    r["fair"] = value.fair_value(r, cfg)
    r["all_in"] = value.all_in(r, cfg)
    r["score"] = value.score(r, cfg)
    r["rank_score"], r["flags"] = value.rank(r, cfg)
    r["history"] = hist
    r["first_price"] = hist[0]["price"] if hist else r["price"]
    seen = r.get("last_seen")
    r["days_since_seen"] = (today - date.fromisoformat(seen)).days if seen else None
    r["stale"] = r["status"] == "active" and (r["days_since_seen"] or 0) >= ranking["stale_days"]
    if r["stale"]:
        r["flags"].append(f"not seen {r['days_since_seen']}d")
    return r


def snapshot(cfg, conn):
    today = date.today()
    hist = db.all_history(conn)
    rows = [listing_view(r, hist.get(r["key"], []), cfg, today) for r in db.all_listings(conn)
            if r["status"] != "removed"]
    open_reports = {}
    for rep in db.reports(conn, "open"):
        open_reports.setdefault(rep["key"], []).append(rep["reason"] + (f": {rep['note']}" if rep["note"] else ""))
    for r in rows:
        r["reports"] = open_reports.get(r["key"], [])
    rows.sort(key=lambda r: r["rank_score"], reverse=True)
    n = 0
    for r in rows:
        if r["status"] == "active":
            n += 1
            r["rank"] = n
    last_scan = db.get_state(conn, "last_scan")
    return {
        "listings": rows,
        "offers": db.offers(conn),
        "reports": [{k: rep[k] for k in ("id", "key", "date", "reason", "note", "status", "resolution")}
                    | {"car": _car_label(rep["snapshot"])} for rep in db.reports(conn)],
        "last_scan": last_scan,
        "failing": bool(db.get_state(conn, "failing")),
        "config": ui_config(cfg),
    }


def ui_config(cfg):
    """Everything the dashboard needs to label fields for this car."""
    p = profile_for(cfg)
    return {
        "vehicle": {"label": p.label, "make": p.make, "model": p.model, "icon": p.icon, "trims": p.trims},
        "body": {"want": p.body_want, "styles": {k: lbl for k, (lbl, _) in p.body_styles.items()}},
        "features": {k: {"label": f["label"], "short": f["short"], "required": f["required"], "value": f["value"]}
                     for k, f in p.features.items()},
        "trade_in": cfg.get("trade_in"),
        **{k: cfg[k] for k in ("ranking", "fair_value", "criteria")},
    }


def _car_label(s):
    miles = f"{s['miles']:,} mi" if s.get("miles") is not None else "? mi"
    price = f"${s['price']:,}" if s.get("price") else "$?"
    return f"{s.get('year')} · {miles} · {price} · {s.get('city') or s.get('dealer') or s.get('source')}"


def _coerce(changes):
    """JSON from the edit form -> DB types. Empty strings mean 'unknown'."""
    out = {}
    for k, v in changes.items():
        if v == "":
            v = None
        if k == "price" and v is None:
            continue  # every listing needs a price; blank means "leave it"
        if k in ("price", "miles", "year", "distance") and v is not None:
            v = int(str(v).replace(",", "").replace("$", "").strip())
        out[k] = v
    return out


def make_handler(cfg, conn, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "car-deal-tracker/1"

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        # --- helpers -------------------------------------------------------------------
        def _send(self, code, body, ctype="application/json", headers=None):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _authed(self):
            if not token:
                return True
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            have = cookie[COOKIE].value if COOKIE in cookie else self.headers.get("X-Token", "")
            return secrets.compare_digest(have, token)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        # --- routes --------------------------------------------------------------------
        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                given = parse_qs(url.query).get("token", [""])[0]
                if token and given and secrets.compare_digest(given, token):
                    # Swap the query-string token for a long-lived cookie, then drop it from the URL.
                    self._send(302, b"", "text/plain", {
                        "Location": "/",
                        "Set-Cookie": f"{COOKIE}={token}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict"})
                    return
                if not self._authed():
                    self._send(401, b"Open /?token=... once to sign in.", "text/plain")
                    return
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                return
            if not self._authed():
                self._send(401, {"error": "unauthorized"})
                return
            if url.path == "/api/state":
                self._send(200, snapshot(cfg, conn))
                return
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authed():
                self._send(401, {"error": "unauthorized"})
                return
            url = urlparse(self.path)
            try:
                body = self._body()
                if url.path.startswith("/api/listings/") and url.path.endswith("/flag"):
                    key = unquote(url.path[len("/api/listings/"):-len("/flag")])
                    if not body.get("reason"):
                        raise ValueError("pick what's wrong")
                    rid = db.flag(conn, key, body["reason"], (body.get("note") or "").strip() or None)
                    self._send(200 if rid else 404, {"ok": bool(rid), "report": rid})
                elif url.path.startswith("/api/listings/"):
                    key = unquote(url.path[len("/api/listings/"):])
                    changes = _coerce(body)
                    if changes.get("status") == "active":
                        db.resolve_reports(conn, "false-alarm", "restored from the dashboard", key=key)
                    row = db.update_fields(conn, key, changes)
                    if row is None:
                        self._send(404, {"error": f"no listing {key}"})
                        return
                    self._send(200, {"ok": True})
                elif url.path == "/api/listings":
                    self._send(200, add_listing(cfg, conn, body))
                elif url.path == "/api/offers":
                    db.add_offer(conn, body["buyer"], int(body["amount"]), body.get("notes") or None)
                    self._send(200, {"ok": True})
                else:
                    self._send(404, {"error": "not found"})
            except (ValueError, KeyError, TypeError) as e:
                self._send(400, {"error": f"{type(e).__name__}: {e}"})

    return Handler


def add_listing(cfg, conn, body):
    """Pasted listing text and/or explicit fields -> stored listing (same path as the CLI 'add')."""
    body = _coerce(body)
    text = body.pop("text", None) or ""
    parsed = parse_text(text, profile_for(cfg), detect_source(text) if text else "manual") if text else []
    l = parsed[0] if parsed else {"source": "manual"}
    if l["source"] == "manual" and body.get("url"):
        l["source"] = detect_source(body["url"])
    # Form values win over what the pasted text said; "unknown" in the form keeps the parsed value.
    feats = {**(l.get("features") or {}),
             **{k: v for k, v in (body.pop("features", None) or {}).items() if v is not None}}
    for k, v in body.items():
        if v is not None:
            l[k] = v
    l["features"] = feats
    l["trim_ok"] = True  # added on purpose
    if not l.get("price"):
        raise ValueError("need a price (type it, or paste text containing one)")
    events = ingest(cfg, conn, [l])
    if not l.get("key"):
        _, reasons = value.matches(l, cfg)
        return {"ok": False, "error": "doesn't meet the search criteria: " + ", ".join(reasons)}
    return {"ok": True, "key": l["key"], "event": events[0][0] if events else "updated"}


def serve(cfg, conn, port):
    token = secret("WEB_TOKEN") or ""
    # Single-threaded on purpose: one shared SQLite connection, one person on a phone.
    httpd = HTTPServer(("0.0.0.0", port), make_handler(cfg, conn, token))
    print(f"{datetime.now():%H:%M:%S} serving on http://0.0.0.0:{port}"
          f"{' (token required)' if token else ''}  db={cfg['db_path']}  root={ROOT}", flush=True)
    httpd.serve_forever()
