import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

from bot_core import (
    QUEUE,
    STREET_NAME,
    TOWN_NAME,
    TZ,
    extract_events,
    extract_update_stamp,
    fetch_site_data,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

STATE_FILE = Path("state.json")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
RUN_ONCE = os.getenv("RUN_ONCE", "0").strip().lower() in {"1", "true", "yes"}
CHECK_EVERY_SECONDS = int(os.getenv("CHECK_EVERY_SECONDS", "300"))
SEND_TEXT_SUMMARY = os.getenv("SEND_TEXT_SUMMARY", "1").strip().lower() in {"1", "true", "yes"}


def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("notified", [])
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"notified": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def day_label(target_date, today):
    if target_date == today:
        return "сьогодні"
    if target_date == today + timedelta(days=1):
        return "завтра"
    return target_date.strftime("%d.%m.%Y")


def telegram_send(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={"chat_id": CHAT_ID, "text": message[:4000], "disable_web_page_preview": True},
        timeout=20,
    )
    logging.info("sendMessage status=%s body=%s", r.status_code, r.text[:300])
    r.raise_for_status()
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")


def telegram_send_photo(photo_path, caption=""):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    with open(photo_path, "rb") as f:
        r = requests.post(
            url,
            data={"chat_id": CHAT_ID, "caption": caption[:1000]},
            files={"photo": f},
            timeout=30,
        )
    logging.info("sendPhoto (%s) status=%s body=%s", photo_path.name, r.status_code, r.text[:300])
    r.raise_for_status()
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")


def has_no_outages_phrase(page_text):
    lower = page_text.lower()
    return any(p in lower for p in ["відключень не заплановано", "немає відключень", "графік відсутній", "не знайдено"])


async def check_once():
    logging.info("Перевіряю офіційний графік для черги %s (chat_id=%s)", QUEUE, CHAT_ID or "EMPTY")
    text, search_shot, grids_shot, schedule_date = await fetch_site_data()
    now = datetime.now(TZ)
    today = now.date()
    state = load_state()
    changed = False

    logging.info(
        "text_len=%s | schedule_date=%s | search_exists=%s | grids_exists=%s",
        len(text),
        schedule_date,
        search_shot.exists(),
        grids_shot.exists(),
    )

    if not text.strip() or any(
        k in text for k in ["Триває перевірка безпеки", "Трохи зачекайте", "Cloudflare", "Ray ID:", "Verify you are human"]
    ):
        raise RuntimeError("Cloudflare Challenge: графік недоступний для бота.")

    events = extract_events(text, schedule_date)
    when_word = day_label(schedule_date, today)
    update_stamp = extract_update_stamp(text)
    logging.info("events_count=%s | update_stamp=%s", len(events), update_stamp)

    if search_shot.exists():
        try:
            telegram_send_photo(
                search_shot,
                f"Пошук ({TOWN_NAME}, {STREET_NAME}) | {when_word} ({schedule_date:%d.%m.%Y})\nОновлено: {update_stamp}",
            )
        except Exception as e:
            logging.error("Помилка фото пошуку: %s", e)

    if grids_shot.exists():
        try:
            telegram_send_photo(grids_shot, f"Графіки ({QUEUE}) | {when_word} ({schedule_date:%d.%m.%Y})")
        except Exception as e:
            logging.error("Помилка фото графіків: %s", e)

    if SEND_TEXT_SUMMARY:
        if events:
            lines_txt = "\n".join(f"• {day_label(s.date(), today)} ({s:%d.%m}): {s:%H:%M}–{e:%H:%M}" for s, e in events)
            msg = (
                f"⚡️ Черга {QUEUE} ({TOWN_NAME}, {STREET_NAME}) — {when_word} ({schedule_date:%d.%m.%Y})\n"
                f"Відключення:\n{lines_txt}\nОновлено: {update_stamp}"
            )
        elif has_no_outages_phrase(text):
            msg = (
                f"✅ Черга {QUEUE} ({TOWN_NAME}, {STREET_NAME}) — {when_word} ({schedule_date:%d.%m.%Y})\n"
                f"На {when_word} відключень немає (або їх вже немає).\nОновлено: {update_stamp}"
            )
        else:
            msg = (
                f"ℹ️ Черга {QUEUE} ({TOWN_NAME}, {STREET_NAME}) — {when_word} ({schedule_date:%d.%m.%Y})\n"
                f"Дивись графіки на другому фото та результат пошуку на першому.\nОновлено: {update_stamp}"
            )
        telegram_send(msg)

    notified = set(state.get("notified", []))
    for start, end in events:
        if end <= now:
            continue
        key = f"{start.isoformat()}|{end.isoformat()}"
        if key not in notified:
            telegram_send(
                f"⚡️ Виявлено майбутнє відключення світла\n\n"
                f"Черга: {QUEUE}\nКоли: {day_label(start.date(), today)} ({start:%Y-%m-%d})\n"
                f"Інтервал: {start:%H:%M}–{end:%H:%M}\n\nДжерело: Волиньобленерго"
            )
            notified.add(key)
            changed = True

    if changed:
        state["notified"] = sorted(notified)
        save_state(state)


async def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Заповни TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID.")
    if RUN_ONCE:
        await check_once()
        return
    telegram_send(f"✅ Бот моніторингу графіка запущений.\nЧерга: {QUEUE}\nПеревірка: кожні {CHECK_EVERY_SECONDS // 60} хв.")
    while True:
        try:
            await check_once()
        except Exception:
            logging.exception("Помилка перевірки")
        await asyncio.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
    
