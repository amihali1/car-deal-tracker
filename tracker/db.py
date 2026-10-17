"""SQLite store: listings, price history, processed emails, trade-in offers."""
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    key           TEXT PRIMARY KEY,
    source        TEXT,
    year          INTEGER,
    miles         INTEGER,
    price         INTEGER,
    url           TEXT,
    vin           TEXT,
    dealer        TEXT,
    city          TEXT,
    distance      INTEGER,
    body          TEXT,             -- key from config body.styles, NULL unknown
    features      TEXT,             -- JSON {"tow": true, "premium": null, ...}; null = unknown
    cpo           INTEGER DEFAULT 0,
    accident      INTEGER,          -- 1 yes, 0 no, NULL unknown
    fees_included INTEGER DEFAULT 0,
    notes         TEXT,
    status        TEXT DEFAULT 'active',   -- see STATUSES
    first_seen    TEXT,
    last_seen     TEXT
);
CREATE TABLE IF NOT EXISTS prices (
    key   TEXT,
    date  TEXT,
    price INTEGER,
    PRIMARY KEY (key, date)
);
CREATE TABLE IF NOT EXISTS emails (
    account     TEXT,
    message_id  TEXT,
    source      TEXT,
    subject     TEXT,
    n_listings  INTEGER,
    processed_at TEXT,
    PRIMARY KEY (account, message_id)
);
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- "Wrong info" reports from the dashboard: the record of what the parser got wrong, with a
-- snapshot of the listing as stored and the email it came from, so the parser can be fixed.
CREATE TABLE IF NOT EXISTS reports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT,
    date       TEXT,
    reason     TEXT,
    note       TEXT,
    snapshot   TEXT,              -- JSON of the listing row + price history when flagged
    email_id   TEXT,              -- Message-ID of the alert email that last updated it
    status     TEXT DEFAULT 'open',   -- open | fixed | false-alarm
    resolution TEXT
);
CREATE TABLE IF NOT EXISTS offers (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    date   TEXT,
    buyer  TEXT,
    amount INTEGER,
    notes  TEXT
);
"""

FIELDS = ["source", "year", "miles", "price", "url", "vin", "dealer", "city", "distance",
          "body", "features", "cpo", "accident", "fees_included", "notes", "email_id"]
BOOLS = ("cpo", "accident", "fees_included")

# active | archived (gone, may relist) | passed (not interested) | flagged (reported wrong info)
# | removed (flagged and deleted from view; kept as a tombstone so alerts don't re-add it)
STATUSES = ("active", "archived", "passed", "flagged", "removed")
# Statuses the user chose: later alerts update these rows quietly and never re-activate them.
SILENT = ("passed", "flagged", "removed")


# Columns added after first deploy: (name, DDL). connect() adds any that are missing.
MIGRATIONS = [
    ("starred", "ALTER TABLE listings ADD COLUMN starred INTEGER DEFAULT 0"),
    ("email_id", "ALTER TABLE listings ADD COLUMN email_id TEXT"),
]

# Fields the web UI may edit directly.
EDITABLE = set(FIELDS) - {"source", "email_id"} | {"starred", "status"}

# Odometers below this are too common (delivery miles, round numbers) to identify a car.
DEDUPE_MIN_MILES = 1000


def _features(v):
    """DB/JSON text or dict -> dict."""
    if not v:
        return {}
    return json.loads(v) if isinstance(v, str) else dict(v)


def merge_features(old, new):
    """Keep what we knew when a terse email doesn't say (None = unknown)."""
    out = _features(old)
    for k, v in _features(new).items():
        if v is not None or k not in out:
            out[k] = v
    return out


def row_dict(row):
    d = dict(row)
    d["features"] = _features(d.get("features"))
    return d


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # Cron scan and the web server share the file; wait out each other's write locks.
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(listings)")}
    for name, ddl in MIGRATIONS:
        if name not in cols:
            conn.execute(ddl)
    conn.commit()
    return conn


