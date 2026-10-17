"""Fetch recent alert emails over IMAP (Gmail app passwords, one per account)."""
import imaplib
import logging
from datetime import date, timedelta

from . import db
from .config import secret
from .parse import parse_email_bytes
from .profile import profile_for

log = logging.getLogger(__name__)


def fetch_account(imap_cfg, account, lookback_days, query=None):
    """Yield raw RFC822 bytes for messages matching the account's Gmail query (or `query`)."""
    password = secret(account["password_env"])
    if account["user"].startswith("<"):
        raise RuntimeError(f"mailbox address not configured: {account['user']}")
    if not password:
        raise RuntimeError(f"{account['password_env']} not set (.env or environment)")
    since = (date.today() - timedelta(days=lookback_days)).strftime("%Y/%m/%d")
    query = query or f'{account["gmail_query"]} after:{since}'
    with imaplib.IMAP4_SSL(imap_cfg["host"]) as m:
        m.login(account["user"], password)
        m.select(imap_cfg["mailbox"], readonly=True)
        # X-GM-RAW = Gmail search syntax over IMAP.
        typ, data = m.uid("SEARCH", "X-GM-RAW", f'"{query}"')
        if typ != "OK":
            log.error("%s search failed: %s", account["user"], data)
            return
        uids = data[0].split()
        log.info("%s: %d messages match", account["user"], len(uids))
        for uid in uids:
            typ, msg = m.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ == "OK" and msg and isinstance(msg[0], tuple):
                yield msg[0][1]


def scan(cfg, conn, lookback_days=None, reprocess=False, dry_run=False):
    """Parse new alert emails from every account.

    Returns (listings, unreadable, errors):
      unreadable = [(source, subject)] car-site emails naming the model where no listing was found
      errors     = [str] login/connection/config failures, one per account
    reprocess=True re-reads already-processed emails (after a parser fix).
    """
    imap_cfg = cfg["imap"]
    profile = profile_for(cfg)
    days = lookback_days or imap_cfg["lookback_days"]
    found, unreadable, errors = [], [], []
    for account in imap_cfg["accounts"]:
        try:
            for raw in fetch_account(imap_cfg, account, days):
                meta, listings = parse_email_bytes(raw, profile)
                if not meta["message_id"]:
                    continue
                if not reprocess and db.email_seen(conn, account["user"], meta["message_id"]):
                    continue
                log.info("[%s] %s -> %d listing(s)", meta["source"], meta["subject"][:70], len(listings))
                found.extend(listings)
                if not listings and meta["source"] != "manual" and meta["mentions_model"]:
                    unreadable.append((meta["source"], meta["subject"]))
                if not dry_run:
                    db.mark_email(conn, account["user"], meta, len(listings))
        except (imaplib.IMAP4.error, OSError, RuntimeError) as e:
            log.error("mail scan failed for %s: %s", account["user"], e)
            errors.append(f"{account['user'].split('@')[0]}: {e}")
    found.sort(key=lambda l: l.get("seen") or "")  # oldest first, so price history runs forward
    return found, unreadable, errors
