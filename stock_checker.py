#!/usr/bin/env python3
"""
iPhone Duo in-store pickup stock checker.

Checks Apple's pickup availability for the iPhone Duo (any color, 256GB/512GB
by default) at the Apple Stores you choose, and sends you a text message when
one shows up as available for pickup.

No third-party dependencies -- standard library only.

Usage:
    python stock_checker.py --list-stores     # find your store numbers (R###)
    python stock_checker.py --list-parts      # show the part numbers being checked
    python stock_checker.py                   # check once
    python stock_checker.py --loop 120        # check every 120 seconds forever
    python stock_checker.py --test-sms        # send a test text

Configuration is via environment variables (or a .env file next to this script).
See README.md for the full list.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import re
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from email.message import EmailMessage
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_PRODUCT_URL = "https://www.apple.com/shop/buy-iphone/iphone-duo"
FULFILLMENT_URL = "https://www.apple.com/shop/fulfillment-messages"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Apple's fulfillment endpoint gets unhappy with very long part lists.
PARTS_PER_REQUEST = 8


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Real env vars win."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env_list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def normalize_capacity(value: str) -> str:
    """'256GB', '256 gb', '256gb' -> '256gb'."""
    return re.sub(r"\s+", "", value).lower()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def http_get(url: str, *, accept: str = "*/*", referer: str | None = None,
             timeout: int = 20) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Part number discovery
# --------------------------------------------------------------------------- #

def _enclosing_json_object(text: str, idx: int) -> dict | None:
    """Return the smallest JSON object in `text` that contains position idx."""
    depth = 0
    start = idx
    while start >= 0:
        ch = text[start]
        if ch == "}":
            depth += 1
        elif ch == "{":
            if depth == 0:
                break
            depth -= 1
        start -= 1
    if start < 0:
        return None

    depth = 0
    in_str = False
    escape = False
    for end in range(start, min(len(text), start + 20000)):
        ch = text[end]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:end + 1])
                except ValueError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def parse_parts_from_html(html: str) -> dict[str, dict]:
    """
    Pull product variants out of the JSON Apple embeds in the buy page.

    Returns {partNumber: {"capacity": "256gb", "color": "night-sky"}}.
    """
    parts: dict[str, dict] = {}
    for m in re.finditer(r'"partNumber"\s*:\s*"([A-Z0-9]{4,6}[A-Z]{1,2}/A)"', html):
        part = m.group(1)
        obj = _enclosing_json_object(html, m.start())
        if obj is None:
            continue
        capacity = obj.get("dimensionCapacity")
        color = obj.get("dimensionColor")
        name = str(obj.get("name") or "")
        # e.g. "iPhone Duo 512GB Night Sky"
        nm = re.search(r"(\d+\s*[GT]B)\s+(.+)$", name, re.I)
        if capacity is None and nm:
            capacity = nm.group(1)
        if color is None and nm:
            color = nm.group(2).strip()
        if capacity is None:
            continue
        info = parts.setdefault(part, {"capacity": normalize_capacity(str(capacity)),
                                       "color": "unknown"})
        if color and info["color"] == "unknown":
            info["color"] = str(color)
    return parts


def discover_parts(product_url: str, capacities: list[str]) -> dict[str, dict]:
    html = http_get(product_url, accept="text/html,application/xhtml+xml")
    all_parts = parse_parts_from_html(html)
    wanted = {normalize_capacity(c) for c in capacities}
    return {p: info for p, info in all_parts.items() if info["capacity"] in wanted}


def get_parts(args_refresh: bool = False) -> dict[str, dict]:
    """Part numbers from PART_NUMBERS, else cached discovery, else live discovery."""
    manual = env_list("PART_NUMBERS")
    if manual:
        return {p: {"capacity": "?", "color": "?"} for p in manual}

    capacities = env_list("CAPACITIES", "256gb,512gb")
    product_url = os.environ.get("PRODUCT_URL", DEFAULT_PRODUCT_URL)
    cache_file = SCRIPT_DIR / ".parts_cache.json"

    if cache_file.exists() and not args_refresh:
        try:
            cached = json.loads(cache_file.read_text())
            if (cached.get("product_url") == product_url
                    and cached.get("capacities") == capacities
                    and cached.get("parts")):
                return cached["parts"]
        except ValueError:
            pass

    parts = discover_parts(product_url, capacities)
    if not parts:
        raise RuntimeError(
            f"Couldn't find any {'/'.join(capacities)} part numbers on {product_url}. "
            "Apple may have changed the page. Set PART_NUMBERS manually "
            "(see README.md for how to find them)."
        )
    cache_file.write_text(json.dumps(
        {"product_url": product_url, "capacities": capacities, "parts": parts},
        indent=2,
    ))
    return parts


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #

def fetch_fulfillment(parts: list[str], *, zip_code: str | None = None,
                      store: str | None = None) -> dict:
    params: list[tuple[str, str]] = [("pl", "true"), ("mts.0", "regular")]
    for i, part in enumerate(parts):
        params.append((f"parts.{i}", part))
    if store:
        params.append(("store", store))
    if zip_code:
        params.append(("location", zip_code))
    url = f"{FULFILLMENT_URL}?{urllib.parse.urlencode(params)}"
    body = http_get(
        url,
        accept="application/json",
        referer=os.environ.get("PRODUCT_URL", DEFAULT_PRODUCT_URL),
    )
    return json.loads(body)


def parse_stores(payload: dict) -> list[dict]:
    """
    Normalize Apple's response into:
      [{"number": "R123", "name": "...", "city": "...",
        "parts": {part: {"available": bool, "title": str, "quote": str}}}]
    """
    stores = (
        payload.get("body", {})
        .get("content", {})
        .get("pickupMessage", {})
        .get("stores", [])
    ) or []
    out = []
    for s in stores:
        parts = {}
        for part, info in (s.get("partsAvailability") or {}).items():
            regular = (info.get("messageTypes") or {}).get("regular") or {}
            title = (
                regular.get("storePickupProductTitle")
                or info.get("storePickupProductTitle")
                or part
            )
            quote = (
                regular.get("storePickupQuote")
                or info.get("pickupSearchQuote")
                or info.get("storePickupQuote")
                or ""
            )
            available = info.get("pickupDisplay") == "available"
            parts[part] = {
                "available": bool(available),
                "title": re.sub(r"<[^>]+>", "", str(title)),
                "quote": re.sub(r"<[^>]+>", "", str(quote)),
            }
        out.append({
            "number": s.get("storeNumber", "?"),
            "name": s.get("storeName", "?"),
            "city": s.get("city", ""),
            "state": s.get("state", ""),
            "distance": s.get("storeDistanceWithUnit") or s.get("storedistance", ""),
            "parts": parts,
        })
    return out


def check_availability(parts: dict[str, dict], zip_code: str,
                       store_numbers: list[str], max_stores: int) -> tuple[list[dict], list[dict]]:
    """Returns (stores_checked, hits). hits = [{store..., part, title, quote}]."""
    part_list = list(parts)
    merged: dict[str, dict] = {}

    for i in range(0, len(part_list), PARTS_PER_REQUEST):
        chunk = part_list[i:i + PARTS_PER_REQUEST]
        if store_numbers:
            # Query each store directly so we always get exactly those stores.
            for store in store_numbers:
                payload = fetch_fulfillment(chunk, zip_code=zip_code, store=store)
                _merge(merged, parse_stores(payload), only={store})
        else:
            payload = fetch_fulfillment(chunk, zip_code=zip_code)
            stores = parse_stores(payload)[:max_stores]
            _merge(merged, stores, only=None)

    checked = list(merged.values())
    hits = []
    for store in checked:
        for part, info in store["parts"].items():
            if info["available"]:
                hits.append({
                    "store_number": store["number"],
                    "store_name": store["name"],
                    "part": part,
                    "title": info["title"],
                    "quote": info["quote"],
                })
    return checked, hits


def _merge(merged: dict[str, dict], stores: list[dict], only: set[str] | None) -> None:
    for s in stores:
        if only is not None and s["number"] not in only:
            continue
        existing = merged.setdefault(s["number"], {**s, "parts": {}})
        existing["parts"].update(s["parts"])


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #

def send_text(message: str) -> list[str]:
    """Send via every configured channel. Returns list of channels used."""
    sent = []
    errors = []

    if os.environ.get("TWILIO_ACCOUNT_SID"):
        try:
            _send_twilio(message)
            sent.append("twilio")
        except Exception as e:  # noqa: BLE001
            errors.append(f"twilio: {e}")

    if os.environ.get("SMS_GATEWAY_EMAIL") and os.environ.get("SMTP_USER"):
        try:
            _send_email_sms(message)
            sent.append("email-to-sms")
        except Exception as e:  # noqa: BLE001
            errors.append(f"email-to-sms: {e}")

    if os.environ.get("NTFY_TOPIC"):
        try:
            _send_ntfy(message)
            sent.append("ntfy")
        except Exception as e:  # noqa: BLE001
            errors.append(f"ntfy: {e}")

    for err in errors:
        log(f"Notification error -> {err}")
    if not sent and not errors:
        log("No notification channel configured! Set Twilio, SMTP or NTFY vars (see README).")
    return sent


def _send_twilio(message: str) -> None:
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    from_number = os.environ["TWILIO_FROM"]
    recipients = env_list("SMS_TO")
    if not recipients:
        raise RuntimeError("SMS_TO is not set")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    auth = b64encode(f"{sid}:{token}".encode()).decode()
    for to in recipients:
        data = urllib.parse.urlencode({"From": from_number, "To": to, "Body": message}).encode()
        req = urllib.request.Request(url, data=data, headers={"Authorization": f"Basic {auth}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from e


def _send_email_sms(message: str) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = ", ".join(env_list("SMS_GATEWAY_EMAIL"))
    msg["Subject"] = "iPhone Duo"
    msg.set_content(message)
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)


def _send_ntfy(message: str) -> None:
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    topic = os.environ["NTFY_TOPIC"]
    req = urllib.request.Request(
        f"{server}/{topic}",
        data=message.encode(),
        headers={"Title": "iPhone Duo in stock!", "Priority": "urgent", "Tags": "iphone"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


# --------------------------------------------------------------------------- #
# Alert de-duplication
# --------------------------------------------------------------------------- #

STATE_FILE = Path(os.environ.get("STATE_FILE", SCRIPT_DIR / ".alert_state.json"))


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def new_hits_to_alert(hits: list[dict], state: dict, realert_minutes: int) -> list[dict]:
    """
    Only alert for a store+part that is newly in stock, or that has stayed in
    stock longer than realert_minutes since the last text. Items that went out
    of stock are forgotten so they re-alert when they come back.
    """
    now = time.time()
    current_keys = {f"{h['store_number']}|{h['part']}" for h in hits}
    for key in list(state):
        if key not in current_keys:
            del state[key]
    to_alert = []
    for h in hits:
        key = f"{h['store_number']}|{h['part']}"
        last = state.get(key)
        if last is None or now - last >= realert_minutes * 60:
            to_alert.append(h)
            state[key] = now
    return to_alert


def format_message(hits: list[dict]) -> str:
    lines = ["iPhone Duo IN STOCK for pickup!"]
    for h in hits:
        line = f"- {h['title']} @ {h['store_name']}"
        if h["quote"]:
            line += f" ({h['quote']})"
        lines.append(line)
    lines.append(os.environ.get("PRODUCT_URL", DEFAULT_PRODUCT_URL))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def require_zip() -> str:
    zip_code = os.environ.get("ZIP_CODE", "").strip()
    if not zip_code:
        sys.exit("ZIP_CODE is not set. Put it in .env or export it (see README).")
    return zip_code


def run_once(refresh_parts: bool = False) -> int:
    zip_code = require_zip()
    store_numbers = env_list("STORE_NUMBERS")
    max_stores = int(os.environ.get("MAX_STORES", "2"))
    realert = int(os.environ.get("REALERT_MINUTES", "60"))

    parts = get_parts(refresh_parts)
    checked, hits = check_availability(parts, zip_code, store_numbers, max_stores)

    store_desc = ", ".join(f"{s['name']} ({s['number']})" for s in checked) or "no stores"
    log(f"Checked {len(parts)} variants at {store_desc}: {len(hits)} available")
    for h in hits:
        log(f"  AVAILABLE: {h['title']} @ {h['store_name']} {h['quote']}")

    state = load_state()
    to_alert = new_hits_to_alert(hits, state, realert)
    if to_alert:
        channels = send_text(format_message(to_alert))
        log(f"Sent alert via: {', '.join(channels) or 'nothing'}")
        if not channels:
            # Don't record as alerted if nothing actually went out.
            for h in to_alert:
                state.pop(f"{h['store_number']}|{h['part']}", None)
    save_state(state)
    return len(hits)


def cmd_list_stores() -> None:
    zip_code = require_zip()
    parts = list(get_parts())[:1]
    stores = parse_stores(fetch_fulfillment(parts, zip_code=zip_code))
    print(f"Apple Stores near {zip_code} (nearest first):\n")
    for s in stores:
        loc = ", ".join(x for x in (s["city"], s["state"]) if x)
        print(f"  {s['number']:<6} {s['name']:<30} {loc:<25} {s['distance']}")
    print("\nPut the two you want in STORE_NUMBERS, e.g. STORE_NUMBERS=R123,R456")


def cmd_list_parts(refresh: bool) -> None:
    parts = get_parts(refresh)
    print(f"Checking {len(parts)} part numbers:\n")
    for p, info in sorted(parts.items(), key=lambda kv: (kv[1]["capacity"], kv[1]["color"])):
        print(f"  {p:<12} {info['capacity']:<8} {info['color']}")


def main() -> None:
    load_dotenv(SCRIPT_DIR / ".env")

    ap = argparse.ArgumentParser(description="iPhone Duo Apple Store pickup stock checker")
    ap.add_argument("--loop", type=int, metavar="SECONDS",
                    help="keep checking every N seconds (with a little random jitter)")
    ap.add_argument("--list-stores", action="store_true", help="show nearby stores + store numbers")
    ap.add_argument("--list-parts", action="store_true", help="show the part numbers being checked")
    ap.add_argument("--refresh-parts", action="store_true", help="re-discover part numbers from apple.com")
    ap.add_argument("--test-sms", action="store_true", help="send a test notification and exit")
    args = ap.parse_args()

    if args.test_sms:
        channels = send_text("Test from your iPhone Duo stock checker. Texts are working!")
        print(f"Sent via: {', '.join(channels) or 'nothing (check your config)'}")
        return
    if args.list_stores:
        cmd_list_stores()
        return
    if args.list_parts:
        cmd_list_parts(args.refresh_parts)
        return

    if not args.loop:
        run_once(args.refresh_parts)
        return

    interval = max(30, args.loop)
    log(f"Checking every ~{interval}s. Ctrl+C to stop.")
    refresh = args.refresh_parts
    while True:
        try:
            run_once(refresh)
            refresh = False
        except urllib.error.HTTPError as e:
            hint = (" Apple is refusing automated requests from this network/IP;"
                    " try running from your home connection.") if e.code in (403, 541) else ""
            log(f"Apple returned HTTP {e.code}; will retry.{hint}")
        except Exception as e:  # noqa: BLE001
            log(f"Error: {e}")
        time.sleep(interval + random.uniform(0, interval * 0.2))


if __name__ == "__main__":
    main()
