# Setting it up for your car

This takes about an hour, most of it on step 1 (saved searches) and step 5 (pricing research).
You need Python 3.10+ and a Gmail account you're willing to dedicate to car alerts.

1. [Save searches on the car sites](#1-save-searches-on-the-car-sites)
2. [Make an inbox just for alerts](#2-make-an-inbox-just-for-alerts)
3. [Describe your car in config.json](#3-describe-your-car-in-configjson)
4. [Check the parser understands your listings](#4-check-the-parser-understands-your-listings)
5. [Set the fair-value numbers](#5-set-the-fair-value-numbers)
6. [Distances from home](#6-distances-from-home)
7. [Phone alerts with ntfy](#7-phone-alerts-with-ntfy)
8. [Run it](#8-run-it)
9. [Run it on a server (optional)](#9-run-it-on-a-server-optional)
10. [When a site changes its emails](#10-when-a-site-changes-its-emails)

---

## 1. Save searches on the car sites

On each site, search for your car (year range, make, model, trim if the site has a trim filter, max miles,
a radius around your ZIP) and save the search with email alerts turned on, sent to the inbox from step 2.

| Site | Where |
|---|---|
| CarGurus | Search → **Save search** → instant or daily email |
| Cars.com | Search → **Save search** ("Inventory Alert" emails carry the dealer location; turn them on) |
| Autotrader | Search → **Save search** → email alerts |
| Carvana | Search → heart a car or **Save search** |
| CarMax | Search → **Save search** |
| Facebook Marketplace | Search → **Notify me** / save; alerts go to the Facebook account's email |

Keep the site filters a little *wider* than what you actually want (e.g. any trim), because the tracker applies
your exact criteria itself and can tell you about near-misses. Narrow them if the volume gets silly.

## 2. Make an inbox just for alerts

Use a separate Gmail address, not your personal inbox. The tracker stores an app password for it on whatever
machine runs the scan, and you don't want that password to open your real email.

1. Create the account (or use the one tied to the Facebook profile you use for Marketplace).
2. Turn on 2-Step Verification, then **Google Account → Security → App passwords** → create one.
3. Gmail → Settings → **Forwarding and POP/IMAP** → enable IMAP.
4. `cp .env.example .env` and fill in `IMAP_USER` (the address) and `IMAP_PASSWORD` (the 16-letter app password).

`.env` is gitignored. Never put these values in `config.json`.

The scan searches the whole mailbox (`[Gmail]/All Mail`) with a Gmail query from `config.json`, including
Spam, since new senders often land there. Edit `imap.accounts[0].gmail_query` so the last part names your model:

```json
"gmail_query": "in:anywhere -in:trash from:(facebookmail.com OR cargurus.com OR cars.com OR autotrader.com OR carvana.com OR carmax.com) (\"cr-v\" OR crv OR marketplace)"
```

## 3. Describe your car in config.json

```bash
cp config.example.json config.json
```

Everything about the car is in four sections. Here is the CR-V example
([full file](../examples/honda-crv-hybrid.json)) next to the Frontier one ([full file](../examples/nissan-frontier-pro4x.json)):

### `vehicle`

```json
"vehicle": {
  "label": "CR-V Hybrid",                    // shown in pushes and the dashboard title
  "make": "Honda",
  "model": "CR-V",                           // must appear in the listing title ("2024 Honda CR-V ...")
  "trims": ["Sport Touring", "Sport-L"],     // wanted trims; [] accepts any trim
  "other_trims": ["EX-L", "EX", "LX"],       // trims that look similar; a title naming one of these is rejected
  "vin_prefixes": [],                        // optional first VIN characters, e.g. ["1N6"] for Frontiers
  "price_range": [10000, 60000],             // dollar amounts outside this are ignored (monthly payments, savings...)
  "certified_phrases": ["honda certified"],  // extra wording that means certified pre-owned
  "icon": "🚙",
  "ntfy_tag": "blue_car"                     // emoji shortcode for pushes (see ntfy docs)
}
```

(JSON doesn't allow comments; they're only here to explain.)

How matching works:

- A listing starts at **`<year> [make] <model>`** ("2024 Honda CR-V", "Used 2024 CR-V"). Spaces and hyphens are
  flexible, so `"CR-V"` also matches "CR V" and "CRV", and `"F-150"` matches "F150".
- The **trim** is read from the ~40 characters right after that title first, then from the rest of the listing.
  `other_trims` exists for near-identical names: the Frontier example lists `PRO-X` so a PRO-X never counts
  as a PRO-4X.
- A listing that never names your model is rejected even if the trim matches (a Titan PRO-4X in a Frontier search).
- Without `vin_prefixes`, only 17 characters labelled "VIN" count as a VIN. The first 3 characters of any VIN for
  your model (from a real listing) make a good prefix.

### `body`

Optional. Use it when the same model comes in body styles you care about (crew vs king cab, sedan vs hatchback,
short vs long bed).

```json
"body": {
  "want": ["crew"],
  "styles": {
    "crew": {"label": "Crew Cab", "patterns": ["crew cab", "crewmax"]},
    "king": {"label": "King Cab", "patterns": ["king cab"]}
  }
}
```

A listing that names an unwanted style is rejected; one that names none is kept and marked "body unknown".
Leave `"body": {}` if it doesn't matter.

### `features`

Packages and options. Each one is detected from the listing text:

```json
"features": {
  "awd":     {"label": "All-wheel drive", "short": "AWD",
              "yes": ["awd", "all[- ]wheel drive"], "no": ["fwd", "front[- ]wheel drive"], "required": true},
  "sunroof": {"label": "Moonroof", "short": "Roof", "yes": ["moonroof", "sunroof"], "value": 800}
}
```

- `yes` / `no`: phrases (regular expressions, case-insensitive, matched as whole words) that say the car
  does / doesn't have it. `no` wins when both appear.
- `required: true`: a must-have. A listing that says it **lacks** it is rejected; one that doesn't say either way is
  kept but ranked lower (`ranking.unknown_feature`) and shown with a "AWD ?" chip you can tap to fill in.
- `value`: dollars it adds to fair value (a must-have usually has no value; it's already assumed in your prices).

Alert emails rarely list options, so most features will start as unknown. That's expected: open the listing,
then set them on the dashboard's Edit form.

### `criteria`

```json
"criteria": {"min_year": 2023, "max_year": null, "max_miles": 40000, "max_price": 38000}
```

`max_miles` is a good place to encode "still under factory warranty" (e.g. 36,000 for a 3-year/36k warranty).

**New cars** are off by default. To rank new cars alongside used ones, add `"conditions": ["used", "new"]` (or
`["new"]` for new only) and give new cars their own prices in `fair_value.new` (step 5). Then save a
**new-car** search on each site too (most sites keep new and used in separate searches; Cars.com and Autotrader
alerts name the search "Used …"/"New …", which the tracker reads). A listing is new when it says so ("New 2026 …",
"Condition: New", an MSRP) or comes from a new-only search; when nothing says, it counts as used.

`python -m tracker check` validates the config and prints what it compiled. A typo in a section or key name is
reported with the exact key instead of crashing mid-scan.

## 4. Check the parser understands your listings

Paste a real listing title + details from any site:

```bash
python -m tracker check --text "2024 Honda CR-V Hybrid Sport Touring AWD \$34,250 18,400 mi Moonroof Smyrna Honda, Smyrna, GA"
```

```
{"year": 2024, "trim_ok": true, "price": 34250, "miles": 18400, "city": "Smyrna, GA", "dealer": "Smyrna Honda",
 "features": {"awd": true, "sunroof": true}, ...}
   -> MATCH: all criteria known
      fair value $33,790, all-in $35,109, deal score -1,319
```

Once a few alert emails have arrived, run them through too. In Gmail: open the email → ⋮ → **Download message**
saves a `.eml`:

```bash
python -m tracker parse data/cargurus-alert.eml
```

If a trim, package or body style is missed, add its wording to the config and re-run. If the email layout itself
is the problem (prices on the wrong car, a city taken from a footer), that's a parser fix in `tracker/parse.py`;
add a test in `tests/test_parse.py` with the email's wording.

## 5. Set the fair-value numbers

The deal score is only as good as these numbers, and they're market- and model-specific. Spend 20 minutes on them.

```json
"fair_value": {
  "base": {"2023": 31000, "2024": 33500, "2025": 36000},
  "per_year": 2500,
  "ref_miles": 15000,
  "per_mile": 0.15,
  "cpo": 800,
  "accident": 3000,
  "fee_estimate": 799,
  "trip_per_mile": 0.5
}
```

| Key | Meaning | How to pick it |
|---|---|---|
| `base` | Typical asking price per model year at `ref_miles`, with your trim and must-haves | Median of 10-20 real listings per year near `ref_miles` (CarGurus "market value" helps) |
| `per_year` | Used for years missing from `base` | Gap between adjacent years in `base` |
| `ref_miles` | Mileage the `base` prices assume | Typical mileage of the listings you sampled |
| `per_mile` | Value lost per extra mile (gained per mile under) | Compare similar listings with different miles; $0.10–0.25 is common |
| `cpo` | Worth of certified pre-owned | The extended warranty's cost, discounted |
| `accident` | Discount for a reported accident | 5–10% of price is a common rule of thumb |
| `fee_estimate` | Dealer doc fee added when the price doesn't include fees | Your state's typical doc fee |
| `trip_per_mile` | Cost per mile of distance to go get the car | Fuel + your time, round trip |

New cars (only with `"new"` in `criteria.conditions`) get their own fair value; miles, CPO and accidents don't
apply to them:

```json
"fair_value": {
  ...,
  "new": {"base": {"2025": 40000, "2026": 41500}, "msrp_discount": 0.10}
}
```

| Key | Meaning | How to pick it |
|---|---|---|
| `new.base` | Typical selling price of a new one per model year, with your trim and must-haves (feature `value`s are added on top) | Advertised prices of 10-20 new listings after dealer discounts and rebates, before fees and tax |
| `new.msrp_discount` | When a listing quotes an MSRP, fair value is MSRP × (1 − this) instead; the MSRP already includes the packages | Typical discount off sticker in those listings; leave it out to always use `base` |
| `new.per_year` | Optional; years missing from `new.base` (defaults to `per_year`) | |

**Deal score** = fair value − (price + fees + distance × `trip_per_mile`). Positive means priced under fair value;
`alerts.good_deal_score` sets the threshold for high-priority pushes.

**Ranking** (`ranking`) is the deal score minus penalties for what the email didn't say: `unknown_feature` per
unknown must-have, `unknown_miles`, an unknown location counted as `unknown_distance` miles away, and
`missing_feature` / `wrong_body` for confirmed mismatches you've kept around. Listings not seen in an alert for
`stale_days` are marked stale; after `expire_days` they're archived (starred ones never are).

## 6. Distances from home

The tracker doesn't geocode. It looks up the listing's city in `distances` (substring match, lower case) and falls
back to a typical distance for the state in `state_distances`:

```json
"distances": {"online": 0, "atlanta": 5, "marietta": 20, "athens": 70},
"state_distances": {"GA": 120, "TN": 220, "AL": 180}
```

Fill in the 15-30 towns where most dealers near you are; `online: 0` covers Carvana-style delivery. Unknown places
count as `ranking.unknown_distance` miles until you set them on the dashboard.

## 7. Phone alerts with ntfy

1. Install the ntfy app (iOS / Android) and subscribe to a topic with a long random name.
2. Put that name in `.env` as `NTFY_TOPIC`. Anyone who knows the name can read the alerts, so make it unguessable.
3. `python -m tracker notify-test`

Without a topic the tracker still works; pushes are just written to the log.

## 8. Run it

```bash
python -m tracker scan --dry-run    # parse recent alert emails, print, store nothing
python -m tracker scan              # store + push (first run: --days 14 to backfill)
python -m tracker report            # ranked list in the terminal
python -m tracker serve             # dashboard at http://localhost:8095
```

Schedule `scan` every few hours with cron (Linux/macOS) or Task Scheduler (Windows):

```
0 7,10,13,16,19,22 * * * cd ~/car-deal-tracker && python3 -m tracker scan >> data/scan.log 2>&1
```

`config.json`, `.env` and `data/` are gitignored, so `git pull` never touches your settings or database.

## 9. Run it on a server (optional)

To get alerts while your laptop is off, run it on any always-on Linux box (home server, Raspberry Pi, small VPS)
with Python 3.10+, SSH access and Docker for the dashboard:

```bash
# in .env
DEPLOY_HOST=me@192.168.1.20
SCAN_CRON=0 7,10,13,16,19,22 * * *     # server clock; check `date` on the server (often UTC)

scripts/deploy.sh --env      # first time: code, config, .env, cron job, dashboard container
scripts/deploy.sh            # later updates
scripts/deploy.sh --no-docker   # cron scan only
```

The dashboard listens on port 8095 on your LAN. To open it from your phone anywhere without exposing it to the
internet, set `TS_AUTHKEY` (a one-off key from the Tailscale admin console) and deploy again: a Tailscale sidecar
serves it at `https://cars.<your-tailnet>.ts.net`. Setting `WEB_TOKEN` adds a password: open `/?token=...` once
per device.

## 10. When a site changes its emails

Car sites redesign their alert emails without notice. The tracker pushes **"couldn't read <site> alert"** when an
email from a car site names your model but no listing comes out of it. To fix:

1. Download that email as `.eml` into `data/` and run `python -m tracker parse data/<file>.eml`.
2. Fix `tracker/parse.py` and add a test with the email's wording to `tests/test_parse.py`.
3. `python -m tracker scan --reprocess --days 7` re-reads recent emails with the fixed parser.

Wrong values that do get through (a price that belongs to the next car, a dealer that's really footer text) can be
flagged with **⚑ Wrong info** on the dashboard. `python -m tracker reports --fetch` saves each flagged listing's
source email for the same fix-and-test loop, and `rebuild --days 21` re-derives listings afterwards while keeping
your stars, notes and decisions.
