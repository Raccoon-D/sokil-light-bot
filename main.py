import asyncio
import json
import logging
import os
import re
from datetime import datetime, date, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

SOURCE_URL = "https://energy.volyn.ua/spozhyvacham/perervy-u-elektropostachanni/hrafik-vidkliuchen/pagephp.php"
QUEUE = "4.1"
TZ = ZoneInfo("Europe/Kyiv")
STATE_FILE = Path("state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
RUN_ONCE = os.getenv("RUN_ONCE", "0").strip().lower() in {"1", "true", "yes"}
CHECK_EVERY_SECONDS = int(os.getenv("CHECK_EVERY_SECONDS", "300"))

TIME_RE = re.compile(
    r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)\s*[-–—]\s*([01]?\d|2[0-3]):([0-5]\d)(?!\d)"
)
DATE_DMY_RE = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4})(?!\d)")
DATE_YMD_RE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
QUEUE_RE = re.compile(r"(?<![\d.])4[.,]1(?!\d)")


def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("notified"), list):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"notified": []}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_date(text):
    m_ymd = DATE_YMD_RE.search(text)
    if m_ymd:
        try:
            return date(int(m_ymd.group(1)), int(m_ymd.group(2)), int(m_ymd.group(3)))
        except ValueError:
            pass

    m_dmy = DATE_DMY_RE.search(text)
    if m_dmy:
        try:
            return date(int(m_dmy.group(3)), int(m_dmy.group(2)), int(m_dmy.group(1)))
        except ValueError:
            pass

    return None


def extract_events(page_text):
    """
    Аналізує сторінку по рядках, щоб коректно прив'язати дату і час до черги 4.1.
    """
    lines = [re.sub(r"\s+", " ", line).strip() for line in page_text.splitlines()]
    lines = [line for line in lines if line]

    now = datetime.now(TZ)
    now_date = now.date()
    current_date = now_date
    events = []

    for idx, line in enumerate(lines):
        found_date = parse_date(line)
        if found_date and not TIME_RE.search(line):
            current_date = found_date

        if not QUEUE_RE.search(line):
            continue

        neighbor_lines = lines[max(0, idx - 2) : min(len(lines), idx + 4)]
        block = " | ".join(neighbor_lines)

        row_date = parse_date(block) or current_date
        ranges = list(TIME_RE.finditer(block))

        for m in ranges:
            sh, sm, eh, em = map(int, m.groups())
            if (sh, sm, eh, em) in {(0, 0, 12, 0), (12, 0, 24, 0)}:
                continue

            start = datetime.combine(row_date, time(sh, sm), TZ)
            end = datetime.combine(row_date, time(eh, em), TZ)
            if end <= start:
                end += timedelta(days=1)

            if end > now:
                events.append((start, end))

    unique = {}
    for start, end in events:
        unique[(start.isoformat(), end.isoformat())] = (start, end)
    return sorted(unique.values(), key=lambda x: x[0])


async def fetch_site_text():
    texts = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        
