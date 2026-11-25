import logging
import os
import re
from datetime import datetime, date, time, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from camoufox import AsyncCamoufox
from camoufox_captcha import solve_captcha

SOURCE_URL = "https://energy.volyn.ua/spozhyvacham/perervy-u-elektropostachanni/hrafik-vidkliuchen/#gsc.tab=0"
QUEUE = "4.1"
TOWN_NAME = "Сокіл"
STREET_NAME = "Молодіжна"
TZ = ZoneInfo("Europe/Kyiv")
SHOT_SEARCH_FILE = Path("schedule_search.png")
SHOT_GRIDS_FILE = Path("schedule_grids.png")
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None

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
        if ("4.1" not in parts) and not QUEUE_RE.search(line):
            continue
        neighbor = " | ".join(lines[max(0, idx - 1) : min(len(lines), idx + 3)])
        row_date = parse_date(line) or parse_date(neighbor) or current_date
        ranges = list(TIME_RE.finditer(line)) or list(TIME_RE.finditer(neighbor))
        for m in ranges:
            sh, sm, eh, em = map(int, m.groups())
            if (sh, sm, eh, em) in {(0, 0, 12, 0), (12, 0, 24, 0)}:
                continue
            start = datetime.combine(row_date, time(sh, sm), TZ)
            end = datetime.combine(row_date, time(eh, em), TZ)
            if end <= start:
                end += timedelta(days=1)
            events.append((start, end))

    unique = {(s.isoformat(), e.isoformat()): (s, e) for s, e in events}
    return sorted(unique.values(), key=lambda x: x[0])


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


async def click_select_button(frame, page):
    candidates = frame.locator(
        "button:has-text('Вибрати'), input[type='submit'][value*='Вибрати'], "
        "input[type='button'][value*='Вибрати'], a:has-text('Вибрати'), "
        "button:has-text('Знайти'), input[type='submit'][value*='Знайти']"
    )
    for i in range(await candidates.count()):
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
    inputs = frame.locator("input[type='text']:visible, input:not([type]):visible")
    cnt = await inputs.count()
    if cnt == 0:
        return False

    try:
        town_inp = inputs.nth(0)
        await town_inp.scroll_into_view_if_needed(timeout=3000)
        await town_inp.click(timeout=3000)
        await town_inp.fill("")
        await town_inp.type(TOWN_NAME, delay=60)
        await page.wait_for_timeout(1200)

        town_opt = frame.locator("[role='option'], .autocomplete-item, li").get_by_text(TOWN_NAME, exact=False)
        if await town_opt.count() > 0:
            try:
                if await town_opt.first.is_visible():
                    await town_opt.first.click(timeout=2000)
                    await page.wait_for_timeout(700)
            except Exception:
                pass

        if cnt >= 2:
            street_inp = inputs.nth(1)
            await street_inp.scroll_into_view_if_needed(timeout=3000)
            await street_inp.click(timeout=3000)
            await street_inp.fill("")
            await street_inp.type(STREET_NAME, delay=60)
            await page.wait_for_timeout(1200)

            street_opt = frame.locator("[role='option'], .autocomplete-item, li").get_by_text(STREET_NAME, exact=False)
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
            ['header', 'nav', '.navbar', '.header', '.modal', '.accessibility-modal', '[class*="cookie" i]', '[id*="cookie" i]']
                .forEach(sel => document.querySelectorAll(sel).forEach(el => { el.style.display = 'none'; }));
        }"""
    )
    await page.set_viewport_size({"width": 1366, "height": 1600})

    search_box = await page.evaluate(
        """() => {
            for (const el of document.querySelectorAll('section, article, main, div, form')) {
                const txt = el.innerText || '';
                if (txt.includes('Пошук вашої адреси') || (txt.includes('Вибрати') && txt.includes('вулиц'))) {
                    const r = el.getBoundingClientRect();
                    if (r.width >= 400 && r.height >= 160 && r.height <= 1400) {
                        return {x: r.x + window.scrollX, y: r.y + window.scrollY, width: r.width, height: r.height};
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
        await page.screenshot(path=str(SHOT_SEARCH_FILE), clip={"x": sx, "y": sy, "width": sw, "height": sh})
    else:
        await page.screenshot(path=str(SHOT_SEARCH_FILE), full_page=False)

    grid_box = await page.evaluate(
        """() => {
            let best = null, bestArea = Infinity;
            for (const el of document.querySelectorAll('section, article, main, div, img, table')) {
                const txt = el.innerText || '';
                const r = el.getBoundingClientRect();
                if (!(txt.includes('00:00') || txt.includes('12:00') || txt.includes('ГПВ')) && el.tagName !== 'IMG') continue;
                if (r.width < 450 || r.height < 260 || r.height > 2400) continue;
                const area = r.width * r.height;
                if (area < bestArea) {
                    bestArea = area;
                    best = {x: r.x + window.scrollX, y: r.y + window.scrollY, width: r.width, height: r.height};
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
        await page.screenshot(path=str(SHOT_GRIDS_FILE), clip={"x": gx, "y": gy, "width": gw, "height": gh})
    else:
        await page.screenshot(path=str(SHOT_GRIDS_FILE), full_page=False)

    return SHOT_SEARCH_FILE, SHOT_GRIDS_FILE


async def fetch_site_data():
    texts = []
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

    async with AsyncCamoufox(**kwargs) as browser:
        page = await browser.new_page(viewport={"width": 1366, "height": 1200})
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
                if await fill_address_in_frame(frame, page):
                    break
            except Exception:
                continue
        await close_accessibility_modal(page)

        for frame in page.frames:
            try:
                rows_text = await frame.evaluate(
                    """() => {
                        const rows = Array.from(document.querySelectorAll('tr'))
                            .map(r => Array.from(r.querySelectorAll('th, td')).map(c => c.innerText.replace(/\\s+/g, ' ').trim()).filter(Boolean).join(' | '))
                            .filter(Boolean).join('\\n');
                        return (rows + '\\n' + (document.body ? document.body.innerText : '')).trim();
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
    
