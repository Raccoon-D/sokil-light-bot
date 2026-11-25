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
from PIL import Image

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


def extract_update_stamp(page_text):
    m = re.search(r"Оновлено:\s*([^\n]+)", page_text)
    return m.group(1).strip() if m else "не вказано"


def extract_schedule_date(page_text, fallback_date):
    m_gpv = re.search(
        r"ГПВ\s+на\s+(\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{4}-\d{1,2}-\d{1,2})",
        page_text,
    )
    if m_gpv:
        parsed = parse_date(m_gpv.group(1))
        if parsed:
            return parsed

    upd = extract_update_stamp(page_text)
    if upd != "не вказано":
        parsed_upd = parse_date(upd)
        if parsed_upd:
            return parsed_upd

    return fallback_date


def parse_gpv_image_4_1(image_path, schedule_date):
    """
    Розпізнає темні клітинки відключень для черги 4.1 напряму з картинки графіка ГПВ
    (2 сітки: 00:00-12:00 та 12:00-24:00, у кожній 12 рядків підчерг 1.1..6.2 і 24 слоти по 30 хв).
    """
    if not Path(image_path).exists():
        return []

    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    if w < 500 or h < 450:
        return []

    px = img.load()

    def is_grid_cell_color(r, g, b):
        # Світло-сіра клітинка (~175..215) або темно-сіра клітинка відключення (~70..130)
        if max(r, g, b) - min(r, g, b) > 35:
            return False
        lum = (r + g + b) / 3.0
        return 60 <= lum <= 220

    # Знаходимо горизонтальний діапазон 24 півгодинних колонок (вони займають праві ~88% таблиці)
    col_scores = []
    for x in range(w):
        cnt = 0
        for y in range(int(h * 0.12), int(h * 0.96), 3):
            if is_grid_cell_color(*px[x, y]):
                cnt += 1
        col_scores.append(cnt)

    max_col = max(col_scores) if col_scores else 0
    if max_col < 20:
        return []

    grid_xs = [x for x, c in enumerate(col_scores) if c > max_col * 0.45]
    if not grid_xs:
        return []
    full_left, full_right = min(grid_xs), max(grid_xs)
    # Перші 2 вузькі колонки — "Черга" і "Підчерга" (~11.5% ширини сітки), далі 24 півгодинні слоти
    slots_left = full_left + (full_right - full_left) * 0.116
    slots_right = full_right
    slot_w = (slots_right - slots_left) / 24.0
    if slot_w < 8:
        return []

    # Знаходимо вертикальні межі двох сіток (верхня 00:00-12:00 і нижня 12:00-24:00)
    row_scores = []
    sample_xs = [int(slots_left + (i + 0.5) * slot_w) for i in range(24)]
    for y in range(h):
        cnt = sum(1 for sx in sample_xs if 0 <= sx < w and is_grid_cell_color(*px[sx, y]))
        row_scores.append(cnt)

    # Групуємо суцільні горизонтальні смуги, де багато клітинок сітки
    bands = []
    in_band = False
    start_y = 0
    for y, c in enumerate(row_scores):
        if c >= 18 and not in_band:
            in_band = True
            start_y = y
        elif c < 18 and in_band:
            in_band = False
            if y - start_y > h * 0.18:
                bands.append((start_y, y - 1))
    if in_band and h - start_y > h * 0.18:
        bands.append((start_y, h - 1))

    if len(bands) < 2:
        return []

    # Беремо 2 найбільші смуги (00:00-12:00 та 12:00-24:00) зверху вниз
    bands = sorted(bands, key=lambda b: b[1] - b[0], reverse=True)[:2]
    bands.sort(key=lambda b: b[0])

    off_slots = [False] * 48  # 48 півгодинних інтервалів за добу

    for half_idx, (top_y, bot_y) in enumerate(bands):
        # Верхній рядок смуги — темна шапка з годинами (~1/13 висоти), під нею 12 рядків підчерг (1.1..6.2)
        band_h = bot_y - top_y
        header_h = band_h / 13.0
        rows_top = top_y + header_h
        row_h = (bot_y - rows_top) / 12.0

        # Черга 4.1 — це 7-й рядок серед 12 підчерг (індекс 6: 1.1=0, 1.2=1, 2.1=2, 2.2=3, 3.1=4, 3.2=5, 4.1=6)
        cy = int(rows_top + (6 + 0.5) * row_h)
        if not (0 <= cy < h):
            continue

        for s_idx in range(24):
            cx = int(slots_left + (s_idx + 0.5) * slot_w)
            if not (0 <= cx < w):
                continue
            r, g, b = px[cx, cy]
            lum = (r + g + b) / 3.0
            # Темно-сіра зафарбована клітинка відключення має яскравість < 135 (світла ~185-205)
            if lum < 138 and (max(r, g, b) - min(r, g, b) < 35):
                off_slots[half_idx * 24 + s_idx] = True

    # Збираємо сусідні півгодинні слоти у безперервні інтервали
    intervals = []
    i = 0
    while i < 48:
        if off_slots[i]:
            j = i
            while j < 48 and off_slots[j]:
                j += 1
            sh, sm = divmod(i * 30, 60)
            eh, em = divmod(j * 30, 60)
            start_dt = datetime.combine(schedule_date, time(sh, sm), TZ)
            if eh == 24:
                end_dt = datetime.combine(schedule_date + timedelta(days=1), time(0, 0), TZ)
            else:
                end_dt = datetime.combine(schedule_date, time(eh, em), TZ)
            intervals.append((start_dt, end_dt))
            i = j
        else:
            i += 1

    return intervals


