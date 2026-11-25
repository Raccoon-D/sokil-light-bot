import asyncio
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
SHOT_SEARCH_FILE = Path("schedule_search.png")
SHOT_GRIDS_FILE = Path("schedule_grids.png")
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


async def click_select_button(frame, page):
    candidates = frame.locator(
        "button:has-text('Вибрати'), input[type='submit'][value*='Вибрати'], "
        "input[type='button'][value*='Вибрати'], a:has-text('Вибрати'), "
        "button:has-text('Знайти'), input[type='submit'][value*='Знайти']"
    )
    cnt = await candidates.count()
    for i in range(cnt):
        btn = candidates.nth(i)
        try:
            if await btn.is_visible():
                await btn.scroll_into_view_if_needed(timeout=3000)
                await page.wait_for_timeout(300)
                await btn.click(timeout=4000)
                await page.wait_for_timeout(4500)
                logging.info("Натиснуто кнопку 'Вибрати'.")
                return True
        except Exception:
            continue

    try:
        clicked_js = await frame.evaluate(
            """() => {
                const els = Array.from(document.querySelectorAll('button, input[type="submit"], input[type="button"], a'));
                const target = els.find(el => {
                    const t = (el.innerText || el.value || '').trim();
                    const r = el.getBoundingClientRect();
                    return (t === 'Вибрати' || t.includes('Вибрати')) && r.width > 0 && r.height > 0;
                });
                if (!target) return false;
                target.scrollIntoView({block: 'center'});
                target.click();
                return true;
            }"""
        )
        if clicked_js:
            await page.wait_for_timeout(4500)
            logging.info("Натиснуто кнопку 'Вибрати' через JS.")
            return True
    except Exception:
        pass

    return False


async def fill_address_in_frame(frame, page):
    text_inputs = frame.locator("input[type='text']:visible, input:not([type]):visible")
    inp_count = await text_inputs.count()
    if inp_count == 0:
        return False

    try:
        town_inp = text_inputs.nth(0)
        await town_inp.scroll_into_view_if_needed(timeout=3000)
        await town_inp.click(timeout=3000)
        await town_inp.fill("")
        await town_inp.type(TOWN_NAME, delay=60)
        await page.wait_for_timeout(1200)

        town_opt = frame.locator("[role='option'], .autocomplete-item, li").get_by_text(
            TOWN_NAME, exact=False
        )
        if await town_opt.count() > 0:
            try:
                if await town_opt.first.is_visible():
                    await town_opt.first.click(timeout=2000)
                    await page.wait_for_timeout(700)
            except Exception:
                pass

        if inp_count >= 2:
            street_inp = text_inputs.nth(1)
            await street_inp.scroll_into_view_if_needed(timeout=3000)
            await street_inp.click(timeout=3000)
            await street_inp.fill("")
            await street_inp.type(STREET_NAME, delay=60)
            await page.wait_for_timeout(1200)

            street_opt = frame.locator("[role='option'], .autocomplete-item, li").get_by_text(
                STREET_NAME, exact=False
            )
            if await street_opt.count() > 0:
                try:
                    if await street_opt.first.is_visible():
                        await street_opt.first.click(timeout=2000)
                        await page.wait_for_timeout(700)
                except Exception:
                    pass

        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
        except Exception:
            pass

        return await click_select_button(frame, page)
    except Exception as e:
        logging.warning("Помилка заповнення форми: %s", e)

    return False


