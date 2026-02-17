import os
import re
import json
import time
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# =======================
# CONFIG
# =======================

# Timezone
TZ = ZoneInfo("America/Los_Angeles")

# LV Temple list view (this is the logic you said works)
LIST_URL = "https://www.lvtemple.org/events/list/"

# WhatsApp Cloud API config (your setup)
GRAPH_VERSION = "v24.0"
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "934053346465745")

# Your approved template details (from your earlier template list)
TEMPLATE_NAME = "events_list"
LANG_CODE = "en"          # IMPORTANT: events_list is approved as "en" (not en_US)
PARAM_NAME = "events"     # NAMED parameter

# Hardcode recipients here (digits only is fine)
RECIPIENTS = [
    "14259791931",
    # "1XXXXXXXXXX",
]

# Throttle between sends
DELAY_BETWEEN_SENDS_SEC = 0.25

# WhatsApp template param rules:
# - no newline/tab characters
# - no more than 4 consecutive spaces (we collapse whitespace)
# Also: you asked for no special characters
MAX_PARAM_LEN = 900

# =======================
# LOGGING
# =======================
LOG_DIR = "logs"
LOG_FILE = f"{LOG_DIR}/lvtemple.log"
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# =======================
# TEXT SANITIZATION (no special chars, WhatsApp-safe)
# =======================
_ALLOWED_PUNCT = set(" .,:;-/()")

def to_ascii_basic(s: str) -> str:
    """
    Keep only:
      - letters, digits, whitespace
      - basic punctuation: . , : ; - / ( )
    Remove bullets/fancy dashes/emoji/non-ascii.
    """
    if not s:
        return ""
    s = s.replace("\u00a0", " ")
    s = s.replace("–", "-").replace("—", "-").replace("•", " ")
    s = s.encode("ascii", errors="ignore").decode("ascii")  # drop non-ascii

    out = []
    for ch in s:
        if ch.isalnum() or ch.isspace() or ch in _ALLOWED_PUNCT:
            out.append(ch)
        else:
            out.append(" ")
    return "".join(out)

def sanitize_for_whatsapp_param(s: str) -> str:
    """
    WhatsApp template param restrictions:
      - No \n \t
      - No long whitespace runs
    Plus "no special chars" requirement.
    """
    s = to_ascii_basic(s)
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s

# =======================
# SCRAPE EVENTS (your working logic)
# =======================
def get_7_day_events():
    # Window: tomorrow through next 6 days (7-day window)
    start_date = datetime.now(TZ).date() + timedelta(days=1)
    end_date = start_date + timedelta(days=6)

    params = {"tribe-bar-date": start_date.isoformat()}
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    }

    logger.info(f"Fetching events from {start_date.isoformat()} to {end_date.isoformat()} (7 days)")
    response = requests.get(LIST_URL, params=params, headers=headers, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    articles = soup.find_all("article", class_="tribe-events-calendar-list__event")

    final_events = []

    for article in articles:
        time_el = article.find("time", class_="tribe-events-calendar-list__event-datetime")
        if not time_el:
            continue

        raw_date_str = time_el.get("datetime")
        if not raw_date_str:
            continue

        # datetime attribute can be 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'
        event_date = datetime.fromisoformat(raw_date_str.split()[0]).date()

        if start_date <= event_date <= end_date:
            title_el = article.find("a", class_="tribe-events-calendar-list__event-title-link")
            title = title_el.get_text(strip=True) if title_el else "Unknown Event"

            # human-friendly text inside <time> (keep it as shown on site)
            display_time = " ".join(time_el.get_text(" ", strip=True).split())

            final_events.append({
                "date": event_date,
                "display_time": display_time,
                "title": title
            })
        elif event_date > end_date:
            break

    logger.info(f"Extracted {len(final_events)} events in the 7-day window")
    return final_events

# =======================
# FORMAT MESSAGE (single-line, no special chars)
# =======================
def format_events_message(events):
    start_date = datetime.now(TZ).date() + timedelta(days=1)
    end_date = start_date + timedelta(days=6)

    # Example: "Next 7 days Feb 18 2026 to Feb 24 2026."
    header = f"Next 7 days {start_date.strftime('%b %d %Y')} to {end_date.strftime('%b %d %Y')}."
    parts = [header]

    # Sort by date (list view is already chronological, but keep deterministic)
    events_sorted = sorted(events, key=lambda e: (e["date"], e.get("display_time") or ""))

    for ev in events_sorted:
        # Include date explicitly for each item
        d = ev["date"].strftime("%b %d")
        # display_time may already include date text; but your output shows it usually includes time.
        # We'll keep it but sanitize.
        t = ev.get("display_time", "")
        title = ev.get("title", "")

        # Build one item sentence (avoid special chars, avoid newlines)
        parts.append(f"{d} {t} {title}.")

    msg = " ".join(parts)
    msg = sanitize_for_whatsapp_param(msg)

    if len(msg) > MAX_PARAM_LEN:
        msg = msg[: MAX_PARAM_LEN - 6].rstrip(" .,:;-/()") + " more."
        msg = sanitize_for_whatsapp_param(msg)

    return msg

# =======================
# WHATSAPP SEND
# =======================
def send_whatsapp_template(token: str, to_number: str, events_text: str) -> dict:
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "template",
        "template": {
            "name": TEMPLATE_NAME,
            "language": {"code": LANG_CODE},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {
                            "type": "text",
                            "text": events_text,
                            "parameter_name": PARAM_NAME,  # NAMED param (required)
                        }
                    ],
                }
            ],
        },
    }

    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
    data = r.json() if r.content else {}
    if r.status_code >= 300:
        raise RuntimeError(f"WhatsApp send failed ({r.status_code}): {data}")
    return data

# =======================
# MAIN
# =======================
def main():
    logger.info("Script started")

    # Read token from env (GitHub Actions secret -> env)
    token = os.environ["WHATSAPP_TOKEN"]

    # 1) Scrape events
    events = get_7_day_events()

    # 2) If no events, do not send any messages
    if not events:
        logger.info("No events found for the next 7 days. Not sending any WhatsApp messages.")
        return

    # 3) Format message (WhatsApp-safe + no special chars)
    events_text = format_events_message(events)
    logger.info(f"Final WhatsApp param length={len(events_text)}")
    logger.info(f"Final WhatsApp param text: {events_text}")

    # 4) Send to all recipients
    sent, failed = 0, 0
    for n in RECIPIENTS:
        try:
            res = send_whatsapp_template(token, n, events_text)
            msg_id = (res.get("messages") or [{}])[0].get("id")
            logger.info(f"Sent to {n} message_id={msg_id}")
            sent += 1
        except Exception as e:
            logger.error(f"Failed to send to {n}: {e}")
            failed += 1
        time.sleep(DELAY_BETWEEN_SENDS_SEC)

    logger.info(f"Done. Sent={sent} Failed={failed}")

if __name__ == "__main__":
    main()
