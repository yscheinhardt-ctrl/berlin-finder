# Berlin Rental Notifier — Implementation Plan

## Context

You want a background tool that watches **https://www.inberlinwohnen.de/wohnungsfinder** and pings your Telegram every time a new rental offer appears, with **price, square meters, and address**. The site lists ~192 apartments from the 7 landeseigene (state-owned) Berlin housing companies, which is where the cheapest legit rentals in the city show up — so being the first to see a new listing is valuable.

Target stack (from your answers): **Python**, **Telegram bot**, **launchd** (macOS scheduler), **every 5 minutes**, with an **interactive district filter** configured on first run.

## Approach

One Python script, run every 5 minutes by launchd. Each run:

1. GET `https://www.inberlinwohnen.de/wohnungsfinder` (with a realistic User-Agent).
2. Extract the **full list of current apartment IDs** from the inline JS `itemIds: [...]` array on the page — this is the authoritative "what exists now" snapshot (all ~192 IDs in one shot, no pagination needed).
3. Extract **details for the top-10 newest listings** from their `aria-label` attributes, which the site helpfully pre-formats like:
   `Wohnungsangebot - 3,0 Zimmer, 71,32 m², 1.276,06 € Kaltmiete | Hugo-Cassirer-Straße 45, 13587 Spandau`
4. Parse the Bezirk (district) from each address (the word after the 5-digit postcode).
5. Load previously-seen IDs and the user's **chosen districts** from `state.json`. Compute `new_ids = current_ids - seen_ids`.
6. For each new ID: if its district is in the allowlist (or is unparseable), send a Telegram message; otherwise drop it silently. Either way, mark the ID as seen so it doesn't reappear next run.
7. Log to `scraper.log` and exit.

### Why this works
- IDs are monotonically increasing auto-increment integers (highest currently `16073`) and the default sort is `created_at.desc`, so new offers always land in the top 10 that are fully rendered — as long as we poll often enough. At 5-minute intervals, landing >10 offers in one window is practically impossible (the site publishes a handful per day).
- No login, no CAPTCHA, no rate-limit headers observed.
- No need to replay Livewire's `/livewire/update` AJAX (CSRF + snapshot + checksum nightmare), because page 1 already has what we need.

### Fallback for >10 new offers in one poll
If `new_ids` contains IDs whose details weren't in the top-10 aria-labels, send a minimal message with just the ID + a link back to the finder, and log a warning. This should basically never fire.

## Files to create

All under `/Users/Younes/Desktop/BerlinWohnen/`:

| File | Purpose |
|---|---|
| `scraper.py` | Main script (fetch, parse, diff, notify, persist). |
| `config.py` | Loads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from a local `.env` file. |
| `.env` | Your two secrets (gitignored; not created by code — you fill it in). |
| `.env.example` | Template showing the two variable names. |
| `requirements.txt` | `requests`, `beautifulsoup4`, `python-dotenv`. |
| `state.json` | Created on first run; stores `{"seen_ids": [...], "last_run": "..."}`. |
| `scraper.log` | Rolling log (simple append; rotate manually if it ever matters). |
| `de.inberlinwohnen.scraper.plist` | LaunchAgent definition (5-min interval). |
| `install.sh` | One-liner to `pip install -r requirements.txt` into a local venv and `launchctl load` the plist. |

No README (per your preferences) — setup steps live as comments at the top of `scraper.py` and in `install.sh`.

## Core script logic (`scraper.py`)

```text
main():
    state = load_state()                        # {seen_ids, districts, initialized}

    # --- First-run interactive setup (TTY required) ---
    if not state.initialized or "--reconfigure" in argv:
        if not stdin.isatty():
            die("First run must be done manually in a terminal so you can pick districts.")
        state.districts = prompt_for_districts()
        state.initialized = True
        # don't return yet — continue to the first real fetch below

    html = fetch("https://www.inberlinwohnen.de/wohnungsfinder")
    current_ids = parse_item_ids(html)          # from  itemIds: [...] JS array
    details     = parse_top_listings(html)      # dict[id -> {rooms, area, price, address, district}]

    if not state.seen_ids:
        # very first fetch — don't spam with 192 existing offers
        state.seen_ids = set(current_ids)
        save_state(state)
        log(f"Initialized with {len(current_ids)} existing offers; districts={state.districts}")
        return

    new_ids = [i for i in current_ids if i not in state.seen_ids]
    for nid in new_ids:
        info = details.get(nid)
        if info is None:
            # rare: >10 new in one 5-min window. Send a minimal placeholder.
            send_telegram(f"🏠 New offer (ID {nid}) — details not in top 10, see site.")
            continue

        if district_allowed(info.district, state.districts):
            send_telegram(format_message(info))
        # else: silently skip, but still mark as seen below

    state.seen_ids |= set(current_ids)
    save_state(state)


district_allowed(bezirk, allowlist):
    if not allowlist:            # empty = user wants everything
        return True
    if bezirk is None:           # parse failure → notify anyway, tagged
        return True
    return bezirk in allowlist
```

### Parsing specifics
- **`current_ids`**: regex-match on the HTML source for `itemIds:\s*\[([\d,\s]+)\]` (the inline JS form, line ~2157 in the current page) and `int`-split the capture. This is more robust than parsing the HTML-entity-encoded Livewire `wire:snapshot` JSON.
- **Top-10 details**: use BeautifulSoup to find all `<button>` elements with `aria-label` starting with `"Wohnungsangebot - "`, then regex the label:
  ```
  Wohnungsangebot - (\d+,\d+) Zimmer, (\d+,\d+) m², ([\d.,]+) € Kaltmiete \| (.+?)\s*$
  ```
  The apartment ID is in the same button's `@click` attribute (`open !== 16073 ? open = 16073 ...`) — regex `open !== (\d+)` to grab it.
