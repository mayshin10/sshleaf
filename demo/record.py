"""Record the README demo with headless Chrome. Usage: record.py <ssh-host> <video-out-dir> [port]"""

import sys
import time

from playwright.sync_api import sync_playwright

URL = f"http://127.0.0.1:{sys.argv[3] if len(sys.argv) > 3 else 8766}/"
HOST, OUT = sys.argv[1], sys.argv[2]
WIDTH, HEIGHT = 1280, 760

# Headless recordings have no pointer; draw one that follows mouse events.
CURSOR_JS = """
window.addEventListener('DOMContentLoaded', () => {
  const dot = document.createElement('div');
  dot.style.cssText = 'position:fixed;left:0;top:0;width:18px;height:18px;margin:-3px 0 0 -3px;z-index:100000;pointer-events:none;'
    + 'background:no-repeat url("data:image/svg+xml;utf8,<svg xmlns=%22http://www.w3.org/2000/svg%22 width=%2218%22 height=%2218%22><path d=%22M3 2 L3 15 L6.5 11.5 L9 17 L11 16 L8.6 10.6 L13.5 10.6 Z%22 fill=%22black%22 stroke=%22white%22 stroke-width=%221.3%22/></svg>")';
  document.body.appendChild(dot);
  const ring = document.createElement('div');
  ring.style.cssText = 'position:fixed;width:26px;height:26px;margin:-10px 0 0 -10px;border-radius:50%;border:2px solid rgba(47,125,79,.9);z-index:99999;pointer-events:none;opacity:0;transition:opacity .35s, transform .35s;';
  document.body.appendChild(ring);
  addEventListener('mousemove', e => { dot.style.left = e.clientX + 'px'; dot.style.top = e.clientY + 'px'; }, true);
  addEventListener('mousedown', e => {
    ring.style.left = e.clientX + 'px'; ring.style.top = e.clientY + 'px';
    ring.style.transition = 'none'; ring.style.opacity = 1; ring.style.transform = 'scale(.6)';
    requestAnimationFrame(() => { ring.style.transition = 'opacity .45s, transform .45s'; ring.style.opacity = 0; ring.style.transform = 'scale(1.5)'; });
  }, true);
});
"""


def pause(seconds):
    time.sleep(seconds)


def center(page, selector):
    box = page.locator(selector).first.bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def move_click(page, x, y, steps=22):
    page.mouse.move(x, y, steps=steps)
    pause(0.15)
    page.mouse.click(x, y)


def click(page, selector):
    move_click(page, *center(page, selector))


def char_xy(page, line, ch):
    return page.evaluate(f"(() => {{ const c = cm.charCoords({{line: {line}, ch: {ch}}}, 'window'); return [c.left, (c.top + c.bottom) / 2]; }})()")


def find_line(page, needle):
    return page.evaluate(f"(() => {{ const lines = cm.getValue().split('\\n'); return lines.findIndex(l => l.includes({needle!r})); }})()")


def wait_compiled(page, mark):
    page.wait_for_function("() => typeof compiling !== 'undefined' && compiling", timeout=10000)
    page.wait_for_function(f"() => !compiling && document.getElementById('status').textContent.includes('{mark}')", timeout=60000)


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="chrome", headless=True)
    context = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT}, color_scheme="light",
                                  record_video_dir=OUT, record_video_size={"width": WIDTH, "height": HEIGHT})
    page = context.new_page()
    page.add_init_script(CURSOR_JS)
    page.goto(URL)
    page.wait_for_selector("#open-modal.show")
    page.wait_for_selector("#listing .item")
    page.mouse.move(640, 300)
    pause(1.2)

    # 1. pick a project folder (local or any ~/.ssh/config host)
    click(page, "#host-btn")
    page.wait_for_selector("#host-menu.show")
    pause(0.8)
    for item in page.locator("#host-menu .dd-item").all():  # glide over the host list
        box = item.bounding_box()
        page.mouse.move(box["x"] + 60, box["y"] + box["height"] / 2, steps=8)
        pause(0.18)
        if item.get_attribute("data-host") == HOST:
            break
    pause(0.5)
    click(page, f'#host-menu .dd-item[data-host="{HOST}"]')
    page.wait_for_function(f"() => document.getElementById('host-label').textContent === {HOST!r}")
    page.wait_for_selector('#listing .item.dir[data-name="papers"]')
    pause(1.2)
    click(page, '#listing .item.dir[data-name="papers"]')
    page.wait_for_selector('#listing .item.dir[data-name="demo-paper"]')
    pause(1.0)
    click(page, '#listing .item.dir[data-name="demo-paper"]')
    page.wait_for_selector("#listing .item.file")
    pause(1.2)
    click(page, "#btn-open-here")
    wait_compiled(page, "✓")
    pause(2.0)

    # 2. edit the title, save -> recompiles
    line = find_line(page, "\\title{")
    x, y = char_xy(page, line, len("\\title{A "))
    move_click(page, x, y)
    page.evaluate(f"cm.setSelection({{line: {line}, ch: 9}}, {{line: {line}, ch: 13}})")
    pause(0.5)
    page.keyboard.type("Live", delay=90)
    pause(0.5)
    page.keyboard.press("Control+S")
    wait_compiled(page, "✓")
    pause(2.0)

    # 3. introduce an error -> logs -> click to jump -> fix
    line = find_line(page, "and compiles them locally")
    text_len = page.evaluate(f"cm.getLine({line}).length")
    x, y = char_xy(page, line, text_len)
    move_click(page, x + 2, y)
    page.evaluate(f"cm.setCursor({{line: {line}, ch: {text_len}}})")
    page.keyboard.type(" \\oops", delay=90)
    page.keyboard.press("Control+S")
    wait_compiled(page, "✗")
    pause(1.8)
    click(page, "#logs .diag.error")
    pause(1.0)
    page.keyboard.press("End")
    pause(0.3)
    for _ in range(6):
        page.keyboard.press("Backspace")
        pause(0.08)
    page.keyboard.press("Control+S")
    wait_compiled(page, "✓")
    pause(1.5)

    # 4. SyncTeX: double-click in the PDF jumps to the source
    box = page.locator('#pdf .page[data-page="1"]').bounding_box()
    page.mouse.move(box["x"] + box["width"] * 0.42, box["y"] + box["height"] * 0.60, steps=25)
    pause(0.4)
    page.mouse.dblclick(box["x"] + box["width"] * 0.42, box["y"] + box["height"] * 0.60)
    pause(2.0)

    # 5. ...and → PDF shows where the cursor's line is
    line = find_line(page, "\\mathcal{L}")
    x, y = char_xy(page, line, 4)
    move_click(page, x, y)
    pause(0.4)
    click(page, "#btn-forward")
    pause(2.5)

    context.close()
    browser.close()
