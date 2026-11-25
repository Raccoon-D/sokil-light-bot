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
HOUSE_FILTER = "8"
TZ = ZoneInfo("Europe/Kyiv")

STATE_FILE = Path("state.json")
SHOT_GRIDS_FILE = Path("schedule_grids.png")
SHOT_SEARCH_FILE = Path("schedule_search.png")

PROXY_URL = os.getenv("PROXY_URL", "").strip() or None
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
RUN_ONCE = os.getenv("RUN_ONCE", "0").strip().lower() in {"1", "true", "yes"}
CHECK_EVERY_SECONDS = int(os.getenv("CHECK_EVERY_SECONDS", "300"))
SEND_TEXT_SUMMARY = os.getenv("SEND_TEXT_SUMMARY", "1").strip().lower() in {"1", "true", "yes"}

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


def extract_address_rows(page_text):
    rows = []
    seen = set()
    for raw_line in page_text.splitlines():
        line = re.sub(r"\s*\|\s*", " | ", re.sub(r"[ \t]+", " ", raw_line)).strip()
        if not line:
            continue
        low = line.lower()
        if ("сокіл" in low or "молодіжна" in low) and ("4.1" in line or TIME_RE.search(line)):
            if line not in seen:
                seen.add(line)
                rows.append(line)
    return rows


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


async def close_accessibility_modal(page):
    for frame in page.frames:
        try:
            btn = frame.locator(
                "button:has-text('Закрити'), button[aria-label*='Закрити' i], button[aria-label*='Close' i]"
            )
            if await btn.count() > 0 and await btn.first.is_visible():
                await btn.first.click(timeout=3000)
                await page.wait_for_timeout(800)
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
                    await page.wait_for_timeout(800)
                    logging.info("Обрано дату ГПВ у списку: %s", txt or val)
                    return True
        except Exception:
            continue
    return False


async def _safe_fill_input(inp, page, value):
    try:
        await inp.scroll_into_view_if_needed(timeout=4000)
    except Exception:
        pass
    await page.wait_for_timeout(200)
    try:
        await inp.fill("", timeout=2500)
        await inp.type(value, delay=50, timeout=4000)
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
        await page.wait_for_timeout(400)
        return True
    return False


async def _pick_autocomplete_option(frame, page, text_needle):
    opts = frame.locator("[role='option'], .autocomplete-item, .dropdown-item, li").get_by_text(
        text_needle, exact=False
    )
    cnt = await opts.count()
    for i in range(min(cnt, 5)):
        opt = opts.nth(i)
        try:
            if await opt.is_visible():
                await opt.click(timeout=2000)
                await page.wait_for_timeout(800)
                return True
        except Exception:
            continue
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
                await page.wait_for_timeout(300)
                await btn.click(timeout=4000)
                await page.wait_for_timeout(5000)
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
            await page.wait_for_timeout(5000)
            logging.info("Натиснуто кнопку 'Вибрати' через JS.")
            return True
    except Exception:
        pass
    return False


async def _find_street_input(frame, town_inp):
    inputs = frame.locator("input[type='text']:visible, input:not([type]):visible")
    cnt = await inputs.count()
    for idx in range(cnt):
        cand = inputs.nth(idx)
        try:
            ph = ((await cand.get_attribute("placeholder")) or "").lower()
            nm = ((await cand.get_attribute("name")) or "").lower()
            val = ((await cand.input_value()) or "").strip().lower()
            if "вулиц" in ph or "street" in ph or "вулиц" in nm or "street" in nm:
                return cand
            if idx > 0 and val != TOWN_NAME.lower():
                return cand
        except Exception:
            continue
    if cnt >= 2:
        last_inp = inputs.nth(cnt - 1)
        try:
            if ((await last_inp.input_value()) or "").strip().lower() != TOWN_NAME.lower():
                return last_inp
        except Exception:
            return last_inp
    return None


