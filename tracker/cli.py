"""Command line entry point: python -m tracker <command>."""
import argparse
import json
import logging
import sys
from datetime import datetime, timedelta

from . import db, notify, value
from .config import ROOT, ConfigError, load_config
from .parse import detect_source, parse_email_bytes, parse_text
from .profile import profile_for

log = logging.getLogger("tracker")


def _enrich(l, cfg):
    if l.get("distance") is None:
        l["distance"] = value.distance_for(l.get("city") or l.get("dealer"), cfg["distances"],
                                           cfg.get("state_distances"))
    return l


def ingest(cfg, conn, listings, dry_run=False):
    """Filter, store, and alert. Returns list of (event, listing, score, reasons)."""
    alerts = cfg["alerts"]
    events = []
    for l in listings:
        _enrich(l, cfg)
        ok, reasons = value.matches(l, cfg)
        if not ok:
            log.info("skip %s %s $%s: %s", l.get("source"), l.get("year"), l.get("price"), ", ".join(reasons))
            continue
        if not l.get("price"):
            log.info("skip %s %s: no price found", l.get("source"), (l.get("url") or "")[:60])
            continue
        s = value.score(l, cfg)
        if dry_run:
            events.append(("dry-run", l, s, reasons))
            continue
        key, event, delta = db.upsert(conn, l, today=l.get("seen"))
        l["key"] = key
        if event == "new" or event == "relisted" or (event == "drop" and -delta >= alerts["min_price_drop"]):
            events.append((event if event != "drop" else f"drop {delta:+,}", l, s, reasons))
    return events


def _describe(cfg, l, s, reasons):
    p = profile_for(cfg)
    miles = f"{l['miles']:,} mi" if l.get("miles") is not None else "mi ?"
    where = l.get("dealer") or l.get("city") or l.get("source")
    extra = f" ({', '.join(reasons)})" if reasons else ""
    feats = l.get("features") or {}
    has = "".join(f" {f['short']}" for k, f in p.features.items() if feats.get(k))
    return f"{l.get('year')} {p.label} {miles} ${l['price']:,} @ {where}{has} | {s:+,} vs fair{extra}"


BUNDLE_OVER = 3  # more alerts than this in one scan -> a single summary push
FAILURE_PUSH_EVERY = timedelta(hours=24)


def _is_good(l, s, good_score):
    # Unknown distance means the trip cost is missing from the score, so it may be flattering.
    return s >= good_score and l.get("distance") is not None


def send_alerts(cfg, events):
    good_score = cfg["alerts"]["good_deal_score"]
    label = profile_for(cfg).label
    if len(events) > BUNDLE_OVER:
        ranked = sorted(events, key=lambda e: e[2], reverse=True)
        best = ranked[0]
        lines = [f"[{e}] {_describe(cfg, l, s, r)}" for e, l, s, r in ranked[:BUNDLE_OVER]]
        more = len(events) - BUNDLE_OVER
        body = "\n".join(lines) + f"\n+{more} more - run 'python -m tracker report'"
        notify.push(cfg, f"{label}: {len(events)} updates", body, url=best[1].get("url"),
                    priority="high" if _is_good(best[1], best[2], good_score) else "default")
        return
    for event, l, s, reasons in events:
        good = _is_good(l, s, good_score)
        title = f"{label}: {event}{' - good deal' if good else ''}"
        notify.push(cfg, title, _describe(cfg, l, s, reasons), url=l.get("url"),
                    priority="high" if good else "default")


def send_unreadable(cfg, unreadable):
    for source, subject in unreadable:
        notify.push(cfg, f"{profile_for(cfg).label}: couldn't read {source} alert",
                    f'"{subject[:120]}" - no listings found. Parser needs a look.', tags="warning")


def report_health(cfg, conn, errors, now=None):
    """Push on failure at most once per FAILURE_PUSH_EVERY; push once when it recovers."""
    now = now or datetime.now()
    if errors:
        last = db.get_state(conn, "last_failure_push")
        db.set_state(conn, "failing", 1)
        if last is None or now - datetime.fromisoformat(last) >= FAILURE_PUSH_EVERY:
            notify.push(cfg, "Car tracker: scan failed", "\n".join(errors)[:500],
                        tags="rotating_light", priority="high")
            db.set_state(conn, "last_failure_push", now.isoformat(timespec="seconds"))
    elif db.get_state(conn, "failing"):
        notify.push(cfg, "Car tracker: recovered", "Scans are working again.", tags="white_check_mark")
        db.set_state(conn, "failing", None)
        db.set_state(conn, "last_failure_push", None)


