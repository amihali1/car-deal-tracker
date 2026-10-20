"""Extract listings for the configured car from alert emails or pasted listing text.

Alert emails differ per site and change without notice, so parsing is heuristic:
1. Flatten HTML to text, remembering where each link sits.
2. Split the text into one chunk per vehicle (by listing links when present, else by
   "<year> <make> <model>" headings).
3. Pull fields out of each chunk with regexes.
Anything the regexes miss stays None, and value.matches() reports it as unknown.

Model-specific patterns (heading, trims, body styles, packages, VIN prefix) come from the
Profile compiled from config.json; this module only knows how car-site emails are laid out.
"""
import re
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser

SOURCES = {
    "facebook": ("facebookmail.com", "facebook.com"),
    "cargurus": ("cargurus.com",),
    "cars.com": ("cars.com",),
    "autotrader": ("autotrader.com",),
    "carvana": ("carvana.com",),
    "carmax": ("carmax.com",),
    "truecar": ("truecar.com",),
    "edmunds": ("edmunds.com",),
}

# Direct listing URLs. Alert emails often wrap these in click-tracking redirects, in which
# case nothing matches and chunking falls back to headings.
LISTING_URL = re.compile(
    r"https?://[^\s\"'<>]*?("
    r"facebook\.com/marketplace/item/(?P<fb>\d+)"
    r"|carvana\.com/vehicle/(?P<carvana>\d+)"
    r"|cars\.com/vehicledetail/(?P<carscom>\d+)"
    r"|autotrader\.com/cars-for-sale/vehicle/(?P<autotrader>\d+)"
    r"|cargurus\.com/[^\s\"'<>]*?(?:listing[=/]|inventorylisting/[^\s\"'<>]*?#listing=)(?P<cargurus>\d+)"
    r"|carmax\.com/car/(?P<carmax>\d+)"
    r")[^\s\"'<>]*",
    re.I,
)

# A price sitting right before a "<year> <make> <model>" heading (CarGurus "just for you" emails
# put each car's price above its title). Only the fee phrase may sit in between.
PRICE_LEAD = re.compile(r"\$\s?\d{1,3},?\d{3}(?:\s*(?:includes|no additional)\s+(?:dealer\s+)?fees)?\s*$", re.I)
PRICE = re.compile(r"\$\s?(\d{1,3}),?(\d{3})(?!\d)")
MILES = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k)?\s*(?:mi\b|miles\b)", re.I)
# Labeled odometer ("Mileage: 8,265", "Odometer 12,000") wins over loose "N miles" matches.
MILEAGE_LABEL = re.compile(r"\b(?:mileage|odometer)\s*:?\s*(\d{1,3}(?:,\d{3})+|\d+)\b", re.I)
# Alert-email footers that follow the last vehicle ("Search radius: 200 miles" etc.).
# Footers carry the site's HQ address (Cars.com Chicago, Autotrader Atlanta, CarGurus Boston), which
# must not become the car's city.
FOOTER = re.compile(r"View all listings|Your search criteria|You requested these alerts|Search radius"
                    r"|See More Matches|This email was sent|Download (?:our|the \w+) app|All rights reserved", re.I)
STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
          "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
CITY_ST = re.compile(r"\b([A-Z][a-zA-Z.\s]{2,30}?),\s*(" + "|".join(STATES) + r")\b")
STREET_TAIL = re.compile(r"\b(?:Street|St|Avenue|Ave|Road|Rd|Court|Ct|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Parkway|Pkwy|"
                         r"Highway|Hwy|Pike|Place|Pl|Circle|Cir|Trail|Tr)\.?\s+")
# National used-car chains name the store by city: "CarMax Naperville".
CHAIN = re.compile(r"\b(?:CarMax|Carvana|DriveTime|AutoNation)(?:\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)?\b")
# New vs used. Sites put the condition right before the title ("New 2026 Nissan Frontier",
# "Used 2025 ..."), or as a "Condition: New" label.
CONDITION_LEAD = re.compile(r"\b(new|used|certified(?:\s+pre-?owned)?|pre-?owned)\s+$", re.I)
CONDITION_LABEL = re.compile(r"\bcondition\s*:?\s*(new|used|certified|pre-?owned)\b", re.I)
# The saved search's own condition: Cars.com names it "Used Nissan Frontier for Sale" and lists filters
# as "• Used •"; Autotrader footers say "| Used |".
SEARCH_CONDITION = re.compile(r"[•|]\s*(new|used)\s*[•|]|\b(new|used) [\w\- ]{1,30}? for sale\b", re.I)
# "MSRP $45,995" or "$45,995 MSRP". Never the price; "Original MSRP" (group 1) on a used listing isn't an MSRP either.
MSRP = re.compile(r"(original\s+)?\bMSRP\s*:?\s*\$\s?(\d{1,3}),?(\d{3})(?!\d)|\$\s?(\d{1,3}),?(\d{3})\s*MSRP\b", re.I)