async def fill_address_in_frame(frame, page, target_date):
    await select_today_date(frame, page, target_date)

    inputs = frame.locator("input[type='text']:visible, input:not([type]):visible")
    if await inputs.count() == 0:
        return False

    try:
        town_inp = inputs.nth(0)
        await _safe_fill_input(town_inp, page, TOWN_NAME)
        await page.wait_for_timeout(1500)
        await _pick_autocomplete_option(frame, page, TOWN_NAME)
        await page.wait_for_timeout(1000)

        street_inp = await _find_street_input(frame, town_inp)
        if street_inp is not None:
            await _safe_fill_input(street_inp, page, STREET_NAME)
            await page.wait_for_timeout(1500)
            await _pick_autocomplete_option(frame, page, STREET_NAME)
            await page.wait_for_timeout(800)

        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
        except Exception:
            pass

        return await click_select_button(frame, page)
    except Exception as e:
        logging.warning("Помилка заповнення форми: %s", e)
    return False


async def _safe_clipped_shot(page, out_path, box):
    if (
        isinstance(box, dict)
        and all(k in box for k in ("x", "y", "width", "height"))
        and all(
            isinstance(box[k], (int, float))
            and box[k] == box[k]
            and abs(box[k]) != float("inf")
            for k in ("x", "y", "width", "height")
        )
        and box["width"] >= 50
        and box["height"] >= 50
    ):
        x = max(0.0, float(box["x"]))
        y = max(0.0, float(box["y"]))
        w = max(50.0, min(1366.0 - x, float(box["width"])))
        h = max(50.0, min(2200.0, float(box["height"])))
        await page.evaluate("(posY) => window.scrollTo(0, posY)", max(0.0, y - 30.0))
        await page.wait_for_timeout(300)
        await page.screenshot(path=str(out_path), clip={"x": x, "y": y, "width": w, "height": h})
        return
    await page.screenshot(path=str(out_path), full_page=False)


async def capture_two_screenshots(page):
    await page.evaluate(
        """() => {
            ['header', 'nav', '.navbar', '.header', 'footer', '.footer', '.modal', '.accessibility-modal', '[class*="cookie" i]', '[id*="cookie" i]']
                .forEach(sel => document.querySelectorAll(sel).forEach(el => { el.style.display = 'none'; }));
        }"""
    )
    await page.set_viewport_size({"width": 1366, "height": 2200})

    grid_box = await page.evaluate(
        """() => {
            let minY = Infinity, maxBottom = -Infinity, minX = Infinity, maxRight = -Infinity;
            for (const el of document.querySelectorAll('h1, h2, h3, h4, div, p, section, table, img, canvas')) {
                const txt = el.innerText || '';
                const r = el.getBoundingClientRect();
                if (!Number.isFinite(r.x) || !Number.isFinite(r.y) || r.width < 100 || r.height < 20) continue;
                const isGrid = txt.includes('ГПВ на') || (r.width >= 450 && r.height >= 200);
                if (!isGrid) continue;
                minY = Math.min(minY, r.top + window.scrollY);
                maxBottom = Math.max(maxBottom, r.bottom + window.scrollY);
                minX = Math.min(minX, r.left + window.scrollX);
                maxRight = Math.max(maxRight, r.right + window.scrollX);
            }
            if (!Number.isFinite(minX) || !Number.isFinite(minY) || !Number.isFinite(maxRight) || !Number.isFinite(maxBottom) || maxBottom <= minY) {
                return null;
            }
            return {
                x: Math.max(0, minX - 15),
                y: Math.max(0, minY - 25),
                width: Math.max(600, (maxRight - minX) + 30),
                height: Math.max(450, (maxBottom - minY) + 40),
            };
        }"""
    )
    await _safe_clipped_shot(page, SHOT_GRIDS_FILE, grid_box)

    search_box = await page.evaluate(
        """() => {
            const matched = Array.from(document.querySelectorAll('tr, .alert, .badge, div'))
                .filter(el => {
                    const r = el.getBoundingClientRect();
                    if (!Number.isFinite(r.x) || !Number.isFinite(r.y) || r.width < 120 || r.height < 18 || r.height > 1400) return false;
                    const t = (el.innerText || '').toLowerCase();
                    return (t.includes('сокіл') || t.includes('молодіжна')) && (t.includes('4.1') || t.includes('фільтр:') || el.tagName === 'TR');
                });
            let minY = Infinity, maxBottom = -Infinity, minX = Infinity, maxRight = -Infinity;
            for (const el of matched) {
                const r = el.getBoundingClientRect();
                minY = Math.min(minY, r.top + window.scrollY);
                maxBottom = Math.max(maxBottom, r.bottom + window.scrollY);
                minX = Math.min(minX, r.left + window.scrollX);
                maxRight = Math.max(maxRight, r.right + window.scrollX);
            }
            if (Number.isFinite(minX) && Number.isFinite(minY) && Number.isFinite(maxRight) && Number.isFinite(maxBottom) && maxBottom > minY) {
                return {
                    x: Math.max(0, minX - 15),
                    y: Math.max(0, minY - 90),
                    width: Math.max(680, (maxRight - minX) + 30),
                    height: Math.min(1500, Math.max(450, (maxBottom - minY) + 140)),
                };
            }
            for (const el of document.querySelectorAll('section, article, main, div, form')) {
                const txt = el.innerText || '';
                const r = el.getBoundingClientRect();
                if (!Number.isFinite(r.x) || !Number.isFinite(r.y) || r.width < 400 || r.height < 160 || r.height > 900) continue;
                if (txt.includes('Пошук вашої адреси') && txt.includes('Вибрати')) {
                    return {
                        x: Math.max(0, r.x + window.scrollX - 10),
                        y: Math.max(0, r.y + window.scrollY - 20),
                        width: r.width + 20,
                        height: r.height + 240,
                    };
                }
            }
            return null;
        }"""
    )
    await _safe_clipped_shot(page, SHOT_SEARCH_FILE, search_box)

    return SHOT_GRIDS_FILE, SHOT_SEARCH_FILE


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

        await page.wait_for_timeout(3000)
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
        try:
            grids_shot, search_shot = await capture_two_screenshots(page)
        except Exception as e:
            logging.error("Не вдалося зробити скріншоти: %s", e)
            grids_shot, search_shot = SHOT_GRIDS_FILE, SHOT_SEARCH_FILE
    finally:
        try:
            await browser.__aexit__(None, None, None)
        except Exception:
            pass

    return full_text, grids_shot, search_shot, schedule_date


