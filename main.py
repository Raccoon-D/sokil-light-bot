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
CHECK_EVERY_SECONDS = 300
TZ = ZoneInfo("Europe/Kyiv")
STATE_FILE = Path("state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)\s*[-–—]\s*([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4})(?!\d)")

def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"notified": []}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def parse_date(text):
    m = DATE_RE.search(text)
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None

def extract_events(page_text):
    """
    Heuristic parser: finds text around the exact queue label and extracts
    time ranges. It intentionally returns no events if the page structure
    cannot be recognized, rather than guessing a different queue.
    """
    text = re.sub(r"[ \t]+", " ", page_text)
    events = []
    positions = [m.start() for m in re.finditer(r"(?<![\d.])4[.,]1(?!\d)", text)]
    now_date = datetime.now(TZ).date()

    for pos in positions:
        chunk = text[max(0, pos - 500): min(len(text), pos + 1800)]
        ranges = list(TIME_RE.finditer(chunk))
        if not ranges:
            continue

        # Dates may be shown before or after the queue row. Use an explicit
        # date when available; otherwise the current local date is assumed.
        event_date = parse_date(chunk) or now_date

        for m in ranges:
            sh, sm, eh, em = map(int, m.groups())
            start = datetime.combine(event_date, time(sh, sm), TZ)
            end = datetime.combine(event_date, time(eh, em), TZ)
            if end <= start:
                end += timedelta(days=1)
            if start.date() == now_date or start >= datetime.now(TZ):
                events.append((start, end))

    # Unique start/end pairs
    unique = {}
    for start, end in events:
        unique[(start.isoformat(), end.isoformat())] = (start, end)
    return sorted(unique.values(), key=lambda x: x[0])

async def fetch_site_text():
    texts = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(locale="uk-UA", timezone_id="Europe/Kyiv")
        await page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)

        # Read the main page and all embedded frames; the schedule widget is
        # loaded separately on the official page.
        for frame in page.frames:
            try:
                body = await frame.locator("body").inner_text(timeout=8000)
                if body.strip():
                    texts.append(body)
            except Exception:
                continue
        await browser.close()
    return "\n".join(texts)

def telegram_send(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": True},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")
    return payload

def event_key(start, end):
    return f"{start.isoformat()}|{end.isoformat()}"

async def check_once():
    logging.info("Перевіряю офіційний графік для черги %s", QUEUE)
    text = await fetch_site_text()
    if not text:
        raise RuntimeError("Сайт не повернув текст графіка.")

    events = extract_events(text)
    if not events:
        logging.warning(
            "Не вдалося автоматично розпізнати часові інтервали черги %s. "
            "Можливо, сайт змінив структуру або графік ще не опублікований. "
            "Повідомлення не надсилаю, щоб не вигадувати час.",
            QUEUE,
        )
        return

    now = datetime.now(TZ)
    state = load_state()
    notified = set(state.get("notified", []))

    for start, end in events:
        key = event_key(start, end)
        alert_at = start - timedelta(minutes=ALERT_BEFORE_MINUTES)

        if alert_at <= now < start and key not in notified:
            minutes_left = max(0, int((start - now).total_seconds() // 60))
            msg = (
                "⚡️ Попередження про відключення світла\n\n"
                f"Черга: {QUEUE}\n"
                f"Початок за графіком: {start:%H:%M}\n"
                f"Орієнтовне завершення: {end:%H:%M}\n"
                f"Залишилось приблизно: {minutes_left} хв.\n\n"
                "Джерело: Волиньобленерго\n"
                "Зверни увагу: фактичний час може відрізнятися від графіка."
            )
            telegram_send(msg)
            notified.add(key)
            logging.info("Надіслано попередження для %s", start.isoformat())

    # Keep only recent/future keys to prevent state file growing forever.
    cutoff = now - timedelta(days=2)
    cleaned = []
    for key in notified:
        try:
            stamp = datetime.fromisoformat(key.split("|", 1)[0])
            if stamp >= cutoff:
                cleaned.append(key)
        except ValueError:
            pass
    state["notified"] = cleaned
    save_state(state)

async def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit(
            "Заповни TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID у файлі .env "
            "або змінних середовища."
        )
    telegram_send(
        "✅ Бот моніторингу графіка запущений.\n"
        f"Черга: {QUEUE}\n"
        f"Перевірка: кожні {CHECK_EVERY_SECONDS // 60} хв.\n"
        "Сповіщення: одразу після виявлення планового відключення у графіку."
    )
    while True:
        try:
            await check_once()
        except Exception:
            logging.exception("Помилка перевірки")
        await asyncio.sleep(CHECK_EVERY_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())
      
