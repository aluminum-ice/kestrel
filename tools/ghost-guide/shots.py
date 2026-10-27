"""Screenshots of the demo device, framed the way the guide shows them.

Phone shots are an iPhone 14 Pro Max in dark mode: Playwright's WebKit (the
Safari engine) with that device's user agent, touch and mobile layout, a
430 x 932 pt screen at 2x. The page area is 430 x 896 pt under a 36 pt status
strip, inside a bezel with the Dynamic Island: 904 x 1908 px, the geometry of
the page's original screenshots. The e-ink shot is the plugin's own 264 x 176
frame, scaled 3x with a 16 px margin (824 x 560 px).
"""

import os

from PIL import Image, ImageDraw

# iPhone 14 Pro Max: 430 x 932 pt screen, rendered at 2x.
SCREEN_W, SCREEN_H = 430, 932
STATUS_H = 36                      # status strip above the page, pt
SCALE = 2
BEZEL = 22                         # px
OUTER_RADIUS = 150                 # px
SCREEN_RADIUS = 120                # px
ISLAND = (337, 27, 567, 89)        # px, in frame coordinates
BEZEL_RGB = (12, 13, 15)
EDGE_RGB = (72, 75, 82)            # thin rim so the phone reads on dark themes
ISLAND_RGB = (8, 9, 11)
FRAME_W = SCREEN_W * SCALE + 2 * BEZEL
FRAME_H = SCREEN_H * SCALE + 2 * BEZEL

EINK_SCALE = 3
EINK_MARGIN = 16
EINK_BG = (225, 227, 230)

_SS = 4                            # supersampling for smooth corners


def _rounded_mask(size, box, radius):
    w, h = size
    big = Image.new("L", (w * _SS, h * _SS), 0)
    x0, y0, x1, y1 = box
    ImageDraw.Draw(big).rounded_rectangle(
        (x0 * _SS, y0 * _SS, (x1 + 1) * _SS - 1, (y1 + 1) * _SS - 1),
        radius=radius * _SS, fill=255)
    return big.resize(size, Image.LANCZOS)


def frame_phone(page_png):
    """Put a 860 x 1792 px page screenshot into the phone frame."""
    page = Image.open(page_png).convert("RGB")
    want = (SCREEN_W * SCALE, (SCREEN_H - STATUS_H) * SCALE)
    if page.size != want:
        raise ValueError("screenshot is %dx%d, expected %dx%d" % (page.size + want))
    # The status strip takes the page's own background colour, as Safari does.
    screen = Image.new("RGB", (SCREEN_W * SCALE, SCREEN_H * SCALE),
                       page.getpixel((4, 4)))
    screen.paste(page, (0, STATUS_H * SCALE))

    size = (FRAME_W, FRAME_H)
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    full = (0, 0, FRAME_W - 1, FRAME_H - 1)
    out.paste(Image.new("RGBA", size, EDGE_RGB + (255,)), (0, 0),
              _rounded_mask(size, full, OUTER_RADIUS))
    inset = (2, 2, FRAME_W - 3, FRAME_H - 3)
    out.paste(Image.new("RGBA", size, BEZEL_RGB + (255,)), (0, 0),
              _rounded_mask(size, inset, OUTER_RADIUS - 2))
    holder = Image.new("RGBA", size, (0, 0, 0, 0))
    holder.paste(screen.convert("RGBA"), (BEZEL, BEZEL))
    screen_box = (BEZEL, BEZEL, FRAME_W - BEZEL - 1, FRAME_H - BEZEL - 1)
    out.paste(holder, (0, 0), _rounded_mask(size, screen_box, SCREEN_RADIUS))
    island_h = ISLAND[3] - ISLAND[1] + 1
    out.paste(Image.new("RGBA", size, ISLAND_RGB + (255,)), (0, 0),
              _rounded_mask(size, ISLAND, island_h // 2))
    return out


def frame_eink(panel):
    """Scale the 1-bit panel frame up crisply on a light margin."""
    panel = panel.convert("RGB")
    big = panel.resize((panel.width * EINK_SCALE, panel.height * EINK_SCALE),
                       Image.NEAREST)
    out = Image.new("RGB", (big.width + 2 * EINK_MARGIN, big.height + 2 * EINK_MARGIN),
                    EINK_BG)
    out.paste(big, (EINK_MARGIN, EINK_MARGIN))
    return out


def _scroll_to_text(page, text):
    """Scroll so the first element containing ``text`` sits near the top."""
    target = page.get_by_text(text, exact=False).first
    target.wait_for(timeout=10000)
    box = target.bounding_box()
    page.evaluate("y => window.scrollTo(0, Math.max(0, y))",
                  box["y"] - 24 + page.evaluate("window.scrollY"))


def capture(demo, base_url, figures, out_dir, browser="webkit"):
    """Write ``<key>.png`` for each figure into ``out_dir``. Returns
    ``{key: (path, width, height)}``."""
    from playwright.sync_api import sync_playwright

    os.makedirs(out_dir, exist_ok=True)
    results = {}
    for fig in figures:
        if fig.get("source") == "eink":
            img = frame_eink(demo.eink_image())
            path = os.path.join(out_dir, fig["key"] + ".png")
            img.save(path, optimize=True)
            results[fig["key"]] = (path, img.width, img.height)

    phone = [f for f in figures if f.get("source") != "eink"]
    if not phone:
        return results
    with sync_playwright() as pw:
        device = pw.devices["iPhone 14 Pro Max"]
        engine = getattr(pw, browser)
        instance = engine.launch()
        try:
            context = instance.new_context(
                user_agent=device["user_agent"],
                viewport={"width": SCREEN_W, "height": SCREEN_H - STATUS_H},
                screen={"width": SCREEN_W, "height": SCREEN_H},
                device_scale_factor=SCALE,
                is_mobile=(browser != "firefox"),
                has_touch=True,
                color_scheme="dark",
                reduced_motion="reduce",
            )
            page = context.new_page()
            for fig in phone:
                demo.set_update_available(fig.get("scenario") == "update-available")
                page.goto(base_url + fig["path"], wait_until="networkidle")
                if fig.get("focus"):
                    _scroll_to_text(page, fig["focus"])
                raw = os.path.join(out_dir, fig["key"] + ".page.png")
                page.screenshot(path=raw)
                img = frame_phone(raw)
                os.remove(raw)
                path = os.path.join(out_dir, fig["key"] + ".png")
                img.save(path, optimize=True)
                results[fig["key"]] = (path, img.width, img.height)
        finally:
            instance.close()
            demo.set_update_available(False)
    return results


def same_pixels(path_a, path_b):
    # Compare the raw pixels: ImageChops.difference(...).getbbox() would look at
    # the alpha channel alone for RGBA, calling any two opaque images equal.
    a = Image.open(path_a).convert("RGBA")
    b = Image.open(path_b).convert("RGBA")
    return a.size == b.size and a.tobytes() == b.tobytes()
