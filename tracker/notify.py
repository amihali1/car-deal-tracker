"""Push notifications via ntfy."""
import logging
import urllib.request

log = logging.getLogger(__name__)


def push(cfg, title, body, url=None, tags=None, priority="default"):
    n = cfg.get("ntfy", {})
    tags = tags or cfg.get("vehicle", {}).get("ntfy_tag", "car")
    if not n.get("topic"):
        log.info("ntfy topic not set; would send: %s | %s", title, body)
        return False
    req = urllib.request.Request(
        f"{n['server'].rstrip('/')}/{n['topic']}",
        data=body.encode("utf-8"),
        method="POST",
        headers={"Title": title.encode("ascii", "ignore").decode(), "Tags": tags, "Priority": priority},
    )
    if url:
        req.add_header("Click", url)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except OSError as e:
        log.error("ntfy push failed: %s", e)
        return False
