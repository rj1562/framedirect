#!/usr/bin/env python3
"""Render the documentation screenshots with made-up data ("Company XYZ",
invented readings, a drawn background, logo and QR code) -- nothing from a
real widget or client. Runs anywhere Pillow + numpy are installed (e.g. on
the kiosk itself); never touches the screen or the kiosk's own cache.

    python3 tools/make-demo-images.py [out_dir]     (default: docs/images)
"""
import json
import os
import random
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(ROOT, "docs", "images")
WORK = tempfile.mkdtemp(prefix="kiosk-demo-")
os.makedirs(os.path.join(WORK, "assets"))
os.environ.update(KIOSK_CACHE=os.path.join(WORK, "latest.json"), KIOSK_ASSET_CACHE=os.path.join(WORK, "assets"),
                  KIOSK_API_LOG=os.path.join(WORK, "api.log"), KIOSK_WIDGET_HEALTH_LOG=os.path.join(WORK, "health.log"),
                  KIOSK_CONFIG=os.path.join(WORK, "config.json"))
sys.path.insert(0, os.path.join(ROOT, "renderer"))

from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: E402

W, H = 1920, 1080
FONT = os.path.join(ROOT, "renderer", "assets", "Inter-Bold.ttf")
rng = random.Random(7)


# ---------- invented branding ----------

def demo_background(path):
    """A dusk city skyline -- drawn, not a photo."""
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=(round(40 + 150 * t), round(70 + 80 * t), round(120 + 40 * t)))
    x = 0
    while x < W:
        bw, bh = rng.randint(90, 220), rng.randint(250, 820)
        shade = rng.randint(25, 55)
        d.rectangle([x, H - bh, x + bw, H], fill=(shade, shade + 8, shade + 20))
        for wy in range(H - bh + 20, H - 20, 34):
            for wx in range(x + 12, x + bw - 16, 26):
                if rng.random() < 0.45:
                    d.rectangle([wx, wy, wx + 12, wy + 18], fill=(240, 200, 120))
        x += bw + rng.randint(4, 30)
    img.filter(ImageFilter.GaussianBlur(1.2)).save(path, "JPEG", quality=90)


def demo_logo(path):
    img = Image.new("RGBA", (900, 300), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 20, 260, 280], radius=60, outline=(255, 255, 255, 255), width=18)
    d.text((40, 88), "XYZ", font=ImageFont.truetype(FONT, 84), fill=(255, 255, 255, 255))
    d.text((300, 60), "COMPANY", font=ImageFont.truetype(FONT, 96), fill=(255, 255, 255, 255))
    d.text((300, 165), "XYZ", font=ImageFont.truetype(FONT, 96), fill=(255, 255, 255, 255))
    img.save(path)


def demo_qr(path, modules=29, px=10, border=40):
    """QR-looking pattern (finder squares + random modules). Not scannable."""
    grid = [[rng.random() < 0.5 for _ in range(modules)] for _ in range(modules)]
    for ox, oy in ((0, 0), (modules - 7, 0), (0, modules - 7)):
        for y in range(-1, 8):
            for x in range(-1, 8):
                if 0 <= ox + x < modules and 0 <= oy + y < modules:
                    ring = max(abs(x - 3), abs(y - 3))
                    grid[oy + y][ox + x] = ring in (0, 1, 3) and 0 <= x < 7 and 0 <= y < 7
    size = modules * px + 2 * border
    img = Image.new("L", (size, size), 255)
    d = ImageDraw.Draw(img)
    for y in range(modules):
        for x in range(modules):
            if grid[y][x]:
                d.rectangle([border + x * px, border + y * px, border + (x + 1) * px - 1, border + (y + 1) * px - 1], fill=0)
    img.save(path)


# ---------- invented widget data ----------

def block(v, unit):
    return {"values": [v], "unit": unit}


GRADES = [{"name": "good", "color": "#87AF1B"}, {"name": "fair", "color": "#FFCD41"}, {"name": "poor", "color": "#FD4E3A"}]
THRESHOLDS = [
    {"species": "IAQI", "thresholds": [3, 5], "interval_grades": ["good", "fair", "poor"]},
    {"species": "PM25", "thresholds": [30, 90], "interval_grades": ["good", "fair", "poor"]},
    {"species": "PM10", "thresholds": [50, 150], "interval_grades": ["good", "fair", "poor"]},
    {"species": "CO2", "thresholds": [800, 1200], "interval_grades": ["good", "fair", "poor"]},
    {"species": "TVOC", "thresholds": [215, 343], "interval_grades": ["good", "fair", "poor"]},
    {"species": "TEMPERATURE", "thresholds": [18, 20, 26, 28], "interval_grades": ["poor", "fair", "good", "fair", "poor"]},
    {"species": "HUMIDITY", "thresholds": [25, 40, 60, 75], "interval_grades": ["poor", "fair", "good", "fair", "poor"]},
]


def config(layout, aqi_500):
    return {"meta": {"extra": {"aqi_config": {"widget": {"indoor": {"IAQI": {}}}}}},
            "response": {"name": "Company XYZ", "layout": layout, "is_aqi_500": aqi_500, "interval": "INTERVAL_5MIN",
                         "rooms": ["room_demo"], "outdoors": [], "time_format": "HOURS_12", "disable_branding": False,
                         "config": {"aq_settings": {"grades": GRADES, "thresholds": THRESHOLDS}}}}