def extract_events(page_text, default_date):
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in page_text.splitlines()]
    lines = [line for line in lines if line]

    current_date = extract_schedule_date(page_text, default_date)
    events = []

    for idx, line in enumerate(lines):
        found_date = parse_date(line)
        if found_date and not TIME_RE.search(line) and "Оновлено:" not in line:
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
    """Знімає саме зображення графіка ГПВ (як на 5141_3.jpg), без обрізки верху і без форми пошуку внизу."""
    await page.evaluate(
        """() => {
            const hideSelectors = [
                'header', 'nav', '.navbar', '.header', '.modal', '.accessibility-modal',
                '[class*="cookie" i]', '[id*="cookie" i]'
            ];
            for (const sel of hideSelectors) {
                document.querySelectorAll(sel).forEach(el => {
                    el.style.display = 'none';
                });
            }
        }"""
    )

    # Шукаємо найбільший <img> у основній частині сторінки — це і є зображення графіка ГПВ
    best_idx = await page.evaluate(
        """() => {
            const imgs = Array.from(document.querySelectorAll('img'));
            let bestIndex = -1;
            let bestArea = 0;
            imgs.forEach((img, idx) => {
                const rect = img.getBoundingClientRect();
                const src = (img.currentSrc || img.src || '').toLowerCase();
                if (src.includes('logo') || src.includes('icon')) return;
                if (rect.width >= 450 && rect.height >= 350) {
                    const area = rect.width * rect.height;
                    if (area > bestArea) {
                        bestArea = area;
                        bestIndex = idx;
                    }
                }
            });
            return bestIndex;
        }"""
    )

    if best_idx is not None and best_idx >= 0:
        img_loc = page.locator("img").nth(best_idx)
        await img_loc.scroll_into_view_if_needed(timeout=4000)
        await page.wait_for_timeout(500)
        await img_loc.screenshot(path=str(SCREENSHOT_FILE))
        return

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
        page = await browser.new_page(viewport={"width": 1366, "height": 1200})
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

        full_text = "\n---\n".join(texts)
        schedule_date = extract_schedule_date(full_text, datetime.now(TZ).date())

        try:
            await capture_schedule_screenshot(page)
        except Exception as e:
            logging.error("Не вдалося зробити скріншот: %s", e)

    return full_text, SCREENSHOT_FILE, schedule_date


def day_label(target_date, today):
    if target_date == today:
        return "сьогодні"
    if target_date == today + timedelta(days=1):
        return "завтра"
    return target_date.strftime("%d.%m.%Y")


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
    text, shot_path, schedule_date = await fetch_site_data()
    now = datetime.now(TZ)
    today = now.date()
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

    img_events = parse_gpv_image_4_1(shot_path, schedule_date)
    text_events = extract_events(text, schedule_date)
    merged = {(s.isoformat(), e.isoformat()): (s, e) for s, e in (img_events + text_events)}
    all_day_events = sorted(merged.values(), key=lambda x: x[0])
    future_events = [(s, e) for s, e in all_day_events if e > now]

    when_word = day_label(schedule_date, today)
    update_stamp = extract_update_stamp(text)

    if shot_path.exists():
        shot_hash = hashlib.md5(shot_path.read_bytes()).hexdigest()
        caption = (
            f"Графік ({QUEUE}) на {schedule_date:%d.%m.%Y} ({when_word})\n"
            f"Оновлено на сайті: {update_stamp}"
        )
        try:
            telegram_send_photo(shot_path, caption)
            state["last_shot_hash"] = shot_hash
            changed = True
            logging.info("Надіслано скріншот графіка.")
        except Exception as e:
            logging.error("Не вдалося надіслати скріншот: %s", e)

    if SEND_TEXT_SUMMARY:
        if all_day_events:
            lines_txt = "\n".join(
                f"• {s:%H:%M}–{e:%H:%M}" + (" (вже минуло)" if e <= now else "")
                for s, e in all_day_events
            )
            summary_msg = (
                f"⚡️ Черга {QUEUE} ({TOWN_NAME}, {STREET_NAME}) — графік на {when_word} ({schedule_date:%d.%m.%Y})\n"
                f"Відключення світла на {when_word}:\n{lines_txt}\n"
                f"Оновлено на сайті: {update_stamp}"
            )
        else:
            summary_msg = (
                f"✅ Черга {QUEUE} ({TOWN_NAME}, {STREET_NAME}) — графік на {when_word} ({schedule_date:%d.%m.%Y})\n"
                f"На {when_word} відключень по черзі {QUEUE} не заплановано.\n"
                f"Оновлено на сайті: {update_stamp}"
            )
        telegram_send(summary_msg)

    notified = set(state.get("notified", []))

    for start, end in future_events:
        key = event_key(start, end)
        if key not in notified:
            msg = (
                "⚡️ Виявлено майбутнє відключення світла\n\n"
                f"Черга: {QUEUE}\n"
                f"Коли: {day_label(start.date(), today)} ({start:%Y-%m-%d})\n"
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
