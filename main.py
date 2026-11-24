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

        # Беремо поточний рядок і сусідні рядки цього ж запису таблиці
        neighbor_lines = lines[max(0, idx - 2) : min(len(lines), idx + 4)]
        block = " | ".join(neighbor_lines)

        row_date = parse_date(block) or current_date
        ranges = list(TIME_RE.finditer(block))

        for m in ranges:
            sh, sm, eh, em = map(int, m.groups())
            # Відсікаємо заголовки шкали на кшталт 00:00-12:00 або 12:00-24:00, якщо вони не схожі на інтервал відключення
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
        page = await browser.new_page(locale="uk-UA", timezone_id="Europe/Kyiv")
        await page.goto(SOURCE_URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(5000)

        for frame in page.frames:
            try:
                # Збираємо рядки таблиць через табуляцію, щоб колонки не злипалися
                rows_text = await frame.evaluate(
                    """() => {
                        const rows = Array.from(document.querySelectorAll('tr'));
                        if (rows.length > 0) {
                            return rows
                                .map(r => Array.from(r.querySelectorAll('th, td'))
                                    .map(c => c.innerText.trim())
                                    .filter(Boolean)
                                    .join(' | '))
                                .filter(Boolean)
                                .join('\\n');
                        }
                        return document.body ? document.body.innerText : '';
                    }"""
                )
                if rows_text and rows_text.strip():
                    texts.append(rows_text)
            except Exception:
                continue
        await browser.close()
    return "\n".join(texts)


def telegram_send(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
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
    if not text.strip():
        raise RuntimeError("Сайт не повернув текст графіка.")

    events = extract_events(text)
    if not events:
        logging.info(
            "Майбутніх або активних інтервалів для черги %s не знайдено або структура сторінки змінилася.",
            QUEUE,
        )
        return

    now = datetime.now(TZ)
    state = load_state()
    notified = set(state.get("notified", []))
    changed = False

    for start, end in events:
        key = event_key(start, end)
        if key not in notified:
            msg = (
                "⚡️ Виявлено майбутнє відключення світла\n\n"
                f"Черга: {QUEUE}\n"
                f"Дата: {start:%Y-%m-%d}\n"
                f"Інтервал: {start:%H:%M}–{end:%H:%M}\n\n"
                "Джерело: Волиньобленерго\n"
                "Фактичний час може відрізнятися від графіка."
            )
            telegram_send(msg)
            notified.add(key)
            changed = True
            logging.info("Надіслано сповіщення для події %s", key)

    cutoff = now - timedelta(days=2)
    cleaned = []
    for key in notified:
        try:
            stamp = datetime.fromisoformat(key.split("|", 1)[0])
            if stamp >= cutoff:
                cleaned.append(key)
        except ValueError:
            pass

    if cleaned != state.get("notified", []) or changed:
        state["notified"] = cleaned
        save_state(state)


async def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit(
            "Заповни TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID у змінних середовища або .env."
        )

    if RUN_ONCE:
        await check_once()
        return

    telegram_send(
        "✅ Бот моніторингу графіка запущений.\n"
        f"Черга: {QUEUE}\n"
        f"Перевірка: кожні {CHECK_EVERY_SECONDS // 60} хв.\n"
        "Сповіщення: одразу після виявлення нового майбутнього відключення."
    )
    while True:
        try:
            await check_once()
        except Exception:
            logging.exception("Помилка перевірки")
        await asyncio.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
    
