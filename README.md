# car-deal-tracker

Find the best-value used car for **any make, model and trim** without refreshing listing sites all day.

You save searches on CarGurus, Cars.com, Autotrader, Carvana, CarMax and Facebook Marketplace and point their alert
emails at one inbox. The tracker reads that inbox over IMAP, pulls each listing out of the email, scores it against a
fair-value model you control, and pushes the good ones to your phone. A small web dashboard ranks everything it has
seen, tracks price drops, and lets you fix what an email got wrong.

It never scrapes the listing sites (their terms forbid it); alert emails, pasted listing text and manual entries are
the only inputs.

```
saved-search alert emails ──IMAP──▶ parser ──▶ criteria + fair value ──▶ SQLite ──▶ ntfy push
     (6 car sites)                  (per-car profile from config.json)        └──────▶ web dashboard
```

## What it does

- **Parses alert emails from six sites**: price, miles, year, trim, VIN, dealer, city, body style, packages,
  accident history, certified pre-owned, dealer-fee wording. Handles the awkward layouts (CarGurus prices sitting
  above their titles, search-radius footers that look like odometers, site HQ addresses in footers).
- **One config file per car.** Make, model, wanted trims, body styles and the packages you care about, each with
  detection phrases, a "must have" flag and a dollar value. Nothing about the car is hard-coded.
- **Deal score** = fair value − all-in cost. Fair value comes from a per-year price table adjusted for mileage,
  packages, CPO and accidents. All-in cost adds dealer fees and the cost of driving to get it.
- **New cars too, if you want them.** Opt in and new listings rank alongside used ones against their own
  fair value (MSRP minus the usual discount when the listing shows one, else your per-year new-car prices).
- **Pessimistic ranking.** Alert emails are terse, so a listing that doesn't mention a must-have package loses
  points instead of looking like a bargain.
- **Push alerts** via [ntfy](https://ntfy.sh): new listings, relistings and price drops, bundled when an email
  burst arrives, high priority for real deals. It also tells you when a site's email format changes and nothing
  parses, or when the scan itself fails.
- **Dashboard** (phone-friendly, no build step): ranked list, price history sparklines, star / pass / archive,
  edit any field, "wrong info" reports that keep a snapshot plus the source email so the parser can be fixed,
  and trade-in offer tracking.
- **Standard library only.** Python 3.10+, `sqlite3`, `imaplib`, `http.server`. Tests use pytest.

## Quick start

```bash
git clone https://github.com/amihali1/car-deal-tracker && cd car-deal-tracker
cp config.example.json config.json        # then edit it for your car (docs/SETUP.md)
cp .env.example .env                      # Gmail address + app password, ntfy topic

python -m tracker check --text "2025 Nissan Frontier PRO-4X Crew Cab Tow Package 12,000 mi \$35,900 Marietta, GA"
python -m tracker scan --dry-run          # read alert emails, print what it would store
python -m tracker scan                    # store + push
python -m tracker serve                   # dashboard on http://localhost:8095
```

**[docs/SETUP.md](docs/SETUP.md)** walks through configuring it for your car, from saved searches and the Gmail
app password to tuning the fair-value numbers and running it on a server with cron.

Examples: [`examples/nissan-frontier-pro4x.json`](examples/nissan-frontier-pro4x.json) (pickup, must-have tow
package, cab styles) and [`examples/honda-crv-hybrid.json`](examples/honda-crv-hybrid.json) (SUV, must-have AWD,
moonroof bonus, price cap, no body styles).

## Commands

| Command | What it does |
|---|---|
| `check [--text "..."]` | Show what the parser looks for with your config; optionally parse a sample |
| `scan [--dry-run] [--days N] [--reprocess]` | Read alert emails, store listings, push alerts |
| `parse FILE.eml\|FILE.txt\|-` | Run the parser on a saved email or text without storing anything |
| `add --text "..." [--price N] [--feature tow=yes]` | Add a listing from pasted text |
| `report [--top N]` | Active listings ranked by deal score |
| `serve [--port N]` | Web dashboard |
| `reports [--fetch] [--resolve ID]` | "Wrong info" reports flagged on the dashboard |
| `rebuild --days N` | Re-derive listings from recent email after a parser fix (keeps your decisions) |
| `price KEY N`, `archive KEY`, `offer BUYER N`, `cleanup`, `seed`, `notify-test` | Housekeeping |

## Layout

```
tracker/profile.py   the car: config -> heading/trim/body/feature/VIN regexes
tracker/parse.py     email + text parsing (site layouts, prices, miles, location)
tracker/value.py     criteria, fair value, deal score, ranking
tracker/db.py        SQLite: listings, price history, emails seen, reports, offers
tracker/mailscan.py  IMAP (Gmail search syntax over X-GM-RAW)
tracker/notify.py    ntfy pushes
tracker/web.py       JSON API + static dashboard (tracker/static/index.html)
tracker/cli.py       commands
```

## Tests

```bash
pip install pytest
python -m pytest
```

The suite runs the parser against real alert-email wording for each site, a second vehicle profile (Honda CR-V)
to keep the code car-agnostic, the database merge rules, alert bundling and the HTTP API.
