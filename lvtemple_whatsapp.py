import os
import re
import json
import time
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, date
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
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# =======================
# Config
# =======================
TZ = ZoneInfo("America/Los_Angeles")

# Month view (The Events Calendar)
CALENDAR_MONTH_URL = "https://www.lvtemple.org/calendar/?tribe-bar-date={date}&eventDisplay=month"

# WhatsApp Cloud API
GRAPH_VERSION = "v24.0"
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "934053346465745")

# Your approved template:
# name: events_list
# language: en
# parameter_format: NAMED
TEMPLATE_NAME = "events_list"
LANG_CODE = "en"
PARAM_NAME = "events"

# Hardcode recipients (digits only is fine)
RECIPIENTS = [
    "14259791931",
    # "1XXXXXXXXXX",
    # "1YYYYYYYYYY",
]

DELAY_BETWEEN_SENDS_SEC = 0.25

# WhatsApp template param constraints:
# - No \n / \t in param value
# - Avoid long whitespace runs
# Keep it conservative
MAX_PARAM_LEN = 900

# =======================
# Regex patterns
# =======================
DAY_HEADER_RE = re.compile(
    r"^\s*(\d+)\s+events?,?\s+(\d{1,2})\s*$",
    re.IGNORECASE,
)

TIME_RE = re.compile(
    r"\b\d{1,2}:\d{2}\s*(am|pm)\b(?:\s*[-–]\s*\d{1,2}:\d{2}\s*(am|pm)\b)?",
    re.IGNORECASE,
)

MONTH_NAME_RE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\b", re.IGNORECASE)

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12
}

# =======================
# Helpers
# =======================
def fetch_html(url: str) -> str:
    logger.info(f"Fetching calendar page: {url}")
    r = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (LVTempleBot/1.0)"},
        timeout=30,
    )
    r.raise_for_status()
    return r.text

def sanitize_template_param(s: str) -> str:
    # WhatsApp template param rule: no newlines/tabs, no >4 spaces
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def detect_month_year_context(soup: BeautifulSoup, fallback: date) -> tuple[int, int]:
    """
    Try to infer the month/year currently being displayed in the month view.
    If we can't, fallback to the passed-in date.
    """
    # Many month views include a title like "February 2026"
    page_text = soup.get_text(" ", strip=True)
    # Find month name near a year
    # We keep this permissive; if it fails we fallback.
    m_month = MONTH_NAME_RE.search(page_text)
    m_year = re.search(r"\b(20\d{2})\b", page_text)
    if m_month and m_year:
        month = MONTHS[m_month.group(1).lower()]
        year = int(m_year.group(1))
        return year, month
    return fallback.year, fallback.month

def find_day_header_element(soup: BeautifulSoup, target_day: int):
    """
    Find the element whose text looks like:
      '0 events  18'  or  '2 events,  1'
    and where the day number matches target_day.
    """
    candidates = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "div", "span", "p"])
    for el in candidates:
        txt = el.get_text(" ", strip=True)
        m = DAY_HEADER_RE.match(txt)
        if not m:
            continue
        day_num = int(m.group(2))
        if day_num == target_day:
            return el
    return None

def extract_events_under_day_header(day_header_el) -> list[dict]:
    """
    Starting from the day header element, walk forward until the next day header.
    Collect event links and their nearby times.
    """
    events = []
    seen = set()

    for el in day_header_el.next_elements:
        if hasattr(el, "get_text"):
            txt = el.get_text(" ", strip=True)
            if DAY_HEADER_RE.match(txt):
                break

        if getattr(el, "name", None) == "a" and el.has_attr("href"):
            title = el.get_text(" ", strip=True)
            href = el["href"]
            if not title:
                continue
            if "/event" not in href and "/events" not in href:
                continue

            key = (title, href)
            if key in seen:
                continue
            seen.add(key)

            container = el.find_parent(["article", "div", "li", "td", "section"]) or el.parent
            block_text = container.get_text(" ", strip=True) if container else ""
            m = TIME_RE.search(block_text)
            time_text = m.group(0) if m else None

            events.append({"title": title, "time": time_text})

    return events

def get_events_for_date(target: date) -> list[dict]:
    """
    Fetch month view for target date and extract events ONLY for that day.
    Returns list of {"date": date, "time": str|None, "title": str}
    """
    url = CALENDAR_MONTH_URL.format(date=target.isoformat())
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    day_header = find_day_header_element(soup, target.day)
    if not day_header:
        logger.info(f"Could not locate day header for {target.isoformat()} (day={target.day}).")
        return []

    header_txt = day_header.get_text(" ", strip=True)
    m = DAY_HEADER_RE.match(header_txt)
    count = int(m.group(1)) if m else None
    logger.info(f"{target.isoformat()} header: '{header_txt}'")

    if count == 0:
        return []

    day_events = extract_events_under_day_header(day_header)
    out = []
    for e in day_events:
        out.append({"date": target, "time": e.get("time"), "title": e.get("title")})
    return out

def format_events_7d_one_line(start: date, events: list[dict], max_len: int = MAX_PARAM_LEN) -> str:
    """
    One-line string safe for WhatsApp template params.
    Includes date per event. Example:

    Next 7 days (Feb 18–Feb 24): Feb 18 7:00 pm — Aarti • Feb 19 10:00 am — Puja • ...
    """
    end = start + timedelta(days=6)
    header = f"Next 7 days ({start.strftime('%b %d')}–{end.strftime('%b %d')}):"
    parts = [header]

    # Sort events by date then time text
    def sort_key(e):
        # time_text sorting is best-effort; keep None last
        t = e.get("time") or "zzzz"
        return (e["date"], t.lower())

    for e in sorted(events, key=sort_key):
        d = e["date"].strftime("%b %d")
        t = e.get("time") or "Time TBD"
        title = (e.get("title") or "").strip()
        parts.append(f"{d} {t} — {title}")

    out = " • ".join(parts)
    out = sanitize_template_param(out)

    if len(out) > max_len:
        out = out[: max_len - 10].rstrip(" •|-") + " • (more)"
    return out

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
                            "parameter_name": PARAM_NAME,  # NAMED param
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
# Main
# =======================
def main():
    logger.info("Script started")
    token = os.environ["WHATSAPP_TOKEN"]

    start_date = datetime.now(TZ).date() + timedelta(days=1)  # tomorrow
    dates = [start_date + timedelta(days=i) for i in range(7)]

    all_events: list[dict] = []
    for d in dates:
        try:
            day_events = get_events_for_date(d)
            logger.info(f"Extracted {len(day_events)} events for {d.isoformat()}")
            all_events.extend(day_events)
        except Exception as e:
            logger.error(f"Error extracting events for {d.isoformat()}: {e}")

    if not all_events:
        logger.info(f"No events found for the next 7 days starting {start_date.isoformat()}. Not sending any messages.")
        return

    events_text = format_events_7d_one_line(start_date, all_events)
    logger.info(f"WhatsApp param text: {events_text}")

    sent, failed = 0, 0
    for n in RECIPIENTS:
        try:
            res = send_whatsapp_template(token, n, events_text)
            msg_id = res.get("messages", [{}])[0].get("id")
            logger.info(f"✅ Sent to {n}: {msg_id}")
            sent += 1
        except Exception as e:
            logger.error(f"❌ Failed to send to {n}: {e}")
            failed += 1

        time.sleep(DELAY_BETWEEN_SENDS_SEC)

    logger.info(f"Done. Sent={sent}, Failed={failed}")

if __name__ == "__main__":
    main()
