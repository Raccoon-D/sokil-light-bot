import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, date, time, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

import requests
from camoufox import AsyncCamoufox
from camoufox_captcha import solve_captcha
from dotenv import load_dotenv

load_dotenv()

SOURCE_URL = "https://energy.volyn.ua/spozhyvacham/perervy-u-elektropostachanni/hrafik-vidkliuchen/#gsc.tab=0"
QUEUE = "4.1"
TOWN_NAME = "Сокіл"
STREET_NAME = "Молодіжна"
TZ = ZoneInfo("Europe/Kyiv")
STATE_FILE = Path("state.json")
SCREENSHOT_FILE = Path("schedule.png")
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None
SEND_TEXT_SUMMARY = os.getenv("SEND_TEXT_SUMMARY", "1").strip().lower() in {"1", "true", "yes"}

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
QUEUE_RE = re.compile(r"(?<![\d.])4\.1(?!\d)")


def build_camoufox_proxy(proxy_url):
    if not proxy_url:
        return None
    normalized = proxy_url if "://" in proxy_url else f"http://{proxy_url}"
    parsed = urlparse(normalized)
    if not parsed.hostname:
        return {"server": normalized}

    scheme = parsed.scheme or "http"
    server = f"{scheme}://{parsed.hostname}"
    if parsed.port:
        server = f"{server}:{parsed.port}"

    proxy_cfg = {"server": server}
    if parsed.username:
        proxy_cfg["username"] = unquote(parsed.username)
    if parsed.password:
        proxy_cfg["password"] = unquote(parsed.password)
    return proxy_cfg


def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("notified", [])
            data.setdefault("last_shot_hash", "")
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"notified": [], "last_shot_hash": ""}


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
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in page_text.splitlines()]
    lines = [line for line in lines if line]

    now = datetime.now(TZ)
    current_date = now.date()
    events = []

    for idx, line in enumerate(lines):
        found_date = parse_date(line)
        if found_date and not TIME_RE.search(line):
            current_date = found_date

        parts = [p.strip() for p in re.split(r"\s*\|\s*|\t+", line) if p.strip()]
        has_queue = ("4.1" in parts) or bool(QUEUE_RE.search(line))
        if not has_queue:
            continue

        neighbor_lines = lines[max(0, idx - 1) : min(len(lines), idx + 3)]
        block = " | ".join(neighbor_lines)

        row_date = parse_date(line) or parse_date(block) or current_date
        ranges = list(TIME_RE.finditer(line)) or list(TIME_RE.finditer(block))

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


async def close_accessibility_modal(page):
    for frame in page.frames:
        try:
            close_btn = frame.locator(
                "button:has-text('Закрити'), button[aria-label*='Закрити' i], button[aria-label*='Close' i]"
            )
            if await close_btn.count() > 0 and await close_btn.first.is_visible():
                await close_btn.first.click(timeout=3000)
                await page.wait_for_timeout(800)
                return
        except Exception:
            continue


async def fill_address_in_frame(frame, page):
    inputs = frame.locator("input:visible")
    inp_count = await inputs.count()
    if inp_count == 0:
        return False

    try:
        town_inp = inputs.nth(0)
        await town_inp.click(timeout=3000)
        await town_inp.fill(TOWN_NAME)
        await page.wait_for_timeout(1500)

        town_opt = frame.locator("[role='option'], .autocomplete-item, li").get_by_text(
            TOWN_NAME, exact=False
        )
        if await town_opt.count() > 0:
            await town_opt.first.click(timeout=3000)
            await page.wait_for_timeout(1000)

        if inp_count >= 2:
            street_inp = inputs.nth(1)
            await street_inp.click(timeout=3000)
            await street_inp.fill(STREET_NAME)
            await page.wait_for_timeout(1500)

            street_opt = frame.locator("[role='option'], .autocomplete-item, li").get_by_text(
                STREET_NAME, exact=False
            )
            if await street_opt.count() > 0:
                await street_opt.first.click(timeout=3000)
                await page.wait_for_timeout(1000)

        find_btn = frame.locator("button:has-text('Знайти'), input[type='submit'][value*='Знайти']")
        if await find_btn.count() > 0 and await find_btn.first.is_visible():
            await find_btn.first.click(timeout=3000)
            await page.wait_for_timeout(4000)
            return True
    except Exception as e:
        logging.debug(f"Помилка заповнення фрейму: {e}")

    return False


async def capture_schedule_screenshot(page):
    # Пробуємо сфокусуватись на блоці графіка/таблиці, щоб скріншот був ближчим до самого графіка
    target_selectors = [
        "table",
        "[class*='schedule' i]",
        "[class*='graph' i]",
        "[class*='gpv' i]",
        "main",
    ]
    for sel in target_selectors:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0:
                box = await loc.first.bounding_box()
                if box and box["width"] > 300 and box["height"] > 200:
                    await loc.first.scroll_into_view_if_needed(timeout=3000)
                    await page.wait_for_timeout(500)
                    clip = {
                        "x": max(0, box["x"] - 20),
                        "y": max(0, box["y"] - 60),
                        "width": min(1366 - max(0, box["x"] - 20), box["width"] + 40),
                        "height": min(1400, box["height"] + 120),
                    }
                    await page.screenshot(path=str(SCREENSHOT_FILE), clip=clip)
                    return
        except Exception:
            continue

    await page.screenshot(path=str(SCREENSHOT_FILE), full_page=False)


