"""The car being hunted, compiled from config.json into the regexes the parser needs.

Everything model-specific lives here: the "2025 Nissan Frontier" heading, wanted vs other trims,
body styles (crew cab, hatchback...), optional packages, VIN prefix, plausible prices.
"""
import re

YEAR = r"(19[89]\d|20[0-4]\d)"
# Common words in dealer names; the configured make is added on top.
DEALER_WORDS = ["Motors", "Auto(?:motive)?", "Cars", "Chevrolet", "Ford", "Toyota", "Honda", "Buick GMC(?: Cadillac)?",
                "Kia", "Hyundai", "Dodge", "Jeep", "Chrysler", "CDJR", "Subaru", "Mazda", "Volkswagen", "Nissan",
                "Lexus", "Acura", "Infiniti", "BMW", "Mercedes-Benz", "Audi", "Volvo", "Lincoln", "GMC", "Ram",
                "Mitsubishi", "Genesis", "Porsche", "Cadillac"]


def phrase(text):
    """'PRO-4X' -> regex matching 'PRO-4X', 'Pro 4X', 'pro4x'; whole words only."""
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", text.strip()) if p]
    return r"(?<![A-Za-z0-9])" + r"[\s\-]?".join(parts) + r"(?![A-Za-z0-9])"


def _any(patterns, whole_words=False):
    """One case-insensitive regex for a list of patterns. whole_words wraps each so "awd" doesn't
    match inside "Hawd..." -- config authors then never need \\b (which JSON makes awkward)."""
    if not patterns:
        return None
    wrap = (lambda p: rf"(?<![A-Za-z0-9])(?:{p})(?![A-Za-z0-9])") if whole_words else (lambda p: f"(?:{p})")
    return re.compile("|".join(wrap(p) for p in patterns), re.I)


class Profile:
    def __init__(self, cfg):
        v = cfg["vehicle"]
        self.label = v["label"]
        self.make, self.model = v["make"], v["model"]
        self.icon = v.get("icon", "🚗")
        self.ntfy_tag = v.get("ntfy_tag", "car")
        self.price_min, self.price_max = v.get("price_range", [3000, 300000])
        self.trims = v.get("trims", [])

        # "2025 Nissan Frontier" / "Used 2025 Frontier": the make is optional, the model isn't.
        self.heading = re.compile(rf"\b{YEAR}\s+(?:{phrase(self.make)}\s+)?{phrase(self.model)}", re.I)
        self.mentions = re.compile(phrase(self.model), re.I)
        self.want_trim = _any([phrase(t) for t in self.trims])
        self.other_trim = _any([phrase(t) for t in v.get("other_trims", [])])

        prefixes = v.get("vin_prefixes") or []
        if prefixes:
            self.vin = re.compile(r"\b(" + "|".join(
                f"{re.escape(p.upper())}[A-HJ-NPR-Z0-9]{{{17 - len(p)}}}" for p in prefixes) + r")\b")
        else:  # without a prefix, only trust 17 characters labelled as a VIN
            self.vin = re.compile(r"\bVIN\s*[:#]?\s*([A-HJ-NPR-Z0-9]{17})\b", re.I)

        certified = [r"certified pre-?owned", r"\bcpo\b"] + [re.escape(p) for p in v.get("certified_phrases", [])]
        self.certified = re.compile("|".join(certified), re.I)

        words = [re.escape(self.make)] + DEALER_WORDS
        self.dealer = re.compile(r"\b([A-Z][\w&'.\- ]{2,40}?(?:" + "|".join(words) + r"))\b")
        self.junk_dealer = re.compile(
            r"privacy|conditions|terms|statement|unsubscribe|copyright|rights reserved"
            r"|view details|search|wallet|dealer fees|price drop|includes"
            rf"|^(?:new|used|certified|pre-?owned)(?: \d{{4}})? (?:cars|{re.escape(self.make)})$", re.I)  # nav, titles

        body = cfg.get("body") or {}
        self.body_want = body.get("want", [])
        self.body_styles = {k: (s.get("label", k), _any(s.get("patterns", []), whole_words=True))
                            for k, s in body.get("styles", {}).items()}

        self.features = {}
        for key, f in cfg.get("features", {}).items():
            self.features[key] = {
                "label": f.get("label", key), "short": f.get("short", f.get("label", key)),
                "yes": _any(f["yes"], whole_words=True), "no": _any(f.get("no", []), whole_words=True),
                "required": bool(f.get("required")), "value": f.get("value", 0),
            }

    def trim_ok(self, chunk):
        """Is this the wanted trim? The title right after the heading wins: text further down a
        chunk can belong to a neighbouring car or the saved-search name. No trims configured = any."""
        if not self.want_trim:
            return True
        h = self.heading.search(chunk)
        if h:
            title = chunk[h.end():h.end() + 40]
            if self.want_trim.search(title):
                return True
            if self.other_trim and self.other_trim.search(title):
                return False
        return bool(self.want_trim.search(chunk))

    def body_of(self, text):
        for key, (_, rx) in self.body_styles.items():
            if rx and rx.search(text):
                return key
        return None

    def features_of(self, text):
        out = {}
        for key, f in self.features.items():
            if f["no"] and f["no"].search(text):
                out[key] = False
            elif f["yes"].search(text):
                out[key] = True
            else:
                out[key] = None
        return out

    def body_label(self, key):
        return self.body_styles.get(key, (key,))[0] if key else None

    def summary(self):
        """Human-readable dump for `python -m tracker check`."""
        lines = [
            f"Hunting: {self.label} ({self.make} {self.model})",
            f"  listing heading regex: {self.heading.pattern}",
            f"  wanted trims: {', '.join(self.trims) or 'any'}",
            f"  price range accepted: ${self.price_min:,} - ${self.price_max:,}",
            f"  VIN regex: {self.vin.pattern}",
        ]
        if self.body_styles:
            lines.append("  body styles: " + ", ".join(
                f"{k} ({lbl}{', wanted' if k in self.body_want else ''})" for k, (lbl, _) in self.body_styles.items()))
        for key, f in self.features.items():
            tag = "required" if f["required"] else f"+${f['value']:,} fair value" if f["value"] else "info only"
            lines.append(f"  feature {key}: {f['label']} [{tag}]")
        return "\n".join(lines)


def profile_for(cfg):
    """Compile once per config dict."""
    p = cfg.get("_profile")
    if p is None:
        p = cfg["_profile"] = Profile(cfg)
    return p