async def check_once():
    logging.info("Перевіряю офіційний графік для черги %s (chat_id=%s)", QUEUE, CHAT_ID or "EMPTY")
    text, grids_shot, search_shot, schedule_date = await fetch_site_data()
    now = datetime.now(TZ)
    today = now.date()
    state = load_state()
    changed = False

    logging.info(
        "text_len=%s | schedule_date=%s | grids_exists=%s | search_exists=%s",
        len(text),
        schedule_date,
        grids_shot.exists(),
        search_shot.exists(),
    )

    if not text.strip() or any(
        k in text for k in ["Триває перевірка безпеки", "Трохи зачекайте", "Cloudflare", "Ray ID:", "Verify you are human"]
    ):
        raise RuntimeError("Cloudflare Challenge: графік недоступний для бота.")

    events = extract_events(text, schedule_date)
    addr_rows = extract_address_rows(text)
    when_word = day_label(schedule_date, today)
    update_stamp = extract_update_stamp(text)
    logging.info("events_count=%s | addr_rows=%s | update_stamp=%s", len(events), len(addr_rows), update_stamp)

    if grids_shot.exists():
        try:
            telegram_send_photo(
                grids_shot,
                f"Графіки ГПВ | {when_word} ({schedule_date:%d.%m.%Y})\nОновлено: {update_stamp}",
            )
        except Exception as e:
            logging.error("Помилка фото графіків: %s", e)

    if search_shot.exists():
        try:
            telegram_send_photo(
                search_shot,
                f"Пошук ({TOWN_NAME}, {STREET_NAME}, черга {QUEUE}) | {when_word} ({schedule_date:%d.%m.%Y})",
            )
        except Exception as e:
            logging.error("Помилка фото пошуку: %s", e)

    if SEND_TEXT_SUMMARY:
        rows_block = "\n".join(addr_rows[:12]) if addr_rows else "Рядків за адресою у тексті не знайдено (дивись 2-й скріншот)."
        events_block = (
            "\n".join(f"• {s:%H:%M}–{e:%H:%M}" for s, e in events)
            if events
            else "Інтервали у тексті не знайдено."
        )
        msg = (
            f"⚡️ Черга {QUEUE} ({TOWN_NAME}, {STREET_NAME}, буд. {HOUSE_FILTER}) — {when_word} ({schedule_date:%d.%m.%Y})\n"
            f"Оновлено: {update_stamp}\n\n"
            f"Рядки з пошуку:\n{rows_block}\n\n"
            f"Інтервали:\n{events_block}"
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