async def fetch_site_data():
    texts = []

    camoufox_kwargs = {
        "headless": True,
        "geoip": True,
        "locale": "uk-UA",
        "os": "windows",
        "i_know_what_im_doing": True,
        "config": {"forceScopeAccess": True},
        "disable_coop": True,
    }

    proxy_cfg = build_camoufox_proxy(PROXY_URL)
    if proxy_cfg:
        camoufox_kwargs["proxy"] = proxy_cfg

    async with AsyncCamoufox(**camoufox_kwargs) as browser:
        page = await browser.new_page(viewport={"width": 1366, "height": 900})
        try:
            await page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            logging.warning("Помилка первинного завантаження сторінки: %s", e)

        solved = False
        for attempt in range(1, 5):
            await page.wait_for_timeout(3500 if attempt == 1 else 6000)
            logging.info("Спроба %s/4 вирішення Cloudflare...", attempt)
            try:
                solved = await solve_captcha(
                    page,
                    captcha_type="cloudflare",
                    challenge_type="interstitial",
                )
            except Exception as e:
                logging.warning("Помилка solve_captcha на спробі %s/4: %s", attempt, e)
                solved = False

            if solved:
                break

            if attempt < 4:
                logging.warning(
                    "Спроба %s/4 не пройшла, пробуємо оновити сторінку...", attempt
                )
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    logging.warning(
                        "Пропускаємо помилку reload на спробі %s/4 (%s), чекаємо далі...",
                        attempt,
                        e,
                    )
                    await page.wait_for_timeout(4000)

        if not solved:
            logging.warning("Cloudflare challenge не пройдено після 4 спроб.")

        await page.wait_for_timeout(3000)
        await close_accessibility_modal(page)

        for frame in page.frames:
            try:
                await fill_address_in_frame(frame, page)
            except Exception:
                continue

        await close_accessibility_modal(page)

        for frame in page.frames:
            try:
                rows_text = await frame.evaluate(
                    """() => {
                        const rows = Array.from(document.querySelectorAll('tr'));
                        const tablePart = rows
                            .map(r => Array.from(r.querySelectorAll('th, td'))
                                .map(c => c.innerText.replace(/\\s+/g, ' ').trim())
                                .filter(Boolean)
                                .join(' | '))
                            .filter(Boolean)
                            .join('\\n');
                        const bodyPart = document.body ? document.body.innerText : '';
                        return (tablePart + '\\n' + bodyPart).trim();
                    }"""
                )
                if rows_text:
                    texts.append(rows_text)
            except Exception:
                continue

        try:
            await capture_schedule_screenshot(page)
        except Exception as e:
            logging.error("Не вдалося зробити скріншот: %s", e)

    return "\n---\n".join(texts), SCREENSHOT_FILE


def telegram_send(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message[:4000],
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")
    return payload


def telegram_send_photo(photo_path, caption=""):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    with open(photo_path, "rb") as f:
        response = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "caption": caption[:1000],
            },
            files={"photo": f},
            timeout=30,
        )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")
    return payload


def event_key(start, end):
    return f"{start.isoformat()}|{end.isoformat()}"


def extract_update_stamp(page_text):
    m = re.search(r"Оновлено:\s*([^\n]+)", page_text)
    return m.group(1).strip() if m else "не вказано"


async def check_once():
    logging.info("Перевіряю офіційний графік для черги %s", QUEUE)
    text, shot_path = await fetch_site_data()
    now = datetime.now(TZ)
    state = load_state()
    changed = False

    is_cf_blocked = (
        not text.strip()
        or "Триває перевірка безпеки" in text
        or "Трохи зачекайте" in text
        or "Cloudflare" in text
        or "Ray ID:" in text
        or "Verify you are human" in text
    )

    if is_cf_blocked:
        raise RuntimeError(
            "Cloudflare Challenge: графік недоступний для бота."
        )

    events = extract_events(text)
    has_4_1_text = ("4.1" in text) or bool(events)
    update_stamp = extract_update_stamp(text)

    if shot_path.exists():
        shot_hash = hashlib.md5(shot_path.read_bytes()).hexdigest()
        caption = (
            f"Графік ({QUEUE}) | {TOWN_NAME}, {STREET_NAME}\n"
            f"Перевірено: {now:%Y-%m-%d %H:%M} | Оновлено на сайті: {update_stamp}"
        )
        try:
            telegram_send_photo(shot_path, caption)
            state["last_shot_hash"] = shot_hash
            changed = True
            logging.info("Надіслано скріншот графіка.")
        except Exception as e:
            logging.error("Не вдалося надіслати скріншот: %s", e)

    if SEND_TEXT_SUMMARY:
        if events:
            lines_txt = "\n".join(
                f"• {s:%Y-%m-%d} | {s:%H:%M}–{e:%H:%M}" for s, e in events
            )
            summary_msg = (
                f"📋 Звіт по черзі {QUEUE} ({TOWN_NAME}, {STREET_NAME})\n"
                f"Оновлено на сайті: {update_stamp}\n"
                f"Знайдено інтервалів:\n{lines_txt}"
            )
        else:
            summary_msg = (
                f"📋 Звіт по черзі {QUEUE} ({TOWN_NAME}, {STREET_NAME})\n"
                f"Оновлено на сайті: {update_stamp}\n"
                f"Згадка 4.1 у тексті: {'так' if has_4_1_text else 'ні (графік може бути на сітці/картинці)'}\n"
                "Майбутніх текстових інтервалів не знайдено."
            )
        telegram_send(summary_msg)

    notified = set(state.get("notified", []))

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
