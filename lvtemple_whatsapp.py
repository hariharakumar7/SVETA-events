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

TZ = ZoneInfo("America/Los_Angeles")

# LV Temple list view (your working scraping approach)
LIST_URL = "https://www.lvtemple.org/events/list/"

# WhatsApp Cloud API config
GRAPH_VERSION = "v24.0"
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "934053346465745")

# WhatsApp template info (your approved template)
TEMPLATE_NAME = "events_list"
LANG_CODE = "en"          # IMPORTANT: events_list is approved in 'en'
PARAM_NAME = "events"     # NAMED parameter

# Throttle between sends
DELAY_BETWEEN_SENDS_SEC = 0.25

# WhatsApp template param rules + your preference
MAX_PARAM_LEN = 900

# Airtable config (from GitHub Secrets)
# You said you'll create another secret for baseId:
# Name it exactly AIRTABLE_BASE_ID
AIRTABLE_TOKEN = os.environ["AIRTABLE_TOKEN"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME", "Recipients")

# Column names in Airtable (match these in your table)
AIRTABLE_PHONE_FIELD = os.environ.get("AIRTABLE_PHONE_FIELD", "phone")
AIRTABLE_ACTIVE_FIELD = os.environ.get("AIRTABLE_ACTIVE_FIELD", "active")

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
# TEXT SANITIZATION
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

def normalize_phone(phone: str) -> str | None:
    """
    Normalize phone number to digits-only string.
    Accepts inputs like '+1 425-979-1931' and returns '14259791931'.
    """
    if not phone:
        return None
    digits = re.sub(r"\D", "", str(phone))
    return digits if digits else None

# =======================
# AIRTABLE INTEGRATION
# =======================
def fetch_recipients_from_airtable() -> list[str]:
    """
    Fetch recipients from Airtable where active = TRUE().
    Requires columns:
      - phone (text)
      - active (checkbox)
    """
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}

    # Airtable formula: checkbox true
    params = {
        "filterByFormula": f"{{{AIRTABLE_ACTIVE_FIELD}}}=TRUE()",
        "pageSize": 100,
    }

    recipients: list[str] = []
    seen = set()

    logger.info("Fetching recipients from Airtable...")
    while True:
        r = requests.get(url, headers=headers, params=params, timeout=30)

        # Helpful error detail if token/base/table is wrong
        if r.status_code >= 300:
            raise RuntimeError(f"Airtable fetch failed ({r.status_code}): {r.text[:300]}")

        data = r.json()
        records = data.get("records", []) or []

        for rec in records:
            fields = rec.get("fields", {}) or {}
            phone_raw = fields.get(AIRTABLE_PHONE_FIELD)
            phone = normalize_phone(phone_raw)
            if phone and phone not in seen:
                recipients.append(phone)
                seen.add(phone)

        offset = data.get("offset")
        if not offset:
            break
        params["offset"] = offset

    logger.info(f"Fetched {len(recipients)} active recipients from Airtable")
    return recipients

# =======================
# SCRAPE EVENTS (your working logic)
# =======================
def get_7_day_events():
    start_date = datetime.now(TZ).date() + timedelta(days=1)
    end_date = start_date + timedelta(days=6)

    params = {"tribe-bar-date": start_date.isoformat()}
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    }

    logger.info(f"Fetching events from {start_date} to {end_date}...")
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

        event_date = datetime.fromisoformat(raw_date_str.split()[0]).date()

        if start_date <= event_date <= end_date:
            title_el = article.find("a", class_="tribe-events-calendar-list__event-title-link")
            title = title_el.get_text(strip=True) if title_el else "Unknown Event"

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
# FORMAT MESSAGE
# =======================
def format_events_message(events):
    start_date = datetime.now(TZ).date() + timedelta(days=1)
    end_date = start_date + timedelta(days=6)

    header = f"Next 7 days {start_date.strftime('%b %d %Y')} to {end_date.strftime('%b %d %Y')}."
    parts = [header]

    events_sorted = sorted(events, key=lambda e: (e["date"], e.get("display_time") or ""))

    for ev in events_sorted:
        d = ev["date"].strftime("%b %d")
        t = ev.get("display_time", "")
        title = ev.get("title", "")
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
                            "parameter_name": PARAM_NAME,
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

    whatsapp_token = os.environ["WHATSAPP_TOKEN"]

    # 1) Fetch recipients from Airtable
    recipients = fetch_recipients_from_airtable()
    if not recipients:
        logger.info("No active recipients found in Airtable. Nothing to send.")
        return

    # 2) Scrape events for next 7 days
    events = get_7_day_events()
    if not events:
        logger.info("No events found for the next 7 days. Not sending any WhatsApp messages.")
        return

    # 3) Format message
    events_text = format_events_message(events)
    logger.info(f"Final WhatsApp param length={len(events_text)}")
    logger.info(f"Final WhatsApp param text: {events_text}")

    # 4) Send to all recipients
    sent, failed = 0, 0
    for n in recipients:
        try:
            res = send_whatsapp_template(whatsapp_token, n, events_text)
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