def cmd_scan(cfg, conn, args):
    from .mailscan import scan
    listings, unreadable, errors, events = [], [], [], []
    try:
        listings, unreadable, errors = scan(cfg, conn, lookback_days=args.days, reprocess=args.reprocess, dry_run=args.dry_run)
        events = ingest(cfg, conn, listings, dry_run=args.dry_run)
    except Exception as e:  # anything unexpected still has to reach the phone
        log.exception("scan crashed")
        errors.append(f"crash: {type(e).__name__}: {e}")
    for event, l, s, reasons in events:
        print(f"[{event}] {_describe(cfg, l, s, reasons)}")
    for source, subject in unreadable:
        print(f"[unreadable] {source}: {subject}")
    for err in errors:
        print(f"[error] {err}")
    if not args.dry_run:
        send_alerts(cfg, events)
        send_unreadable(cfg, unreadable)
        report_health(cfg, conn, errors)
        if not errors:
            db.set_state(conn, "last_scan", datetime.now().isoformat(timespec="seconds"))
            for key in db.expire_stale(conn, cfg["ranking"]["expire_days"]):
                print(f"[expired] {key}: not seen in {cfg['ranking']['expire_days']} days")
    print(f"{len(listings)} parsed, {len(events)} alert(s), {len(unreadable)} unreadable, {len(errors)} error(s)")
    if errors:
        sys.exit(1)


def cmd_parse(cfg, conn, args):
    """Test the parser on a saved .eml or text file (or '-' for stdin). Stores nothing."""
    if args.path == "-":
        raw = sys.stdin.buffer.read()
    else:
        raw = open(args.path, "rb").read()
    profile = profile_for(cfg)
    if args.path.endswith(".eml"):
        meta, listings = parse_email_bytes(raw, profile)
        print(f"source={meta['source']} subject={meta['subject']!r}")
    else:
        text = raw.decode("utf-8", "replace")
        listings = parse_text(text, profile, args.source or detect_source(text))
    _print_parsed(cfg, listings)


def _print_parsed(cfg, listings):
    if not listings:
        print("no listings found (no '<year> <model>' heading or price in range)")
    for l in listings:
        _enrich(l, cfg)
        ok, reasons = value.matches(l, cfg)
        print(json.dumps({k: v for k, v in l.items() if v not in (None, False, "", {})}, indent=None))
        print(f"   -> {'MATCH' if ok else 'skip'}: {', '.join(reasons) or 'all criteria known'}")
        if ok and l.get("price"):
            print(f"      fair value ${value.fair_value(l, cfg):,}, all-in ${value.all_in(l, cfg):,}, "
                  f"deal score {value.score(l, cfg):+,}")


def cmd_check(cfg, conn, args):
    """Show what the parser looks for, and optionally run it on sample text."""
    print(profile_for(cfg).summary())
    c = cfg["criteria"]
    years = f"{c['min_year']}-{c['max_year']}" if c.get("max_year") else f"{c['min_year']}+"
    print(f"Criteria: {years}, <= {c['max_miles']:,} mi" + (f", <= ${c['max_price']:,}" if c.get("max_price") else ""))
    for account in cfg.get("imap", {}).get("accounts", []):
        print(f"Mailbox: {account.get('user')}  query: {account.get('gmail_query')}")
    print(f"ntfy topic: {'set' if cfg.get('ntfy', {}).get('topic') else 'NOT set (pushes are only logged)'}")
    if args.text:
        print(f"\nParsing: {args.text!r}")
        _print_parsed(cfg, parse_text(args.text, profile_for(cfg), detect_source(args.text)))


def cmd_add(cfg, conn, args):
    """Add from pasted listing text (stdin or --text)."""
    text = args.text or sys.stdin.read()
    listings = parse_text(text, profile_for(cfg), args.source or "manual")
    if not listings:
        sys.exit("Couldn't find a year or price in that text.")
    l = listings[0]
    for f in ("price", "miles", "year", "distance"):
        if getattr(args, f) is not None:
            l[f] = getattr(args, f)
    if args.url:
        l["url"] = args.url
    for spec in args.feature or []:
        name, _, yn = spec.partition("=")
        if name not in cfg.get("features", {}) or yn not in ("yes", "no"):
            sys.exit(f"--feature wants NAME=yes|no with NAME one of: {', '.join(cfg.get('features', {}))}")
        l.setdefault("features", {})[name] = yn == "yes"
    l["trim_ok"] = True  # user is adding it on purpose
    events = ingest(cfg, conn, [l])
    print(f"stored as {l.get('key')}" if l.get("key") else "not stored (fails criteria, see log)")
    for event, l, s, reasons in events:
        print(f"[{event}] {_describe(cfg, l, s, reasons)}")


def cmd_seed(cfg, conn, args):
    """Load hand-collected listings from data/seed.json (optional; a JSON list of listing objects)."""
    path = ROOT / "data" / "seed.json"
    if not path.exists():
        print(f"no {path.name}; nothing to seed")
        return
    rows = json.loads(path.read_text(encoding="utf-8"))
    for r in rows:
        today = r.pop("date", None)
        db.upsert(conn, r, today=today)
    print(f"seeded {len(rows)} listings from {path.name}")