def listing_key(l):
    if l.get("vin"):
        return l["vin"].upper()
    if l.get("listing_id"):
        return f"{l['source']}:{l['listing_id']}"
    miles = l.get("miles")
    bucket = round(miles, -2) if miles is not None else "na"
    who = (l.get("dealer") or l.get("city") or "").lower().replace(" ", "")[:20]
    if miles is None and not who:
        # Nothing but year + price: key on the price, or every such car would share one row.
        return f"{l['source']}:{l.get('year')}:${l.get('price')}"
    return f"{l['source']}:{l.get('year')}:{who}:{bucket}"


def find_same_car(conn, l):
    """Key of an existing listing for the same car seen under a different key, else None.

    Alert emails rarely carry a VIN or listing id, so the fallback key includes whatever
    dealer/city text the parser found, and the same car shows up twice ("Used Nissan" vs
    "Morrow, GA", or on two sites). An exact odometer reading + model year is specific enough.
    """
    miles = l.get("miles")
    if l.get("vin") or l.get("listing_id"):
        return None
    if miles is None and not (l.get("city") or l.get("dealer")) and l.get("price"):
        # Year + price is all the email gave (Autotrader "1 more vehicle", Cars.com recommendations):
        # an exact price match on a car we already know is almost surely the same one.
        row = conn.execute("SELECT key FROM listings WHERE year = ? AND price = ? ORDER BY first_seen LIMIT 1",
                           (l.get("year"), l["price"])).fetchone()
        return row["key"] if row else None
    if miles is None or miles < DEDUPE_MIN_MILES:
        return None
    row = conn.execute("SELECT key FROM listings WHERE year = ? AND miles = ? ORDER BY first_seen LIMIT 1",
                       (l.get("year"), miles)).fetchone()
    return row["key"] if row else None


def _bool(v):
    return None if v is None else int(bool(v))


def upsert(conn, l, today=None):
    """Insert or update a listing. Returns (key, event, delta).

    event: 'new' | 'drop' | 'rise' | 'same' | 'relisted' | 'passed'. delta = new price - old price.
    'passed' = the user already said no to this car; it's updated quietly and stays hidden.
    Price history rows are written only when the price changes (one per day max).
    """
    today = today or date.today().isoformat()
    key = l.get("key") or find_same_car(conn, l) or listing_key(l)
    row = conn.execute("SELECT * FROM listings WHERE key = ?", (key,)).fetchone()
    vals = {f: l.get(f) for f in FIELDS}
    for f in BOOLS:
        vals[f] = _bool(vals[f])
    vals["features"] = json.dumps(_features(vals["features"]))

    if row is None:
        conn.execute(
            f"INSERT INTO listings (key, {', '.join(FIELDS)}, status, first_seen, last_seen) "
            f"VALUES (?, {', '.join('?' * len(FIELDS))}, ?, ?, ?)",
            [key, *vals.values(), l.get("status", "active"), today, today],
        )
        if vals["price"]:
            conn.execute("INSERT OR REPLACE INTO prices VALUES (?, ?, ?)", (key, today, vals["price"]))
        conn.commit()
        return key, "new", 0

    # Merge: keep known values when the new source doesn't know them (terse alert emails).
    merged = {f: (vals[f] if vals[f] is not None else row[f]) for f in FIELDS}
    merged["features"] = json.dumps(merge_features(row["features"], vals["features"]))
    for f in ("cpo", "fees_included"):
        merged[f] = int(bool(row[f]) or bool(vals[f]))
    if row["notes"] and vals["notes"] and vals["notes"] not in row["notes"]:
        merged["notes"] = f"{row['notes']} | {vals['notes']}"
    elif row["notes"]:
        merged["notes"] = row["notes"]

    old_price, new_price = row["price"], merged["price"]
    event, delta = "same", 0
    if row["status"] == "archived" and l.get("status") != "archived":
        event = "relisted"
    if old_price and new_price and new_price != old_price:
        delta = new_price - old_price
        event = "drop" if delta < 0 else "rise"
        conn.execute("INSERT OR REPLACE INTO prices VALUES (?, ?, ?)", (key, today, new_price))

    status = l.get("status") or ("active" if event == "relisted" else row["status"])
    if row["status"] in SILENT:
        event, status = row["status"], row["status"]
    conn.execute(
        f"UPDATE listings SET {', '.join(f + ' = ?' for f in FIELDS)}, status = ?, last_seen = ? WHERE key = ?",
        [*merged.values(), status, max(today, row["last_seen"] or today), key],
    )
    conn.commit()
    return key, event, delta


