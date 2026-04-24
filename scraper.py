# Berlin rental notifier — polls inberlinwohnen.de every 5 min via launchd.
#
# Setup (one-time, in terminal):
#   1. Copy .env.example to .env and fill in your Telegram bot token + chat ID.
#   2. python scraper.py          ← district selection menu appears
#   3. bash install.sh            ← installs deps and registers launchd agent
#
# Reconfigure districts: python scraper.py --reconfigure

import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "scraper.log"
FINDER_URL = "https://www.inberlinwohnen.de/wohnungsfinder"

BEZIRKE = [
    "Mitte",
    "Friedrichshain-Kreuzberg",
    "Pankow",
    "Charlottenburg-Wilmersdorf",
    "Spandau",
    "Steglitz-Zehlendorf",
    "Tempelhof-Schöneberg",
    "Neukölln",
    "Treptow-Köpenick",
    "Marzahn-Hellersdorf",
    "Lichtenberg",
    "Reinickendorf",
]

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger(__name__)


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        data["seen_ids"] = set(data.get("seen_ids", []))
        return data
    return {"initialized": False, "districts": [], "seen_ids": set(), "last_run": None}


def save_state(state: dict) -> None:
    data = dict(state)
    data["seen_ids"] = sorted(state["seen_ids"])
    data["last_run"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


# ── First-run district setup ───────────────────────────────────────────────────

def prompt_for_districts() -> list:
    print("\nWhich districts do you want alerts for?")
    for i, b in enumerate(BEZIRKE, 1):
        print(f"  {i:>2}) {b}")
    print()
    raw = input("Enter numbers separated by commas (or ENTER for all): ").strip()
    if not raw:
        print("→ Watching all districts.")
        return []
    chosen = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(BEZIRKE):
                chosen.append(BEZIRKE[idx])
            else:
                print(f"  (ignoring out-of-range number: {part})")
        else:
            print(f"  (ignoring non-numeric input: {part})")
    if not chosen:
        print("→ No valid selection — watching all districts.")
        return []
    print(f"→ Watching: {', '.join(chosen)}")
    return chosen


# ── HTTP fetch ─────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}


def fetch_html() -> str:
    for attempt in range(2):
        try:
            resp = requests.get(FINDER_URL, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            if attempt == 0:
                log.warning("Fetch failed (%s), retrying in 5s…", exc)
                time.sleep(5)
            else:
                log.error("Fetch failed after retry: %s", exc)
                sys.exit(1)


# ── Parsing ────────────────────────────────────────────────────────────────────

def parse_item_ids(html: str) -> list[int]:
    m = re.search(r"itemIds:\s*\[([\d,\s]+)\]", html)
    if not m:
        log.error("Could not find itemIds in page source")
        sys.exit(1)
    return [int(x) for x in m.group(1).split(",") if x.strip()]


_ARIA_RE = re.compile(
    r"Wohnungsangebot - ([\d,]+) Zimmer, ([\d,]+) m², ([\d.,]+) € Kaltmiete \| (.+?)\s*$"
)
_ID_RE = re.compile(r"open !== (\d+)")
_PLZ_RE = re.compile(r"\b\d{5}\s+(.+)$")


def parse_top_listings(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    results = {}
    for btn in soup.find_all("button", attrs={"aria-label": True}):
        label = btn["aria-label"]
        if not label.startswith("Wohnungsangebot - "):
            continue
        m_label = _ARIA_RE.match(label)
        if not m_label:
            continue
        click = btn.get("@click", "") or btn.get("x-on:click", "")
        m_id = _ID_RE.search(click)
        if not m_id:
            continue
        apt_id = int(m_id.group(1))
        rooms, area, price, address = m_label.groups()
        district = parse_district(address)
        results[apt_id] = {
            "rooms": rooms,
            "area": area,
            "price": price,
            "address": address.strip(),
            "district": district,
        }
    return results


def parse_district(address: str) -> "str | None":
    m = _PLZ_RE.search(address)
    if not m:
        return None
    candidate = m.group(1).strip()
    for bezirk in BEZIRKE:
        if bezirk.lower() == candidate.lower():
            return bezirk
    # partial match for compound names like "Marzahn-Hellersdorf"
    for bezirk in BEZIRKE:
        if candidate.lower() in bezirk.lower() or bezirk.lower() in candidate.lower():
            return bezirk
    return None


# ── Telegram ───────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> None:
    try:
        from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    except KeyError as exc:
        log.error("Missing env var: %s — check your .env file", exc)
        sys.exit(1)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Telegram sent: %s", text[:60])
    except requests.RequestException as exc:
        log.error("Telegram send failed: %s", exc)


def format_message(info: dict, apt_id: int) -> str:
    district_label = info["district"] if info["district"] else "unbekannter Bezirk"
    link = f"{FINDER_URL}#apartment-{apt_id}"
    return (
        f"🏠 Neues Wohnungsangebot — {district_label}\n\n"
        f"📍 {info['address']}\n"
        f"📐 {info['area']} m²   🛏 {info['rooms']} Zimmer\n"
        f"💶 {info['price']} € Kaltmiete\n\n"
        f"🔗 {link}"
    )


# ── District filter ────────────────────────────────────────────────────────────

def district_allowed(district: "str | None", allowlist: list) -> bool:
    if not allowlist:
        return True
    if district is None:
        return True
    return district in allowlist


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    state = load_state()

    if not state["initialized"] or "--reconfigure" in sys.argv:
        if not sys.stdin.isatty():
            print(
                "ERROR: First run must be done interactively in a terminal "
                "so you can choose districts.",
                file=sys.stderr,
            )
            sys.exit(1)
        state["districts"] = prompt_for_districts()
        state["initialized"] = True

    html = fetch_html()
    current_ids = parse_item_ids(html)
    details = parse_top_listings(html)

    log.info("Fetched page: %d total IDs, %d rendered details", len(current_ids), len(details))

    if not state["seen_ids"]:
        # Very first real fetch — seed seen_ids without sending any notifications.
        state["seen_ids"] = set(current_ids)
        save_state(state)
        log.info(
            "Initialized with %d existing offers; districts=%s",
            len(current_ids),
            state["districts"] or "all",
        )
        print(
            f"Initialized. {len(current_ids)} existing offers seeded into state.json.\n"
            f"Districts: {', '.join(state['districts']) if state['districts'] else 'all'}.\n"
            "Daemon will notify you about new offers from here on."
        )
        return

    new_ids = [i for i in current_ids if i not in state["seen_ids"]]
    log.info("%d new offer(s) found", len(new_ids))

    for nid in new_ids:
        info = details.get(nid)
        if info is None:
            log.warning("ID %d not in top-10 details (>10 new in one window?)", nid)
            send_telegram(f"🏠 New offer (ID {nid}) — details not rendered, see site:\n🔗 {FINDER_URL}")
            continue

        if district_allowed(info["district"], state["districts"]):
            send_telegram(format_message(info, nid))
            log.info("Notified: ID %d — %s", nid, info["address"])
        else:
            log.info("Skipped (district %s not in allowlist): ID %d", info["district"], nid)

    state["seen_ids"] |= set(current_ids)
    save_state(state)


if __name__ == "__main__":
    main()