def cmd_report(cfg, conn, args):
    p = profile_for(cfg)
    must = [k for k, f in p.features.items() if f["required"]]
    rows = db.active(conn)
    rows.sort(key=lambda r: value.rank(r, cfg)[0], reverse=True)
    print(f"{'score':>7} {'price':>8} {'all-in':>8} {'miles':>7}  {'must-haves':<10} key / where")
    for r in rows[: args.top]:
        h = db.history(conn, r["key"])
        trend = ""
        if len(h) > 1 and h[-1]["price"] != h[0]["price"]:
            trend = f"  ({h[-1]['price'] - h[0]['price']:+,} since {h[0]['date']})"
        marks = {True: "+", False: "-"}
        have = " ".join(f"{p.features[k]['short']}{marks.get(r['features'].get(k), '?')}" for k in must) or "-"
        miles = f"{r['miles']:,}" if r["miles"] is not None else "?"
        print(f"{value.score(r, cfg):>+7,} {r['price']:>8,} {value.all_in(r, cfg):>8,} {miles:>7}  {have:<10} "
              f"{r['key']} / {r['dealer'] or r['city']}{trend}")
    offers = db.offers(conn)
    if offers:
        best = offers[0]
        label = cfg.get("trade_in", {}).get("label", "your current car")
        print(f"\nBest trade-in offer for {label}: ${best['amount']:,} from {best['buyer']} ({best['date']})")


def cmd_price(cfg, conn, args):
    db.set_price(conn, args.key, args.price)
    print(f"{args.key} -> ${args.price:,}")


def cmd_archive(cfg, conn, args):
    n = db.set_status(conn, args.key, "archived")
    print("archived" if n else f"no listing {args.key}")


def cmd_offer(cfg, conn, args):
    if args.buyer:
        db.add_offer(conn, args.buyer, args.amount, args.notes)
    for o in db.offers(conn):
        print(f"${o['amount']:>7,}  {o['buyer']:<12} {o['date']}  {o['notes'] or ''}")


def cmd_cleanup(cfg, conn, args):
    """Fix rows stored by older parser builds: junk dealer text, missing distances, duplicate cars."""
    print(f"cleared {db.clear_junk_dealers(conn, profile_for(cfg).junk_dealer)} junk dealer name(s)")
    for r in db.all_listings(conn):
        if r["distance"] is None:
            d = value.distance_for(r["city"] or r["dealer"], cfg["distances"], cfg.get("state_distances"))
            if d is not None:
                db.update_fields(conn, r["key"], {"distance": d}, today=r["last_seen"])
                print(f"distance {r['key']}: {d} mi ({r['city']})")
    for kept, dropped in db.merge_duplicates(conn):
        print(f"merged {dropped} -> {kept}")


def cmd_rebuild(cfg, conn, args):
    """Re-derive every email-sourced listing after a parser fix. Keeps seed rows, the reports log,
    and what the user decided (status, star, notes) for listings that still exist. No pushes."""
    from .mailscan import scan
    keep = {r["key"]: r for r in db.all_listings(conn)
            if r["status"] != "active" or r["starred"] or r["notes"]}
    listings, unreadable, errors = scan(cfg, conn, lookback_days=args.days, reprocess=True, dry_run=True)
    if errors:
        sys.exit("mail scan failed, nothing changed: " + "; ".join(errors))
    conn.execute("DELETE FROM listings")
    conn.execute("DELETE FROM prices")
    conn.commit()
    cmd_seed(cfg, conn, args)
    events = ingest(cfg, conn, listings)
    restored = 0
    for key, r in keep.items():
        if db.update_fields(conn, key, {"status": r["status"], "starred": r["starred"], "notes": r["notes"]},
                            today=r["last_seen"]):
            restored += 1
        else:
            print(f"[gone] {key} ({r['status']}) is no longer produced by the parser")
    expired = db.expire_stale(conn, cfg["ranking"]["expire_days"])
    print(f"{len(listings)} parsed from {args.days} days of email, {len(db.active(conn))} active, "
          f"{restored} user decisions restored, {len(expired)} expired, {len(unreadable)} unreadable")