def set_price(conn, key, price, today=None):
    today = today or date.today().isoformat()
    conn.execute("UPDATE listings SET price = ?, last_seen = ? WHERE key = ?", (price, today, key))
    conn.execute("INSERT OR REPLACE INTO prices VALUES (?, ?, ?)", (key, today, price))
    conn.commit()


def update_fields(conn, key, changes, today=None):
    """Apply a web-UI edit. 'seen': true marks the listing as checked still-for-sale today.
    A price change is logged to history. Returns the updated row, or None if no such key."""
    today = today or date.today().isoformat()
    row = conn.execute("SELECT * FROM listings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    sets = {f: v for f, v in changes.items() if f in EDITABLE}
    if "status" in sets and sets["status"] not in STATUSES:
        raise ValueError(f"bad status {sets['status']!r}")
    for f in BOOLS + ("starred",):
        if f in sets:
            sets[f] = _bool(sets[f])
    if "features" in sets:
        # Edits set features explicitly, including back to unknown (None).
        feats = _features(row["features"])
        feats.update(_features(sets["features"]))
        sets["features"] = json.dumps(feats)
    if changes.get("seen"):
        sets["last_seen"] = today
    if sets.get("price") and sets["price"] != row["price"]:
        conn.execute("INSERT OR REPLACE INTO prices VALUES (?, ?, ?)", (key, today, sets["price"]))
        sets["last_seen"] = today
    if sets:
        conn.execute(f"UPDATE listings SET {', '.join(f + ' = ?' for f in sets)} WHERE key = ?",
                     [*sets.values(), key])
    conn.commit()
    return row_dict(conn.execute("SELECT * FROM listings WHERE key = ?", (key,)).fetchone())


def expire_stale(conn, days, today=None):
    """Archive active, unstarred listings not seen in an alert (or checked by hand) for `days`.
    Returns the keys archived. A later alert for the same car brings it back as 'relisted'."""
    today = date.fromisoformat(today) if today else date.today()
    cutoff = (today - timedelta(days=days)).isoformat()
    keys = [r["key"] for r in conn.execute(
        "SELECT key FROM listings WHERE status = 'active' AND COALESCE(starred, 0) = 0 AND last_seen < ?",
        (cutoff,))]
    for key in keys:
        conn.execute("UPDATE listings SET status = 'archived' WHERE key = ?", (key,))
    conn.commit()
    return keys


def merge_duplicates(conn):
    """One-off cleanup for rows stored before find_same_car existed. Folds each later
    duplicate into the earliest row for that (year, miles). Returns [(kept, dropped)]."""
    merged = []
    groups = conn.execute(
        "SELECT year, miles FROM listings WHERE miles >= ? GROUP BY year, miles HAVING COUNT(*) > 1",
        (DEDUPE_MIN_MILES,)).fetchall()
    for g in groups:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM listings WHERE year = ? AND miles = ? ORDER BY (vin IS NULL), first_seen",
            (g["year"], g["miles"]))]
        keep = rows[0]
        for dup in rows[1:]:
            fill = {f: dup[f] for f in FIELDS + ["starred"] if keep.get(f) is None and dup.get(f) is not None}
            newer, older = (dup, keep) if dup["last_seen"] > keep["last_seen"] else (keep, dup)
            feats = json.dumps(merge_features(older["features"], newer["features"]))
            if feats != keep["features"]:
                fill["features"] = feats
            if dup["last_seen"] > keep["last_seen"]:
                fill["last_seen"] = dup["last_seen"]
                fill["price"] = dup["price"]
                if keep["status"] == "archived":
                    fill["status"] = dup["status"]
            if fill:
                conn.execute(f"UPDATE listings SET {', '.join(f + ' = ?' for f in fill)} WHERE key = ?",
                             [*fill.values(), keep["key"]])
                keep.update(fill)
            conn.execute("INSERT OR IGNORE INTO prices SELECT ?, date, price FROM prices WHERE key = ?",
                         (keep["key"], dup["key"]))
            conn.execute("DELETE FROM prices WHERE key = ?", (dup["key"],))
            conn.execute("DELETE FROM listings WHERE key = ?", (dup["key"],))
            merged.append((keep["key"], dup["key"]))
    conn.commit()
    return merged


