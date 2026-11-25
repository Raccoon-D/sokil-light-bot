import asyncio
import json
import logging
import os
import re
from datetime import date, datetime, time, timedelta
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

PROXY_URL = os.getenv("PROXY_URL", "").strip() or None
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
RUN_ONCE = os.getenv("RUN_ONCE", "0").strip().lower() in {"1", "true", "yes"}
CHECK_EVERY_SECONDS = int(os.getenv("CHECK_EVERY_SECONDS", "300"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

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
    cfg = {"server": server}
    if parsed.username:
        cfg["username"] = unquote(parsed.username)
    if parsed.password:
        cfg["password"] = unquote(parsed.password)
    return cfg


def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("last_signature", "")
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"last_signature": ""}


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
    return m.group(1).strip() if m else ""


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
    if upd:
        parsed_upd = parse_date(upd)
        if parsed_upd:
            return parsed_upd
    return fallback_date


def extract_houses(page_text):
    houses = []
    seen = set()
    for raw_line in page_text.splitlines():
        line = re.sub(r"\s*\|\s*", " | ", re.sub(r"[ \t]+", " ", raw_line)).strip()
        if not line:
            continue
        low = line.lower()
        if ("сокіл" in low and "молодіжна" in low and "4.1" in line) or (
            "молодіжна" in low and "4.1" in line
        ):
            if "|" in line:
                parts = [p.strip() for p in line.split("|")]
                for idx, part in enumerate(parts):
                    if "молодіжна" in part.lower() and idx + 1 < len(parts):
                        candidate = parts[idx + 1].strip()
                        if candidate and candidate != "4.1" and candidate not in seen:
                            seen.add(candidate)
                            houses.append(candidate)
            else:
                m = re.search(
                    r"Молодіжна\s+(.+?)\s+4\.1\b",
                    line,
                    flags=re.IGNORECASE,
                )
                if m:
                    candidate = m.group(1).strip(" ,|")
                    if candidate and candidate not in seen:
                        seen.add(candidate)
                        houses.append(candidate)
    return houses


def extract_events(page_text, default_date):
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in page_text.splitlines()]
    lines = [line for line in lines if line]
    current_date = extract_schedule_date(page_text, default_date)
    events = []

    for idx, line in enumerate(lines):
        found_date = parse_date(line)
        if found_date and not TIME_RE.search(line) and "Оновлено:" not in line:
            current_date = found_date

        low = line.lower()
        is_target = ("4.1" in line or QUEUE_RE.search(line)) and (
            "сокіл" in low or "молодіжна" in low or TIME_RE.search(line)
        )
        if not is_target:
            continue

        neighbor = " | ".join(lines[max(0, idx - 1) : min(len(lines), idx + 2)])
        row_date = parse_date(line) or parse_date(neighbor) or current_date
        for m in TIME_RE.finditer(line + " " + neighbor):
            sh, sm, eh, em = map(int, m.groups())
            start = datetime.combine(row_date, time(sh, sm), TZ)
            end = datetime.combine(row_date, time(eh, em), TZ)
            if end <= start:
                end += timedelta(days=1)
            events.append((start, end))

    unique = {(s.isoformat(), e.isoformat()): (s, e) for s, e in events}
    return sorted(unique.values(), key=lambda x: x[0])


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
        json={
            "chat_id": CHAT_ID,
            "text": message[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    logging.info("sendMessage status=%s body=%s", r.status_code, r.text[:300])
    r.raise_for_status()
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")


async def close_accessibility_modal(page):
    for frame in page.frames:
        try:
            btn = frame.locator(
                "button:has-text('Закрити'), button[aria-label*='Закрити' i], button[aria-label*='Close' i]"
            )
            if await btn.count() > 0 and await btn.first.is_visible():
                await btn.first.click(timeout=3000)
                await page.wait_for_timeout(600)
                return
        except Exception:
            continue


async def select_today_date(frame, page, target_date):
    variants = [
        target_date.strftime("%Y-%m-%d"),
        target_date.strftime("%d.%m.%Y"),
        target_date.strftime("%d-%m-%Y"),
    ]
    selects = frame.locator("select:visible")
    for i in range(await selects.count()):
        sel = selects.nth(i)
        try:
            options = sel.locator("option")
            for j in range(await options.count()):
                opt = options.nth(j)
                txt = ((await opt.inner_text()) or "").strip()
                val = ((await opt.get_attribute("value")) or "").strip()
                if any(v in txt or v in val for v in variants):
                    await sel.select_option(index=j)
                    await page.wait_for_timeout(600)
                    logging.info("Обрано дату ГПВ у списку: %s", txt or val)
                    return True
        except Exception:
            continue
    return False


async def _fill_single_field(inp, page, value):
    try:
        await inp.scroll_into_view_if_needed(timeout=4000)
    except Exception:
        pass
    await page.wait_for_timeout(150)

    try:
        await inp.click(timeout=3000)
        await inp.fill("")
        await inp.type(value, delay=60, timeout=5000)
        await page.wait_for_timeout(200)
        return True
    except Exception:
        pass

    handle = await inp.element_handle(timeout=3000)
    if handle:
        await page.evaluate(
            """([el, val]) => {
                el.scrollIntoView({block: 'center', inline: 'center'});
                el.focus();
                el.value = '';
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.value = val;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            [handle, value],
        )
        await page.wait_for_timeout(200)
        return True
    return False


async def click_select_button(frame, page):
    candidates = frame.locator(
        "button:has-text('Вибрати'), input[type='submit'][value*='Вибрати'], "
        "input[type='button'][value*='Вибрати'], a:has-text('Вибрати')"
    )
    for i in range(await candidates.count()):
        btn = candidates.nth(i)
        try:
            if await btn.is_visible():
                await btn.scroll_into_view_if_needed(timeout=3000)
                await page.wait_for_timeout(200)
                await btn.click(timeout=4000)
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
                    return t.includes('Вибрати') && r.width > 0 && r.height > 0;
                });
                if (!target) return false;
                target.scrollIntoView({block: 'center'});
                target.click();
                return true;
            }"""
        )
        if clicked_js:
            logging.info("Натиснуто кнопку 'Вибрати' через JS.")
            return True
    except Exception:
        pass
    return False


async def _wait_for_filter_result(page, timeout_ms=10000):
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000.0)
    while asyncio.get_running_loop().time() < deadline:
        for frame in page.frames:
            try:
                txt = (await frame.evaluate("() => document.body ? document.body.innerText : ''")) or ""
                low = txt.lower()
                if "фільтр:" in low or ("молодіжна" in low and ("4.1" in low or bool(TIME_RE.search(txt)))):
                    return True
            except Exception:
                continue
        await page.wait_for_timeout(350)
    return False


async def fill_address_in_frame(frame, page, target_date):
    await select_today_date(frame, page, target_date)

    town_candidates = frame.locator(
        "input[placeholder*='населен' i]:visible, "
        "input[aria-label*='населен' i]:visible, "
        "input[name*='town' i]:visible, input[name*='city' i]:visible"
    )
    street_candidates = frame.locator(
        "input[placeholder*='вулиц' i]:visible, "
        "input[aria-label*='вулиц' i]:visible, "
        "input[name*='street' i]:visible"
    )
    all_inputs = frame.locator("input[type='text']:visible, input:not([type]):visible")

    town_inp = (
        town_candidates.first
        if await town_candidates.count() > 0
        else (all_inputs.nth(0) if await all_inputs.count() > 0 else None)
    )
    street_inp = (
        street_candidates.first
        if await street_candidates.count() > 0
        else (all_inputs.nth(1) if await all_inputs.count() > 1 else None)
    )

    if town_inp is None and street_inp is None:
        return False

    try:
        if town_inp is not None:
            await _fill_single_field(town_inp, page, TOWN_NAME)
            await page.wait_for_timeout(250)

        if street_inp is not None:
            await _fill_single_field(street_inp, page, STREET_NAME)
            await page.wait_for_timeout(250)

        clicked = await click_select_button(frame, page)
        if clicked:
            await _wait_for_filter_result(page, timeout_ms=10000)
        return clicked
    except Exception as e:
        logging.warning("Помилка заповнення форми: %s", e)
    return False


async def fetch_site_data():
    texts = []
    today = datetime.now(TZ).date()
    kwargs = {
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
        kwargs["proxy"] = proxy_cfg

    browser = AsyncCamoufox(**kwargs)
    ctx = await browser.__aenter__()
    try:
        page = await ctx.new_page(viewport={"width": 1366, "height": 1400})
        try:
            await page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            logging.warning("Помилка завантаження сторінки: %s", e)

        solved = False
        for attempt in range(1, 5):
            await page.wait_for_timeout(3500 if attempt == 1 else 6000)
            logging.info("Спроба %s/4 вирішення Cloudflare...", attempt)
            try:
                solved = await solve_captcha(page, captcha_type="cloudflare", challenge_type="interstitial")
            except Exception as e:
                logging.warning("Помилка solve_captcha (%s/4): %s", attempt, e)
                solved = False
            if solved:
                break
            if attempt < 4:
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    await page.wait_for_timeout(4000)

        await page.wait_for_timeout(2000)
        await close_accessibility_modal(page)
        for frame in page.frames:
            try:
                if await fill_address_in_frame(frame, page, today):
                    break
            except Exception:
                continue
        await close_accessibility_modal(page)

        for frame in page.frames:
            try:
                rows_text = await frame.evaluate(
                    """() => {
                        const rows = Array.from(document.querySelectorAll('tr'))
                            .map(r => Array.from(r.querySelectorAll('th, td')).map(c => (c.innerText || '').replace(/\\s+/g, ' ').trim()).filter(Boolean).join(' | '))
                            .filter(Boolean).join('\\n');
                        return (rows + '\\n' + (document.body ? document.body.innerText : '')).trim();
                    }"""
                )
                if rows_text:
                    texts.append(rows_text)
            except Exception:
                continue

        full_text = "\n---\n".join(texts)
        schedule_date = extract_schedule_date(full_text, today)
    finally:
        try:
            await browser.__aexit__(None, None, None)
        except Exception:
            pass

    return full_text, schedule_date


def format_clean_message(schedule_date, today, events, houses):
    when_word = day_label(schedule_date, today)
    intervals_lines = "\n".join(f"• <b>{s:%H:%M} – {e:%H:%M}</b>" for s, e in events)
    houses_line = f"\n🏠 <b>Будинки:</b> {', '.join(houses[:4])}" if houses else ""
    return (
        f"⚡ <b>Графік відключень світла</b>\n"
        f"📅 <b>Коли:</b> {when_word} ({schedule_date:%d.%m.%Y})\n"
        f"📍 <b>Адреса:</b> {TOWN_NAME}, вул. {STREET_NAME}\n"
        f"🔢 <b>Черга:</b> {QUEUE}{houses_line}\n\n"
        f"🕒 <b>Час відключень:</b>\n{intervals_lines}"
    )


async def check_once():
    logging.info("Перевіряю графік для %s, %s (черга %s)", TOWN_NAME, STREET_NAME, QUEUE)
    text, schedule_date = await fetch_site_data()
    now = datetime.now(TZ)
    today = now.date()

    if not text.strip() or any(
        k in text for k in ["Триває перевірка безпеки", "Трохи зачекайте", "Cloudflare", "Ray ID:", "Verify you are human"]
    ):
        logging.warning("Cloudflare Challenge — пропускаю без відправки в Telegram.")
        return

    events = extract_events(text, schedule_date)
    if not events:
        logging.info("Графіка/інтервалів відключення для адреси/черги не знайдено — в Telegram нічого не пишу.")
        return

    houses = extract_houses(text)
    signature = f"{schedule_date.isoformat()}|{QUEUE}|{','.join(f'{s:%H:%M}-{e:%H:%M}' for s, e in events)}"

    state = load_state()
    if state.get("last_signature") == signature:
        logging.info("Цей графік уже надісланий — повторно не пишу.")
        return

    msg = format_clean_message(schedule_date, today, events, houses)
    telegram_send(msg)
    state["last_signature"] = signature
    save_state(state)


async def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Заповни TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID.")
    if RUN_ONCE:
        await check_once()
        return
    while True:
        try:
            await check_once()
        except Exception:
            logging.exception("Помилка перевірки")
        await asyncio.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
