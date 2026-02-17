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
# Logging setup
# =======================

LOG_DIR = "logs"
LOG_FILE = f"{LOG_DIR}/lvtemple.log"

os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

# =======================
# Config
# =======================

TZ = ZoneInfo("America/Los_Angeles")

CALENDAR_MONTH_URL = "https://www.lvtemple.org/calendar/?tribe-bar-date={date}&eventDisplay=month"

GRAPH_VERSION = "v24.0"
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "934053346465745")
TEMPLATE_NAME = "events_list"
LANG_CODE = "en"
PARAM_NAME = "events"

RECIPIENTS = [
    "14259791931",
]

DELAY_BETWEEN_SENDS_SEC = 0.25

# =======================
# Helpers
# =======================

def fetch_html(url: str) -> str:
    logger.info(f"Fetching calendar page: {url}")
    r = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    r.raise_for_status()
    return r.text

def sanitize_template_param(s: str) -> str:
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def parse_events_from_month_grid(html: str, target_date_iso: str):
    soup = BeautifulSoup(html, "html.parser")

    day_cell = soup.find(attrs={"data-date": target_date_iso})
    if not day_cell:
        logger.info(f"No day cell found for {target_date_iso}")
        return []

    title_links = day_cell.find_all("a", href=True)

    events = []
    seen = set()

    for a in title_links:
        title = a.get_text(" ", strip=True)
        href = a["href"]

        if not title:
            continue

        if "/event" not in href and "/events" not in href:
            continue

        key = (title, href)
        if key in seen:
            continue

        seen.add(key)

        container = a.find_parent(["article", "div", "li", "td"]) or day_cell

        time_text = None

        ttag = container.find("time")
        if ttag:
            time_text = ttag.get_text(" ", strip=True)

        events.append({
            "title": title,
            "time": time_text
        })

    logger.info(f"Found {len(events)} events for {target_date_iso}")

    return events

def format_events_one_line(target_date_str, events):
    parts = [target_date_str]

    for e in events:
        t = e.get("time") or "Time TBD"
        title = e.get("title")
        parts.append(f"{t} — {title}")

    return sanitize_template_param(" • ".join(parts))

def send_whatsapp_template(token, to_number, events_text):

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

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
                    ]
                }
            ]
        }
    }

    logger.info(f"Sending message to {to_number}")

    r = requests.post(url, headers=headers, json=payload)

    if r.status_code >= 300:
        logger.error(f"Failed sending to {to_number}: {r.text}")
        return False

    resp = r.json()

    msg_id = resp.get("messages", [{}])[0].get("id")

    logger.info(f"Message sent successfully to {to_number}, message_id={msg_id}")

    return True

# =======================
# Main
# =======================

def main():

    logger.info("Script started")

    token = os.environ["WHATSAPP_TOKEN"]

    target_date = datetime.now(TZ).date() + timedelta(days=1)

    target_date_iso = target_date.isoformat()
    target_date_str = target_date.strftime("%b %d, %Y")

    url = CALENDAR_MONTH_URL.format(date=target_date_iso)

    html = fetch_html(url)

    events = parse_events_from_month_grid(html, target_date_iso)

    if not events:
        logger.info("No events found. No WhatsApp messages will be sent.")
        return

    events_text = format_events_one_line(target_date_str, events)

    logger.info(f"Events text: {events_text}")

    sent = 0
    failed = 0

    for n in RECIPIENTS:

        success = send_whatsapp_template(token, n, events_text)

        if success:
            sent += 1
        else:
            failed += 1

        time.sleep(DELAY_BETWEEN_SENDS_SEC)

    logger.info(f"Script finished. Sent={sent}, Failed={failed}")

if __name__ == "__main__":
    main()