- **District**: the 4th regex group is the full address (e.g. `Hugo-Cassirer-Straße 45, 13587 Spandau`). Match `\b\d{5}\s+(.+)$` to extract the word(s) after the postcode. Normalize to the 12 canonical Berlin Bezirke (the site uses the short forms `Spandau`, `Pankow`, `Marzahn-Hellersdorf`, etc.). Anything that doesn't match a known Bezirk → `None` and is tagged "unbekannter Bezirk" in the notification.
- Convert German number formats (`1.276,06` → `1276.06`) for any downstream sorting, but keep the original string for the Telegram message so it reads naturally in German.

### Telegram notification format
```
🏠 Neues Wohnungsangebot — Spandau

📍 Hugo-Cassirer-Straße 45, 13587 Spandau
📐 71,32 m²   🛏 3,0 Zimmer
💶 1.276,06 € Kaltmiete

🔗 https://www.inberlinwohnen.de/wohnungsfinder
```
(If the Bezirk can't be parsed, the first line reads `— unbekannter Bezirk`.)
Sent via `https://api.telegram.org/bot<TOKEN>/sendMessage` with `chat_id` and `text`.

### HTTP hygiene
- Realistic `User-Agent` header (Chrome on macOS).
- 15-second timeout, one retry on network error, hard fail otherwise (launchd will just run again in 5 min).
- Respect HTTP errors: log and exit non-zero so launchd records the failure.

## Configuration

**Secrets live in `.env`:**
```
TELEGRAM_BOT_TOKEN=123456:AAE...
TELEGRAM_CHAT_ID=987654321
```

**Runtime settings live in `state.json`** (auto-managed):
```json
{
  "initialized": true,
  "districts": ["Pankow", "Mitte", "Friedrichshain-Kreuzberg"],
  "seen_ids": [239, 325, 5932, ...],
  "last_run": "2026-04-22T15:30:00+02:00"
}
```

**One-time setup you'll do by hand:**
1. Message `@BotFather` on Telegram → `/newbot` → copy the token.
2. Message your new bot once (anything), then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` → copy `chat.id` from the JSON.
3. Paste both into `.env`.
4. Run `python scraper.py` **once in your terminal** — it will show a numbered menu of the 12 Berlin Bezirke:
   ```
   Which districts do you want alerts for?
     1) Mitte                       7) Tempelhof-Schöneberg
     2) Friedrichshain-Kreuzberg    8) Neukölln
     3) Pankow                      9) Treptow-Köpenick
     4) Charlottenburg-Wilmersdorf 10) Marzahn-Hellersdorf
     5) Spandau                    11) Lichtenberg
     6) Steglitz-Zehlendorf        12) Reinickendorf

   Enter numbers separated by commas (or ENTER for all):
   ```
   Your choice is saved. Afterwards launchd can take over.
5. To change your choice later: `python scraper.py --reconfigure`.

## Scheduling (`de.inberlinwohnen.scraper.plist`)

LaunchAgent at `~/Library/LaunchAgents/de.inberlinwohnen.scraper.plist`:

- `Label`: `de.inberlinwohnen.scraper`
- `ProgramArguments`: `["/Users/Younes/Desktop/BerlinWohnen/.venv/bin/python", "/Users/Younes/Desktop/BerlinWohnen/scraper.py"]`
- `StartInterval`: `300` (seconds)
- `RunAtLoad`: `true` (runs once immediately when loaded)
- `WorkingDirectory`: `/Users/Younes/Desktop/BerlinWohnen`
- `StandardOutPath` / `StandardErrorPath`: `/Users/Younes/Desktop/BerlinWohnen/launchd.log`

Install: `launchctl load -w ~/Library/LaunchAgents/de.inberlinwohnen.scraper.plist`
Uninstall: `launchctl unload ~/Library/LaunchAgents/de.inberlinwohnen.scraper.plist`

## Verification

1. **Interactive setup**: run `python scraper.py` once in terminal — district menu appears; pick e.g. `3,5` (Pankow + Spandau) or press ENTER for all. `state.json` should now have `initialized: true` and your chosen `districts`, plus ~192 IDs seeded into `seen_ids`. No Telegram message sent yet.
2. **District filter works**: manually remove two IDs from `seen_ids` — one whose address is inside your allowlist and one outside. Run again. Only the matching one should hit Telegram; the other should be silently re-marked as seen. Confirm via `scraper.log`.
3. **Notification format**: the Telegram message has the district in the header (`— Spandau`), correct address/€/m²/Zimmer, and the link is clickable.
4. **Unknown-district fallback**: temporarily hack a listing's parsed district to `None` and confirm the message renders `— unbekannter Bezirk` and still goes through.
5. **Reconfigure**: run `python scraper.py --reconfigure` and confirm the menu re-appears and your new selection persists.
6. **launchd activation**: `launchctl load -w …/de.inberlinwohnen.scraper.plist`, then `launchctl list | grep inberlinwohnen` shows it; tail `launchd.log` to confirm it ticks every 5 min.
7. **End-to-end patience test**: leave it running overnight — within ~24h at least one new offer typically appears. If it matches your districts you'll get a real Telegram ping; if not, it will pass quietly and you'll see the skip in `scraper.log`.

## Out of scope (can add later)

- Sub-Bezirk (Ortsteil / neighborhood) filtering — needs a Berlin PLZ → Ortsteil mapping.
- Filtering by max rent / min m² / min rooms (easy: add predicates next to `district_allowed`).
- Including the company + direct exposé link (the page has an "Alle Details" href per listing — trivial to parse once we add it).
- SQLite instead of JSON (unnecessary until state grows past a few thousand entries).
- Auto-removing stale IDs from `state.json` when they drop off the site.