class _Flattener(HTMLParser):
    """HTML -> text, plus (text_offset, href) for every link."""

    BLOCK = {"p", "div", "br", "tr", "td", "li", "h1", "h2", "h3", "h4", "table", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self._len, self._skip = [], [], 0, 0

    def _emit(self, s):
        self.parts.append(s)
        self._len += len(s)

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._skip += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append((self._len, unescape(href)))
        if tag in self.BLOCK:
            self._emit("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head") and self._skip:
            self._skip -= 1
        if tag in self.BLOCK:
            self._emit("\n")

    def handle_data(self, data):
        if not self._skip:
            self._emit(data)


def html_to_text(html):
    p = _Flattener()
    p.feed(html)
    text = "".join(p.parts)
    return text, p.links


def plain_links(text):
    return [(m.start(), m.group(0)) for m in re.finditer(r"https?://\S+", text)]


def detect_source(sender_or_text):
    s = (sender_or_text or "").lower()
    for name, needles in SOURCES.items():
        if any(n in s for n in needles):
            return name
    return "manual"


def _num(s):
    return int(s.replace(",", ""))


def parse_chunk(chunk, profile):
    """Fields from one vehicle's worth of text. Returns dict (values may be None)."""
    c = " ".join(chunk.split())
    m = FOOTER.search(c)
    if m:
        c = c[:m.start()]
    # The wanted model and trim. A chunk that never names the model is some other car
    # (a "Titan PRO-4X" in a Frontier search), even if the trim name matches.
    model_ok = bool(profile.mentions.search(c))
    out = {"year": None, "trim_ok": model_ok and profile.trim_ok(c), "price": None, "miles": None,
           "vin": None, "city": None, "dealer": None, "body": None, "features": {},
           "cpo": False, "accident": None, "fees_included": False, "condition": None, "msrp": None}

    m = profile.heading.search(c) or re.search(r"\b(19[89]\d|20[0-4]\d)\b", c)
    if m:
        out["year"] = int(m.group(1))

    msrps = list(MSRP.finditer(c))
    # Price: first $xx,xxx in the configured plausible range. "was $X" / "price drop of $X" / MSRPs skipped.
    for m in PRICE.finditer(c):
        before = c[max(0, m.start() - 20):m.start()].lower()
        if re.search(r"(was|drop(?:ped)?(?: of)?|save|off|below|under|over|est\.?|/mo|monthly)\s*$", before):
            continue
        if any(x.start() <= m.start() < x.end() for x in msrps):
            continue
        val = int(m.group(1) + m.group(2))
        if profile.price_min <= val <= profile.price_max:
            out["price"] = val
            break

    for m in msrps:
        val = int((m.group(2) or m.group(4)) + (m.group(3) or m.group(5)))
        if not m.group(1) and profile.price_min <= val <= profile.price_max:
            out["msrp"] = val
            out["price"] = out["price"] or val  # no discounted price shown: MSRP is the asking price
            break

    m = MILEAGE_LABEL.search(c)
    if m:
        out["miles"] = _num(m.group(1))
    for m in [] if out["miles"] is not None else MILES.finditer(c):
        raw, k = m.group(1), m.group(2)
        before = c[max(0, m.start() - 12):m.start()].lower()
        after = c[m.end():m.end() + 8].lower()
        if re.match(r"\s*(away|from|of|radius)", after) or re.search(r"(within|radius)\s*:?\s*$", before):
            continue  # search radius / distance ("200 miles from 30303"), not odometer
        val = float(raw.replace(",", "")) * (1000 if k else 1)
        if val < 10 and not k:
            continue
        out["miles"] = int(val)
        break

    m = profile.vin.search(c)
    if m:
        out["vin"] = m.group(1).upper()

    m = CITY_ST.search(c)
    if m:
        # "3320 Odyssey Court Naperville, IL" -> keep only what follows the street type.
        city = STREET_TAIL.split(m.group(1).strip())[-1].strip()
        out["city"] = f"{city}, {m.group(2)}"
    m = CHAIN.search(c)
    if m:
        out["dealer"] = m.group(0).strip()
    else:
        for m in profile.dealer.finditer(c):
            if not profile.junk_dealer.search(m.group(1)):  # email footers ("Privacy Statement. New Cars")
                out["dealer"] = m.group(1).strip()
                break

    low = c.lower()
    out["body"] = profile.body_of(c)
    out["features"] = profile.features_of(c)
    out["cpo"] = bool(profile.certified.search(c))
    out["condition"] = condition_of(c, profile, out)
    if re.search(r"no accidents?|0 accidents|accident[- ]free|clean (carfax|history)", low):
        out["accident"] = False
    elif re.search(r"accident reported|\d+ accidents?|damage reported", low):
        out["accident"] = True
    out["fees_included"] = bool(re.search(r"(includes|including|incl\.?)\s+(all\s+)?(dealer\s+)?fees"
                                          r"|no additional (dealer )?fees", low))
    return out


def condition_of(c, profile, fields):
    """'new', 'used' or None (unknown)."""
    m = None
    h = profile.heading.search(c)
    if h:
        m = CONDITION_LEAD.search(c[max(0, h.start() - 30):h.start()])
    m = m or CONDITION_LABEL.search(c)
    if m:
        return "new" if m.group(1).lower() == "new" else "used"
    if fields["cpo"]:
        return "used"
    if fields["msrp"]:
        return "new"  # used listings don't quote an MSRP
    return None


def search_condition(text):
    """'new' or 'used' when the saved search the email is for asks for only one ("Used Nissan Frontier
    for Sale", "200 mi of 30303 • Used • Min year"), for listings that don't say. Else None."""
    if re.search(r"\bnew\s*(?:and|&|/|or)\s*used\b", text, re.I):
        return None
    found = {(a or b).lower() for a, b in SEARCH_CONDITION.findall(text)}
    return found.pop() if len(found) == 1 else None


def _listing_id(url):
    m = LISTING_URL.search(url or "")
    if not m:
        return None
    for k, v in m.groupdict().items():
        if v:
            return v
    return None


def split_listings(text, links, profile):
    """Yield (chunk_text, url_or_None) per vehicle."""
    anchors = []
    seen = set()
    for off, href in links:
        m = LISTING_URL.search(href)
        if m and m.group(0) not in seen:
            seen.add(m.group(0))
            anchors.append((off, m.group(0)))

    heads = []
    for m in profile.heading.finditer(text):
        # Start at the condition word that leads the title, so "Used" doesn't end the previous car's chunk.
        s = m.start()
        c = CONDITION_LEAD.search(text[max(0, s - 30):s])
        heads.append(max(0, s - 30) + c.start() if c else s)
    if heads and PRICE_LEAD.search(text[max(0, heads[0] - 120):heads[0]]):
        # Price-above-title layout: start each chunk at the price that leads its heading,
        # otherwise every car gets the next car's price.
        moved = []
        for s in heads:
            m = PRICE_LEAD.search(text[max(0, s - 120):s])
            moved.append(max(0, s - 120) + m.start() if m else s)
        heads = moved
    if not heads:
        if anchors:
            # No "<year> <make> <model>" headings: one chunk per listing link.
            for i, (s, url) in enumerate(anchors):
                e = anchors[i + 1][0] if i + 1 < len(anchors) else min(len(text), s + 1500)
                yield text[s:e], url
        else:
            yield text, None
        return

    # One chunk per heading. A link belongs to the chunk it falls in; the small slack
    # covers <a> tags that open just before the heading text they wrap.
    def owner(off):
        idx = None
        for i, s in enumerate(heads):
            if s <= off + 5:
                idx = i
        return idx

    urls = {}
    for off, url in anchors:
        i = owner(off)
        if i is not None:
            urls.setdefault(i, url)
    for off, href in links:  # fallback: any link (e.g. click-tracking redirect)
        i = owner(off)
        if i is not None and href.startswith("http"):
            urls.setdefault(i, href)

    for i, s in enumerate(heads):
        e = heads[i + 1] if i + 1 < len(heads) else min(len(text), s + 1500)
        yield text[s:e], urls.get(i)


def parse_text(text, profile, source="manual", links=None):
    links = links if links is not None else plain_links(text)
    results = []
    for chunk, url in split_listings(text, links, profile):
        fields = parse_chunk(chunk, profile)
        if fields["year"] is None and fields["price"] is None:
            continue
        fields["source"] = source
        fields["url"] = url
        fields["listing_id"] = _listing_id(url)
        if source == "carvana":
            fields["fees_included"] = True
            fields["city"] = fields["city"] or "Online"
            fields["dealer"] = fields["dealer"] or "Carvana"
        results.append(fields)
    return results


def parse_email_bytes(raw, profile):
    """Parse an RFC822 message. Returns (meta, listings)."""
    msg = message_from_bytes(raw, policy=policy.default)
    sender = str(msg.get("From", ""))
    meta = {
        "message_id": str(msg.get("Message-ID", "")).strip(),
        "sender": sender,
        "subject": str(msg.get("Subject", "")),
        "date": str(msg.get("Date", "")),
    }
    source = detect_source(sender)
    html_part = msg.get_body(preferencelist=("html",))
    if html_part is not None:
        text, links = html_to_text(html_part.get_content())
    else:
        body = msg.get_body(preferencelist=("plain",))
        text = body.get_content() if body is not None else ""
        links = plain_links(text)
    meta["source"] = source
    try:
        meta["day"] = parsedate_to_datetime(meta["date"]).astimezone().date().isoformat()
    except (TypeError, ValueError):
        meta["day"] = None
    prefix = meta["subject"] + "\n"  # subject lines often carry the price drop / model
    links = [(off + len(prefix), href) for off, href in links]
    meta["mentions_model"] = bool(profile.mentions.search(prefix + text))
    listings = parse_text(prefix + text, profile, source, links)
    search = search_condition(prefix + text)
    for l in listings:
        l["condition"] = l["condition"] or search
        l["email_id"] = meta["message_id"] or None
        l["seen"] = meta["day"]  # stored as first/last seen, so a re-scan doesn't make old alerts look fresh
    return meta, listings