def indoor_readings(iaqi):
    return {"IAQI": block(iaqi, "IAQI"), "PM25": block(12.4, "MICROGRAMS_PER_CUBIC_METER"),
            "PM10": block(19.6, "MICROGRAMS_PER_CUBIC_METER"), "CO2": block(612, "PARTS_PER_MILLION"),
            "TVOC": block(96, "PARTS_PER_BILLION"), "TEMPERATURE": block(23.4, "CELSIUS"),
            "HUMIDITY": block(48, "PERCENT")}


def timeseries(indexes, outdoor=None, rest=False, weekly=None):
    ts = {"meta": {"extra": {"is_rest_time": rest, "weekly_averages": {"indoor": {"indexes": weekly or {}}, "outdoor": {}}}},
          "response": {"name": "Company XYZ - Floor 3", "data": {"indexes": indexes}}}
    if outdoor:
        ts["response"]["outdoor"] = {"indexes": outdoor}
    return ts


# ---------- render ----------

def main():
    import time
    import client_assets
    import kiosk_renderer as kr

    class FakeFB:
        def __init__(self, *a, **k):
            self.w, self.h, self.bpp, self.stride = W, H, 16, W * 2
            self.last = None

        def show(self, image):
            self.last = image.copy()

    kr.Framebuffer = FakeFB
    os.makedirs(OUT, exist_ok=True)
    src = os.path.join(WORK, "src")
    os.makedirs(src)
    demo_background(os.path.join(src, "background.jpg"))
    demo_logo(os.path.join(src, "logo.png"))
    demo_qr(os.path.join(src, "qr.png"))

    def make(name, cfg_json, snapshot, branding):
        with open(os.environ["KIOSK_CONFIG"], "w") as f:
            json.dump(cfg_json, f)
        wanted = {"background_img": os.path.join(src, "background.jpg") if branding else None,
                  "logo": os.path.join(src, "logo.png") if branding else None,
                  "qr_code_img": os.path.join(src, "qr.png") if branding else None}
        client_assets.sync(wanted, log=lambda m: None)
        r = kr.Renderer()
        r._config = snapshot["config"]
        layout = kr.choose_layout(r.cfg, r._config, snapshot["timeseries"])
        (r.draw_indoor if layout == "indoor" else r.draw_widget)(snapshot)
        r.fb.last.save(os.path.join(OUT, name))
        print("wrote", name, f"({layout})")
        return r

    base_cfg = {"api_base": "https://example.invalid", "widget_id": "widget_demo", "secret_key": "demo",
                "device_label": "demo", "poll_config_every_sec": 1800}
    now = time.time()
    indoor_cfg = config("TYPE_INDOOR", False)
    cmp_cfg = config("TYPE_INDOOR_OUTDOOR_CMP", True)
    outdoor = {"IAQI": block(118, "IAQI"), "PM25": block(46, "MICROGRAMS_PER_CUBIC_METER"),
               "PM10": block(88, "MICROGRAMS_PER_CUBIC_METER"), "TEMPERATURE": block(31, "CELSIUS"),
               "HUMIDITY": block(62, "PERCENT")}
    cmp_indoor = dict(indoor_readings(42))
    cmp_indoor.pop("TVOC")

    def snap(cfg, ts):
        return {"config": cfg, "timeseries": ts, "last_good_at": now}

    make("indoor-good.png", dict(base_cfg, outdoor="none"), snap(indoor_cfg, timeseries(indoor_readings(2))), False)
    make("indoor-fair.png", dict(base_cfg, outdoor="none"), snap(indoor_cfg, timeseries(indoor_readings(4))), False)
    make("indoor-bad.png", dict(base_cfg, outdoor="none"), snap(indoor_cfg, timeseries(indoor_readings(7))), False)
    weekly = {k: v for k, v in indoor_readings(2).items() if k in ("IAQI", "CO2", "PM25", "TEMPERATURE", "HUMIDITY")}
    empty = {k: block(None, v["unit"]) for k, v in indoor_readings(2).items()}
    make("indoor-weekly.png", dict(base_cfg, outdoor="none"), snap(indoor_cfg, timeseries(empty, weekly=weekly)), False)
    make("indoor-branded.png", dict(base_cfg, outdoor="none"), snap(indoor_cfg, timeseries(indoor_readings(2))), True)
    make("indoor-outdoor.png", dict(base_cfg, outdoor="widget"), snap(cmp_cfg, timeseries(cmp_indoor, outdoor)), False)
    r = make("indoor-outdoor-branded.png", dict(base_cfg, outdoor="widget"), snap(cmp_cfg, timeseries(cmp_indoor, outdoor)), True)
    r._boot_frame(2).save(os.path.join(OUT, "boot-screen.png"))
    print("wrote boot-screen.png")

    # one overview sheet of the indoor-only grades
    names = ["indoor-good.png", "indoor-fair.png", "indoor-bad.png", "indoor-weekly.png"]
    sheet = Image.new("RGB", (W, H))
    for i, n in enumerate(names):
        sheet.paste(Image.open(os.path.join(OUT, n)).resize((W // 2, H // 2)), ((i % 2) * W // 2, (i // 2) * H // 2))
    sheet.save(os.path.join(OUT, "indoor-grades.png"))
    print("wrote indoor-grades.png")


if __name__ == "__main__":
    main()
