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
DEBUG_REPORT = os.getenv("DEBUG_REPORT", "1").strip().lower() in {"1", "true", "yes"}

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

        buttons = frame.locator("button:visible, input[type='submit']:visible")
        if await buttons.count() > 0:
            await buttons.first.click(timeout=3000)
            await page.wait_for_timeout(5000)
            return True
    except Exception as e:
        logging.debug(f"Помилка заповнення фрейму: {e}")

    return False


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
        await page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)

        solved = await solve_captcha(
            page,
            captcha_type="cloudflare",
            challenge_type="interstitial",
        )
        if not solved:
            await page.reload(wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)
            solved = await solve_captcha(
                page,
                captcha_type="cloudflare",
                challenge_type="interstitial",
            )
        if not solved:
            logging.warning(
                "Cloudflare challenge не пройдено автоматично, робимо скріншот поточного стану..."
            )

        await page.wait_for_timeout(8000)

        for frame in page.frames:
            try:
                await fill_address_in_frame(frame, page)
            except Exception:
                continue

        for frame in page.frames:
            try:
                selects = frame.locator("select")
                count = await selects.count()
                for i in range(count):
                    sel = selects.nth(i)
                    options = sel.locator("option")
                    opt_count = await options.count()
                    if opt_count > 1:
                        val = await options.nth(opt_count - 1).get_attribute("value")
                        if val:
                            await sel.select_option(val)
                            await page.wait_for_timeout(3000)
            except Exception:
                continue

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
            await page.screenshot(path=str(SCREENSHOT_FILE), full_page=True)
        except Exception as e:
            logging.error(f"Не вдалося зробити скріншот: {e}")

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

    events = [] if is_cf_blocked else extract_events(text)

    if shot_path.exists():
        shot_hash = hashlib.md5(shot_path.read_bytes()).hexdigest()
        status_tag = "CF_BLOCK" if is_cf_blocked else f"Подій: {len(events)}"
        if DEBUG_REPORT or shot_hash != state.get("last_shot_hash", ""):
            caption = f"Скріншот ({QUEUE}) | {TOWN_NAME}, {STREET_NAME} | {now:%Y-%m-%d %H:%M} | {status_tag}"
            try:
                telegram_send_photo(shot_path, caption)
                state["last_shot_hash"] = shot_hash
                changed = True
                logging.info("Надіслано скріншот графіка.")
            except Exception as e:
                logging.error(f"Не вдалося надіслати скріншот: {e}")

    if is_cf_blocked:
        if changed:
            save_state(state)
        raise RuntimeError(
            "Cloudflare Challenge: графік недоступний для бота."
        )

    if not events:
        logging.info(
            "Майбутніх або активних інтервалів для черги %s не знайдено.",
            QUEUE,
        )
        if changed:
            save_state(state)
        return

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
                
