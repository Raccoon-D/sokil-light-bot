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

    now = datetime.now(TZ)
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

            if end > now:
                events.append((start, end))

    unique = {}
    for start, end in events:
        unique[(start.isoformat(), end.isoformat())] = (start, end)
    return sorted(unique.values(), key=lambda x: x[0])


async def extract_grid_events(page, schedule_date):
    """Пробує зчитати активні клітинки для черги 4 / підчерги 1 (4.1) з HTML-таблиць графіка."""
    now = datetime.now(TZ)
    grid_events = []

    for frame in page.frames:
        try:
            raw_slots = await frame.evaluate(
                """() => {
                    const out = [];
                    const tables = Array.from(document.querySelectorAll('table'));
                    for (const tbl of tables) {
                        const headers = Array.from(tbl.querySelectorAll('thead tr th, tr:first-child th, tr:first-child td'))
                            .map(el => el.innerText.replace(/\\s+/g, ' ').trim());
                        const rows = Array.from(tbl.querySelectorAll('tbody tr, tr'));
                        let activeQueue = '';
                        for (const tr of rows) {
                            const cells = Array.from(tr.querySelectorAll('th, td'));
                            if (!cells.length) continue;
                            const firstText = cells[0].innerText.replace(/\\s+/g, ' ').trim();
                            if (/^[1-6]$/.test(firstText)) {
                                activeQueue = firstText;
                            }
                            const secondText = cells.length > 1 ? cells[1].innerText.replace(/\\s+/g, ' ').trim() : '';
                            const rowLabel = `${activeQueue}.${secondText}`;
                            if (activeQueue === '4' && (secondText === '1' || rowLabel.includes('4.1'))) {
                                cells.forEach((cell, idx) => {
                                    const style = window.getComputedStyle(cell);
                                    const cls = (cell.className || '').toString().toLowerCase();
                                    const bg = style.backgroundColor || '';
                                    const dataAttr = JSON.stringify(cell.dataset || {}).toLowerCase();
                                    const isOff = (
                                        cls.includes('off') ||
                                        cls.includes('out') ||
                                        cls.includes('disable') ||
                                        cls.includes('active') ||
                                        cls.includes('dark') ||
                                        dataAttr.includes('off') ||
                                        dataAttr.includes('1') ||
                                        /rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)\\)/.test(bg) && (() => {
                                            const m = bg.match(/rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)\\)/);
                                            const r = Number(m[1]), g = Number(m[2]), b = Number(m[3]);
                                            return (r + g + b) < 420 && Math.max(r, g, b) - Math.min(r, g, b) < 45;
                                        })()
                                    );
                                    if (isOff && idx >= 2) {
                                        out.push({
                                            idx,
                                            header: headers[idx] || '',
                                            text: cell.innerText.trim(),
                                        });
                                    }
                                });
                            }
                        }
                    }
                    return out;
                }"""
            )
            for item in raw_slots or []:
                hdr = item.get("header", "")
                m = TIME_RE.search(hdr)
                if m:
                    sh, sm, eh, em = map(int, m.groups())
                    start = datetime.combine(schedule_date, time(sh, sm), TZ)
                    end = datetime.combine(schedule_date, time(eh, em), TZ)
                    if end <= start:
                        end += timedelta(days=1)
                    if end > now:
                        grid_events.append((start, end))
        except Exception:
            continue

    unique = {}
    for start, end in grid_events:
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
    """Знімає повний блок графіка з датою і обома сітками (00:00-12:00 та 12:00-24:00), як на 5141_2.jpg."""
    await page.evaluate(
        """() => {
            const hideSelectors = [
                'header', 'nav', '.navbar', '.header', '.modal', '.accessibility-modal',
                '[class*="cookie" i]', '[id*="cookie" i]'
            ];
            for (const sel of hideSelectors) {
                document.querySelectorAll(sel).forEach(el => {
                    if (!el.innerText || !el.innerText.includes('00:00')) {
                        el.style.display = 'none';
                    }
                });
            }
        }"""
    )

    # Шукаємо елемент, що містить заголовок ГПВ / дату і обидва інтервали 00:00 та 12:00
    target_box = await page.evaluate(
        """() => {
            const candidates = Array.from(document.querySelectorAll('section, article, div, main'));
            let best = null;
            let bestArea = Infinity;
            for (const el of candidates) {
                const txt = (el.innerText || '');
                if (!txt.includes('00:00') || !txt.includes('12:00')) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 500 || rect.height < 450 || rect.height > 2400) continue;
                const area = rect.width * rect.height;
                if (area < bestArea) {
                    bestArea = area;
                    best = {
                        x: rect.x + window.scrollX,
                        y: rect.y + window.scrollY,
                        width: rect.width,
                        height: rect.height,
                    };
                }
            }
            return best;
        }"""
    )

    if target_box:
        # Підхоплюємо трохи вище від верху сітки, щоб у кадр потрапив заголовок з датою і часом формування
        top_y = max(0.0, float(target_box["y"]) - 95.0)
        full_h = min(2400.0, float(target_box["height"]) + 115.0)
        win_w = max(1200, int(float(target_box["width"]) + 60))
        win_h = min(2600, max(1000, int(full_h + 120)))

        await page.set_viewport_size({"width": win_w, "height": win_h})
        await page.evaluate("(y) => window.scrollTo(0, y)", top_y)
        await page.wait_for_timeout(400)

        clip = {
            "x": max(0.0, float(target_box["x"]) - 15.0),
            "y": top_y,
            "width": min(float(win_w), float(target_box["width"]) + 30.0),
            "height": full_h,
        }
        await page.screenshot(path=str(SCREENSHOT_FILE), clip=clip)
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

        full_text = "\n---\n".join(texts)
        schedule_date = extract_schedule_date(full_text, datetime.now(TZ).date())
        grid_events = await extract_grid_events(page, schedule_date)

        try:
            await capture_schedule_screenshot(page)
        except Exception as e:
            logging.error("Не вдалося зробити скріншот: %s", e)

    return full_text, SCREENSHOT_FILE, schedule_date, grid_events


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
    text, shot_path, schedule_date, grid_events = await fetch_site_data()
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

    text_events = extract_events(text, schedule_date)
    merged = {(s.isoformat(), e.isoformat()): (s, e) for s, e in (grid_events + text_events)}
    events = sorted(merged.values(), key=lambda x: x[0])

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
        if events:
            lines_txt = "\n".join(
                f"• {day_label(s.date(), today)} ({s:%d.%m}): {s:%H:%M}–{e:%H:%M}"
                for s, e in events
            )
            summary_msg = (
                f"⚡️ Черга {QUEUE} ({TOWN_NAME}, {STREET_NAME}) — графік на {when_word} ({schedule_date:%d.%m.%Y})\n"
                f"Відключення заплановані ({when_word}):\n{lines_txt}\n"
                f"Оновлено на сайті: {update_stamp}"
            )
        else:
            summary_msg = (
                f"ℹ️ Черга {QUEUE} ({TOWN_NAME}, {STREET_NAME}) — графік на {when_word} ({schedule_date:%d.%m.%Y})\n"
                f"Майбутніх текстових/сіткових відключень для {QUEUE} на {when_word} не виявлено (перевір сітку на фото).\n"
                f"Оновлено на сайті: {update_stamp}"
            )
        telegram_send(summary_msg)

    notified = set(state.get("notified", []))

    for start, end in events:
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
