"""Config loading. Secrets come from env vars or a gitignored .env file, never config.json."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    pass


def config_path(path=None):
    """Explicit path, else $TRACKER_CONFIG, else ROOT/config.json."""
    if path:
        return Path(path)
    if os.environ.get("TRACKER_CONFIG"):
        return Path(os.environ["TRACKER_CONFIG"])
    return ROOT / "config.json"


def load_config(path=None):
    path = config_path(path)
    if not path.exists():
        raise ConfigError(f"{path} not found. Copy config.example.json to config.json and edit it "
                          "for your car (see docs/SETUP.md).")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    validate(cfg)
    cfg["db_path"] = str(ROOT / cfg.get("db_path", "data/tracker.db"))
    # Identifying values (mailbox address, ntfy topic) live in .env so the repo stays clean.
    for account in cfg.get("imap", {}).get("accounts", []):
        if "user_env" in account:
            account["user"] = secret(account["user_env"]) or f"<{account['user_env']} unset>"
    ntfy = cfg.get("ntfy", {})
    if "topic_env" in ntfy:
        ntfy["topic"] = secret(ntfy["topic_env"]) or ""
    return cfg


REQUIRED = {
    "vehicle": ("label", "make", "model"),
    "criteria": ("min_year", "max_miles"),
    "fair_value": ("base", "ref_miles", "per_mile", "fee_estimate", "trip_per_mile"),
    "ranking": ("unknown_distance", "unknown_miles", "unknown_feature", "missing_feature", "stale_days",
                "expire_days"),
    "alerts": ("min_price_drop", "good_deal_score"),
}


def validate(cfg):
    """Fail early, with the config key in the message, instead of a KeyError mid-scan."""
    problems = []
    for section, keys in REQUIRED.items():
        if section not in cfg:
            problems.append(f"missing section '{section}'")
            continue
        problems += [f"missing '{section}.{k}'" for k in keys if k not in cfg[section]]
    if not cfg.get("fair_value", {}).get("base"):
        problems.append("'fair_value.base' needs at least one \"year\": price entry")
    for name, feat in cfg.get("features", {}).items():
        if not feat.get("yes"):
            problems.append(f"feature '{name}' needs a 'yes' list of phrases")
    body = cfg.get("body") or {}
    for style in body.get("want", []):
        if style not in body.get("styles", {}):
            problems.append(f"body.want lists '{style}' but body.styles has no such key")
    if problems:
        raise ConfigError("config problems:\n  - " + "\n  - ".join(problems))


def load_dotenv():
    """Populate os.environ from ROOT/.env (KEY=value lines). Existing env vars win."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def secret(name):
    load_dotenv()
    return os.environ.get(name)
