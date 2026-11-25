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


async def fill_address_in_frame(frame, page, target_date):
    await select_today_date(frame, page, target_date)

    inputs = frame.locator("input[type='text']:visible, input:not([type]):visible")
    cnt = await inputs.count()
    if cnt == 0:
        return False

    try:
        town_inp = inputs.nth(0)
        await _safe_fill_input(town_inp, page, TOWN_NAME)
        await page.wait_for_timeout(1200)

        town_opt = frame.locator("[role='option'], .autocomplete-item, li").get_by_text("Сокіл", exact=False)
        if await town_opt.count() > 0:
            try:
                if await town_opt.first.is_visible():
                    await town_opt.first.click(timeout=2000)
                    await page.wait_for_timeout(700)
            except Exception:
                pass

        street_inp = inputs.nth(cnt - 1) if cnt >= 2 else town_inp
        if cnt >= 2:
            await _safe_fill_input(street_inp, page, STREET_NAME)
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
            ['header', 'nav', '.navbar', '.header', '.modal', '.accessibility-modal', '[class*="cookie" i]', '[id*="cookie" i]']
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
            const rows = Array.from(document.querySelectorAll('tr'))
                .filter(tr => {
                    const t = (tr.innerText || '').toLowerCase();
                    return t.includes('сокіл') || t.includes('молодіжна');
                });
            let minY = Infinity, maxBottom = -Infinity, minX = Infinity, maxRight = -Infinity;
            for (const rEl of rows) {
                const r = rEl.getBoundingClientRect();
                if (Number.isFinite(r.x) && Number.isFinite(r.y) && r.width > 0 && r.height > 0) {
                    minY = Math.min(minY, r.top + window.scrollY);
                    maxBottom = Math.max(maxBottom, r.bottom + window.scrollY);
                    minX = Math.min(minX, r.left + window.scrollX);
                    maxRight = Math.max(maxRight, r.right + window.scrollX);
                }
            }
            if (Number.isFinite(minX) && Number.isFinite(minY) && Number.isFinite(maxRight) && Number.isFinite(maxBottom) && maxBottom > minY) {
                return {
                    x: Math.max(0, minX - 15),
                    y: Math.max(0, minY - 120),
                    width: Math.max(650, (maxRight - minX) + 30),
                    height: Math.max(500, (maxBottom - minY) + 160),
                };
            }
            for (const el of document.querySelectorAll('section, article, main, div, form')) {
                const txt = el.innerText || '';
                const r = el.getBoundingClientRect();
                if (!Number.isFinite(r.x) || !Number.isFinite(r.y) || r.width < 400 || r.height < 160 || r.height > 1800) continue;
                if (txt.includes('Пошук вашої адреси') || txt.includes('Вибрати')) {
                    return {
                        x: Math.max(0, r.x + window.scrollX - 10),
                        y: Math.max(0, r.y + window.scrollY - 20),
                        width: r.width + 20,
                        height: r.height + 120,
                    };
                }
            }
            return null;
        }"""
    )
    await _safe_clipped_shot(page, SHOT_SEARCH_FILE, search_box)

    return SHOT_GRIDS_FILE, SHOT_SEARCH_FILE
