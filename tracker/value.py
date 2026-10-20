"""Fair-value model and search criteria, driven by config.json.

score = fair value - all-in cost; positive means the car is priced under fair value.
"""
import math
import re

from .profile import profile_for


def round(x):  # noqa: A001 - half-up rounding (JS Math.round), so the dashboard and CLI agree
    return math.floor(x + 0.5)


def base_price(year, fv):
    """Typical price at ref_miles for a model year. Years missing from the table are extrapolated
    from the nearest listed year by `per_year` (default $2,000/yr)."""
    table = {int(y): v for y, v in fv["base"].items()}
    if year is None:
        year = max(table)
    if year in table:
        return table[year]
    nearest = min(table, key=lambda y: abs(y - year))
    return table[nearest] + (year - nearest) * fv.get("per_year", 2000)


def is_new(listing, cfg):
    """New cars get their own fair value (fair_value.new); unknown condition counts as used."""
    return listing.get("condition") == "new" and bool(cfg["fair_value"].get("new"))


def new_fair_value(listing, cfg):
    """A new car's fair price: the listed MSRP less the usual discount when the listing quotes an MSRP
    (it already prices in the packages), else the per-year table for new cars plus package values."""
    fv = cfg["fair_value"]
    nv = fv["new"]
    if listing.get("msrp") and nv.get("msrp_discount") is not None:
        return round(listing["msrp"] * (1 - nv["msrp_discount"]))
    value = base_price(listing.get("year"), {"base": nv["base"], "per_year": nv.get("per_year", fv.get("per_year", 2000))})
    feats = listing.get("features") or {}
    for key, f in cfg.get("features", {}).items():
        if feats.get(key):
            value += f.get("value", 0)
    return round(value)


def fair_value(listing, cfg):
    fv = cfg["fair_value"]
    if is_new(listing, cfg):
        return new_fair_value(listing, cfg)
    value = base_price(listing.get("year"), fv)
    miles = listing.get("miles")
    if miles is None:
        miles = fv["ref_miles"]
    value -= (miles - fv["ref_miles"]) * fv["per_mile"]
    feats = listing.get("features") or {}
    for key, f in cfg.get("features", {}).items():
        if feats.get(key):
            value += f.get("value", 0)
    if listing.get("cpo"):
        value += fv.get("cpo", 0)
    if listing.get("accident"):
        value -= fv.get("accident", 0)
    return round(value)


def fees(listing, cfg):
    return 0 if listing.get("fees_included") else cfg["fair_value"]["fee_estimate"]


def trip_cost(listing, cfg):
    return round((listing.get("distance") or 0) * cfg["fair_value"]["trip_per_mile"])


def all_in(listing, cfg):
    return listing["price"] + fees(listing, cfg) + trip_cost(listing, cfg)


def score(listing, cfg):
    return fair_value(listing, cfg) - all_in(listing, cfg)


def distance_for(city, distances, state_distances=None):
    """Rough driving miles from home, via substring match on the config table, else a typical
    distance for the state ("Kernersville, NC"). None if unknown."""
    if not city:
        return None
    c = city.lower()
    # Longest key first so "east ridge" beats "ridge"-style partial collisions.
    for key in sorted(distances, key=len, reverse=True):
        if key in c:
            return distances[key]
    m = re.search(r",\s*([A-Z]{2})\b", city)
    if m and state_distances:
        return state_distances.get(m.group(1))
    return None


def matches(listing, cfg):
    """Return (ok, reasons). Unknown values pass but are reported, since alert emails are terse."""
    criteria, profile = cfg["criteria"], profile_for(cfg)
    reasons = []
    year = listing.get("year")
    if year is None or year < criteria["min_year"]:
        return False, [f"year {year}"]
    if criteria.get("max_year") and year > criteria["max_year"]:
        return False, [f"year {year}"]
    if not listing.get("trim_ok"):
        return False, [f"not {'/'.join(profile.trims)}"]
    condition = listing.get("condition")
    if condition and condition not in criteria.get("conditions", ["used"]):
        return False, [condition]
    body = listing.get("body")
    if body and profile.body_want and body not in profile.body_want:
        return False, [profile.body_label(body)]
    miles = listing.get("miles")
    if miles is not None and miles > criteria["max_miles"]:
        return False, [f"{miles:,} mi"]
    price = listing.get("price")
    if price and criteria.get("max_price") and price > criteria["max_price"]:
        return False, [f"${price:,}"]
    feats = listing.get("features") or {}
    for key, f in profile.features.items():
        if f["required"] and feats.get(key) is False:
            return False, [f"no {f['short']}"]
    if listing.get("distance") is None:
        reasons.append("distance unknown")
    if body is None and profile.body_want:
        reasons.append("body unknown")
    if miles is None and not is_new(listing, cfg):
        reasons.append("miles unknown")
    for key, f in profile.features.items():
        if f["required"] and feats.get(key) is None:
            reasons.append(f"{f['short']} unknown")
    return True, reasons


def rank(listing, cfg):
    """Ranking score: the deal score, made pessimistic about whatever the alert email didn't say.

    A cheap car with unknown options often turns out to lack the package you need, so unknowns
    cost points instead of quietly counting as good news. Returns (rank_score, flags).
    """
    ranking, profile = cfg["ranking"], profile_for(cfg)
    flags = []
    l = dict(listing)
    penalty = 0
    if l.get("distance") is None:
        l["distance"] = ranking["unknown_distance"]
        flags.append("distance?")
    if l.get("miles") is None and not is_new(l, cfg):  # a new car's odometer doesn't move its value
        penalty += ranking["unknown_miles"]
        flags.append("miles?")
    feats = l.get("features") or {}
    for key, f in profile.features.items():
        if not f["required"]:
            continue
        if feats.get(key) is None:
            penalty += ranking["unknown_feature"]
            flags.append(f"{f['short']}?")
        elif not feats[key]:
            penalty += ranking["missing_feature"]
            flags.append(f"no {f['short']}")
    body = l.get("body")
    if body and profile.body_want and body not in profile.body_want:
        penalty += ranking.get("wrong_body", ranking["missing_feature"])
        flags.append(profile.body_label(body))
    return score(l, cfg) - penalty, flags