def cmd_reports(cfg, conn, args):
    """The 'wrong info' log from the dashboard. --fetch saves each report's source email to
    data/reports/ so the parser can be fixed against it (then add a test and --resolve)."""
    if args.resolve:
        n = db.resolve_reports(conn, "fixed", args.note or "parser fixed", report_id=args.resolve)
        print(f"resolved {n} report(s)")
        return
    rows = db.reports(conn, None if args.all else "open")
    for r in rows:
        s = r["snapshot"]
        print(f"#{r['id']} [{r['status']}] {r['date']} {r['reason']}: {r['note'] or ''}")
        print(f"    {s.get('year')} {s.get('miles')} mi ${s.get('price')} | {s.get('city')} | {s.get('dealer')} "
              f"| {s.get('source')} | key {r['key']}")
        if r["resolution"]:
            print(f"    resolution: {r['resolution']}")
        if args.fetch and r["email_id"]:
            path = fetch_email(cfg, r["email_id"], ROOT / "data" / "reports" / f"report-{r['id']}.eml")
            print(f"    email: {path or 'not found'}")
    print(f"{len(rows)} report(s)")


def fetch_email(cfg, message_id, dest):
    from .mailscan import fetch_account
    for account in cfg["imap"]["accounts"]:
        query = f"in:anywhere rfc822msgid:{message_id.strip('<>')}"
        for raw in fetch_account(cfg["imap"], account, 0, query=query):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
            return dest
    return None


def cmd_serve(cfg, conn, args):
    from .web import serve
    serve(cfg, conn, args.port or cfg["web"]["port"])


def cmd_notify_test(cfg, conn, args):
    ok = notify.push(cfg, f"{profile_for(cfg).label} tracker test", "If you see this, alerts work.")
    print("sent" if ok else "not sent (set NTFY_TOPIC in .env)")


def main(argv=None):
    p = argparse.ArgumentParser(prog="tracker", description="Used-car deal tracker: alert emails -> SQLite -> push")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="read alert emails, store listings, push alerts")
    s.add_argument("--dry-run", action="store_true", help="parse and print, store nothing")
    s.add_argument("--days", type=int, help="lookback window (default from config)")
    s.add_argument("--reprocess", action="store_true", help="re-read emails already processed")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("check", help="show what the parser looks for; --text runs it on a sample")
    s.add_argument("--text")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("parse", help="test the parser on a .eml/.txt file or '-' (stdin)")
    s.add_argument("path")
    s.add_argument("--source")
    s.set_defaults(fn=cmd_parse)

    s = sub.add_parser("add", help="add a listing from pasted text")
    s.add_argument("--text")
    s.add_argument("--source")
    s.add_argument("--url")
    s.add_argument("--price", type=int)
    s.add_argument("--miles", type=int)
    s.add_argument("--year", type=int)
    s.add_argument("--distance", type=int)
    s.add_argument("--feature", action="append", metavar="NAME=yes|no",
                   help="set a configured feature, e.g. --feature tow=yes (repeatable)")
    s.set_defaults(fn=cmd_add)

    sub.add_parser("seed", help="load data/seed.json").set_defaults(fn=cmd_seed)

    s = sub.add_parser("report", help="active listings ranked by deal score")
    s.add_argument("--top", type=int, default=20)
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("price", help="log a new price for a listing")
    s.add_argument("key")
    s.add_argument("price", type=int)
    s.set_defaults(fn=cmd_price)

    s = sub.add_parser("archive", help="mark a listing sold/passed")
    s.add_argument("key")
    s.set_defaults(fn=cmd_archive)

    s = sub.add_parser("offer", help="log or list trade-in / sale offers for your current car")
    s.add_argument("buyer", nargs="?")
    s.add_argument("amount", nargs="?", type=int)
    s.add_argument("--notes")
    s.set_defaults(fn=cmd_offer)

    sub.add_parser("cleanup", help="fix old rows: junk dealer names, missing distances, duplicate cars"
                   ).set_defaults(fn=cmd_cleanup)

    s = sub.add_parser("rebuild", help="re-derive listings from recent emails after a parser fix (no pushes)")
    s.add_argument("--days", type=int, default=21)
    s.set_defaults(fn=cmd_rebuild)

    s = sub.add_parser("reports", help="'wrong info' reports flagged on the dashboard")
    s.add_argument("--all", action="store_true", help="include fixed / false-alarm reports")
    s.add_argument("--fetch", action="store_true", help="save each report's source email to data/reports/")
    s.add_argument("--resolve", type=int, metavar="ID", help="mark a report fixed")
    s.add_argument("--note", help="resolution note for --resolve")
    s.set_defaults(fn=cmd_reports)

    s = sub.add_parser("serve", help="run the web dashboard")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)

    sub.add_parser("notify-test", help="send a test ntfy push").set_defaults(fn=cmd_notify_test)

    args = p.parse_args(argv)
    # Listing titles (esp. FB) carry emoji; the Windows console codepage can't encode them.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO if args.verbose or args.cmd in ("scan", "serve") else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config()
    except ConfigError as e:
        sys.exit(str(e))
    conn = db.connect(cfg["db_path"])
    args.fn(cfg, conn, args)


if __name__ == "__main__":
    main()