def flag(conn, key, reason, note=None, today=None):
    """Record a "wrong info" report and hide the listing. Returns the report id, or None."""
    row = conn.execute("SELECT * FROM listings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    snap = {**row_dict(row), "history": history(conn, key)}
    cur = conn.execute(
        "INSERT INTO reports (key, date, reason, note, snapshot, email_id) VALUES (?, ?, ?, ?, ?, ?)",
        (key, today or date.today().isoformat(), reason, note, json.dumps(snap), row["email_id"]))
    conn.execute("UPDATE listings SET status = 'flagged' WHERE key = ?", (key,))
    conn.commit()
    return cur.lastrowid


def reports(conn, status=None):
    sql = "SELECT * FROM reports" + (" WHERE status = ?" if status else "") + " ORDER BY id DESC"
    out = []
    for r in conn.execute(sql, (status,) if status else ()):
        r = dict(r)
        r["snapshot"] = json.loads(r["snapshot"] or "{}")
        out.append(r)
    return out


def resolve_reports(conn, status, resolution, key=None, report_id=None):
    """Close open reports for a listing (key) or one report (report_id). Returns the count."""
    where, arg = ("key = ?", key) if key else ("id = ?", report_id)
    cur = conn.execute(f"UPDATE reports SET status = ?, resolution = ? WHERE {where} AND status = 'open'",
                       (status, resolution, arg))
    conn.commit()
    return cur.rowcount


def clear_junk_dealers(conn, junk):
    """Null out dealer names that were really email navigation text. Returns the count."""
    keys = [r["key"] for r in conn.execute("SELECT key, dealer FROM listings WHERE dealer IS NOT NULL")
            if junk.search(r["dealer"])]
    for key in keys:
        conn.execute("UPDATE listings SET dealer = NULL WHERE key = ?", (key,))
    conn.commit()
    return len(keys)


def all_listings(conn):
    return [row_dict(r) for r in conn.execute("SELECT * FROM listings")]


def all_history(conn):
    out = {}
    for r in conn.execute("SELECT key, date, price FROM prices ORDER BY date"):
        out.setdefault(r["key"], []).append({"date": r["date"], "price": r["price"]})
    return out


def set_status(conn, key, status):
    cur = conn.execute("UPDATE listings SET status = ? WHERE key = ?", (status, key))
    conn.commit()
    return cur.rowcount


def active(conn):
    return [row_dict(r) for r in conn.execute("SELECT * FROM listings WHERE status = 'active'")]


def history(conn, key):
    return [dict(r) for r in conn.execute("SELECT date, price FROM prices WHERE key = ? ORDER BY date", (key,))]


def email_seen(conn, account, message_id):
    return conn.execute("SELECT 1 FROM emails WHERE account = ? AND message_id = ?",
                        (account, message_id)).fetchone() is not None


def mark_email(conn, account, meta, n):
    conn.execute("INSERT OR REPLACE INTO emails VALUES (?, ?, ?, ?, ?, ?)",
                 (account, meta["message_id"], meta["source"], meta["subject"][:200], n,
                  datetime.now().isoformat(timespec="seconds")))
    conn.commit()


def add_offer(conn, buyer, amount, notes=None, today=None):
    conn.execute("INSERT INTO offers (date, buyer, amount, notes) VALUES (?, ?, ?, ?)",
                 (today or date.today().isoformat(), buyer, amount, notes))
    conn.commit()


def offers(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM offers ORDER BY amount DESC")]


def get_state(conn, key):
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn, key, value):
    if value is None:
        conn.execute("DELETE FROM state WHERE key = ?", (key,))
    else:
        conn.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, str(value)))
    conn.commit()