async def capture_two_screenshots(page):
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

    await page.set_viewport_size({"width": 1366, "height": 1600})

    search_box = await page.evaluate(
        """() => {
            const els = Array.from(document.querySelectorAll('section, article, main, div, form'));
            for (const el of els) {
                const txt = (el.innerText || '');
                if (txt.includes('Пошук вашої адреси') || (txt.includes('Вибрати') && txt.includes('вулиц'))) {
                    const r = el.getBoundingClientRect();
                    if (r.width >= 400 && r.height >= 160 && r.height <= 1400) {
                        return {
                            x: r.x + window.scrollX,
                            y: r.y + window.scrollY,
                            width: r.width,
                            height: r.height,
                        };
                    }
                }
            }
            return null;
        }"""
    )

    if search_box:
        sy = max(0.0, float(search_box["y"]) - 20.0)
        sh = min(1150.0, max(320.0, float(search_box["height"]) + 80.0))
        sx = max(0.0, float(search_box["x"]) - 10.0)
        sw = min(1366.0, float(search_box["width"]) + 20.0)
        await page.evaluate("(y) => window.scrollTo(0, y)", sy)
        await page.wait_for_timeout(300)
        await page.screenshot(
            path=str(SHOT_SEARCH_FILE),
            clip={"x": sx, "y": sy, "width": sw, "height": sh},
        )
    else:
        await page.screenshot(path=str(SHOT_SEARCH_FILE), full_page=False)

    grid_box = await page.evaluate(
        """() => {
            const els = Array.from(document.querySelectorAll('section, article, main, div, img, table'));
            let best = null;
            let bestArea = Infinity;
            for (const el of els) {
                const txt = (el.innerText || '');
                const r = el.getBoundingClientRect();
                const isGraphBlock = (txt.includes('00:00') || txt.includes('12:00') || txt.includes('ГПВ'));
                if (!isGraphBlock && el.tagName !== 'IMG') continue;
                if (r.width < 450 || r.height < 260 || r.height > 2400) continue;
                const area = r.width * r.height;
                if (area < bestArea) {
                    bestArea = area;
                    best = {
                        x: r.x + window.scrollX,
                        y: r.y + window.scrollY,
                        width: r.width,
                        height: r.height,
                    };
                }
            }
            return best;
        }"""
    )

    if grid_box:
        gy = max(0.0, float(grid_box["y"]) - 40.0)
        gh = min(1800.0, max(350.0, float(grid_box["height"]) + 60.0))
        gx = max(0.0, float(grid_box["x"]) - 10.0)
        gw = min(1366.0, float(grid_box["width"]) + 20.0)
        await page.evaluate("(y) => window.scrollTo(0, y)", gy)
        await page.wait_for_timeout(300)
        await page.screenshot(
            path=str(SHOT_GRIDS_FILE),
            clip={"x": gx, "y": gy, "width": gw, "height": gh},
        )
    else:
        await page.screenshot(path=str(SHOT_GRIDS_FILE), full_page=False)

    return SHOT_SEARCH_FILE, SHOT_GRIDS_FILE


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
                clicked = await fill_address_in_frame(frame, page)
                if clicked:
                    break
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
            search_shot, grids_shot = await capture_two_screenshots(page)
        except Exception as e:
            logging.error("Не вдалося зробити скріншоти: %s", e)
            search_shot, grids_shot = SHOT_SEARCH_FILE, SHOT_GRIDS_FILE

    return full_text, search_shot, grids_shot, schedule_date


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


def has_no_outages_phrase(page_text):
    lower = page_text.lower()
    return any(
        phrase in lower
        for phrase in [
            "відключень не заплановано",
            "немає відключень",
            "графік відсутній",
            "не знайдено",
        ]
    )


async def check_once():
    logging.info("Перевіряю офіційний графік для черги %s", QUEUE)
    text, search_shot, grids_shot, schedule_date = await fetch_site_data()
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

    events = extract_events(text, schedule_date)
    when_word = day_label(schedule_date, today)
    update_stamp = extract_update_stamp(text)

    if search_shot.exists():
        try:
            telegram_send_photo(
                search_shot,
                f"Пошук ({TOWN_NAME}, {STREET_NAME}) | {when_word} ({schedule_date:%d.%m.%Y})\nОновлено: {update_stamp}",
            )
        except Exception as e:
            logging.error("Не вдалося надіслати скріншот пошуку: %s", e)

    if grids_shot.exists():
        try:
            telegram_send_photo(
                grids_shot,
                f"Графіки ({QUEUE}) | {when_word} ({schedule_date:%d.%m.%Y})",
            )
        except Exception as e:
            logging.error("Не вдалося надіслати скріншот графіків: %s", e)

    if SEND_TEXT_SUMMARY:
        if events:
            lines_txt = "\n".join(
                f"• {day_label(s.date(), today)} ({s:%d.%m}): {s:%H:%M}–{e:%H:%M}"
     
