#!/usr/bin/env python3
"""
Native kiosk renderer. Replaces Chromium + labwc + XWayland entirely.

RENDER BACKEND (2026-09-23, rewrite): this used to render via pygame/SDL2's
"kmsdrm" video driver, drawing directly through DRM/KMS with no compositor.
That worked fine on the original bench device, but on a freshly-imaged
Raspberry Pi OS Trixie image it stopped producing any visible output at all
-- confirmed via direct DRM debugfs introspection: a minimal five-line
pygame script (no app logic at all) correctly committed its buffer to the
right plane/CRTC/connector, with zero errors, yet nothing appeared on the
physical screen. Switching to the older vc4-fkms-v3d overlay made it fail
LOUDLY instead ("Could not queue pageflip: -22") but still showed nothing.
Both are real upstream Raspberry Pi OS / Mesa / SDL2 regressions, unrelated
to anything in this file -- proven by the fact that a script with none of
this project's code reproduced the identical failure.

The one thing that worked correctly and consistently through that entire
investigation was /dev/fb0 -- it's what showed the kernel's own boot and
console text on the physical screen every single time. So this renderer no
longer goes through SDL2/DRM/KMS at all: it draws with Pillow (a pure
Python/C imaging library with no display-driver dependency) onto an
in-memory image, then writes that image's raw pixels directly into the
framebuffer device via mmap -- the lowest-level path available, and the
only one proven to actually work on this hardware/OS combination. See the
Framebuffer class below.

Boot sequence:
  1. Open /dev/fb0 and paint the boot logo full-screen immediately -- this
     is the "single image, held until real content is ready" behaviour
     that was the whole point of this rewrite (both this one and the
     original pygame one before it).
  2. Start the API-polling thread (api_client) in the background.
  3. As soon as a first valid data snapshot exists, switch to the live
     widget and keep redrawing it (clock every second, values whenever the
     snapshot file changes). If the API goes quiet, keep showing the last
     good values with a "stale since Xm" note instead of blanking.

LAYOUT NOTE (carried over unchanged from the pygame build): none of the
positions/colors/thresholds below are reverse-engineered from a screenshot.
They're built from the real production bundle of myindoor.global
(a real comparison-layout widget) -- fetched and read directly:
  - index-D0XbSKC6.css, ComparisonWidget-DBp-c1mf.css, WidgetSpecies-*.css
    (the real, un-minified-enough CSS -- exact colors, vw/vh-based font
    sizes and spacing, exact 55%/45% indoor/outdoor panel split).
  - ComparisonWidget-*.js / index-*.js (the real component logic -- how the
    status word's color/label is chosen, which species get which coloring
    rule, the six real AQI-band colors, the CO2 rest-time message).
  - Both live API endpoints' real JSON shape (config + timeseries),
    including aqi_config/aq_settings, comparison.left thresholds, and
    weekly_averages / is_rest_time.
  - Direct getBoundingClientRect() measurements of every real element on
    the live page, confirming the exact panel split, column positions, and
    stacking order -- including that the OUTDOOR panel actually shows its
    metric row ABOVE the big status word with column labels hidden, the
    reverse of the indoor panel.
  - The real six-color-band glow behind the big status word: two
    overlapping Gaussian-blurred circles, extracted as actual SVGs from
    the bundle and rasterized (see assets/glow/*.png), not a hand-drawn
    approximation.
All positions below are stored as FRACTIONS (0..1) of screen width/height,
matching the real page's vw/vh-based sizing -- this is what actually
satisfies "works from a 24 inch monitor to a 55 inch TV". Fractions
measured from a real element's WIDTH-based CSS rule (vw, or an image whose
aspect ratio must be preserved) are applied against self.w; fractions from
a HEIGHT-based rule (vh) are applied against self.h.
"""
import json
import mmap
import os
import threading
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import api_client
import client_assets
from indoor_layout import IndoorLayout

CONFIG_PATH = os.environ.get("KIOSK_CONFIG", "/etc/kiosk-renderer/config.json")
CACHE_PATH = os.environ.get("KIOSK_CACHE", "/var/lib/kiosk-renderer/latest.json")
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
# Pre-rendered boot screens in raw framebuffer format, one per geometry, so
# kiosk_splash.py can put the image up at boot without loading PIL/numpy.
BOOT_RAW_DIR = os.path.dirname(CACHE_PATH)
SCREEN_STATE_PATH = os.path.join(BOOT_RAW_DIR, "screen-state")  # read by kiosk_splash.py --guard
REDRAW_REQUEST_PATH = os.path.join(BOOT_RAW_DIR, "redraw-request")  # written by kiosk_splash.py --guard
ASSET_RETRY_SEC = 300

# --- Real six-tier AQI banding, confirmed from the live legend bar's own
# SVG gradient stops (bottom-left color scale) and cross-checked against
# the widget config API's aqi_config.widget.comparison.left.labels
# (satisfactory:51, moderate:101, poor:201, veryPoor:301, severe:401).
# Value < threshold picks that band; still >= the last one -> severe.
AQI_BANDS = [
    (51, "Good", (135, 175, 27)),        # #87AF1B
    (101, "Satisfactory", (190, 198, 23)),  # #BEC617
    (201, "Moderate", (255, 184, 0)),    # #FFB800
    (301, "Poor", (255, 115, 68)),       # #FF7344
    (401, "Very Poor", (200, 62, 73)),   # #C83E49
    (float("inf"), "Severe", (171, 93, 205)),  # #AB5DCD
]
GLOW_ASSET_BY_LABEL = {
    "Good": "glow-good.png",
    "Satisfactory": "glow-satisfactory.png",
    "Moderate": "glow-moderate.png",
    "Poor": "glow-poor.png",
    "Very Poor": "glow-verypoor.png",
    "Severe": "glow-severe.png",
}

# Temperature/Humidity use their OWN band tables (interval_grades from the
# real config's aq_settings.thresholds), not the AQI scale above --
# confirmed by reading the actual rendered dot color for both rows on the
# live page (e.g. indoor humidity 73% -> yellow, outdoor temp 34C ->
# orange) and matching it back to these exact thresholds/colors, not the
# species' own aq_settings 2-threshold good/fair/poor table (which would
# have given a different color -- verified wrong before trusting this).
GRADE_COLOR = {
    "good": (135, 175, 27),   # #87AF1B
    "fair": (255, 184, 0),    # #FFB800
    "poor": (255, 115, 68),   # #FF7344
}
TEMPERATURE_BANDS = ([18, 20, 27, 30, 35], ["poor", "fair", "good", "fair", "poor"])
HUMIDITY_BANDS = ([25, 40, 60, 75, 100], ["poor", "fair", "good", "fair", "poor"])
# CO2's own simple 3-tier table (aq_settings.thresholds for CO2).
CO2_BANDS = ([1000, 1500], ["good", "fair", "poor"])

# Five columns in the metric grid -- confirmed CO2 is NOT one of them (the
# real page pulls it out and shows it separately, see _draw_right_panel).
# Real formatting (the bundled app's own `h()` helper) rounds every value
# to 0 fraction digits by default -- confirmed side-by-side against the
# live page (it shows "5", "24", not "4.6", "24.1").
COLUMNS = [
    # (index key, display label, unit, decimals)
    ("IAQI", "AQI", "points", 0),
    ("PM25", "PM2.5", "µg/m³", 0),
    ("PM10", "PM10", "µg/m³", 0),
    ("TEMPERATURE", "Temperature", "°C", 0),
    ("HUMIDITY", "Rel. Humidity", "%", 0),
]

REST_TIME_MESSAGE = (
    "Ventilation equipment is currently off according to schedule. "
    "Operation resumes at the start of the next workday."
)

STALE_WARN_SEC = 60  # roughly 3 missed 20s polls

WHITE = (255, 255, 255)
NEAR_BLACK = (25, 25, 25)
MUTED_LABEL = (184, 191, 204)  # #b8bfcc -- real panel-name-color / element-name-color
CARD_WHITE_ALPHA = (255, 255, 255, 179)  # #ffffffb3 -- real date-pill background

FALLBACK_BG_COLOR = (30, 32, 38)  # used until a client background has been synced
LOADING_LABEL = "Airvoice Display Loading"


def dig(d, path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def aqi_band(value):
    """Real widgetStatus logic (comparison.left.labels), also used directly
    for the AQI/PM2.5/PM10 column dot colors -- confirmed empirically
    against the live page's actual rendered colors for both indoor and
    outdoor rows, not assumed from each species' own aq_settings table."""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    for threshold, label, color in AQI_BANDS:
        if value < threshold:
            return label, color
    return AQI_BANDS[-1][1], AQI_BANDS[-1][2]


def band_color(thresholds, grades, value):
    """Real per-species interval grading (TEMPERATURE/HUMIDITY/CO2): find
    the first threshold strictly greater than value, use the grade at that
    index; past the last threshold, use the last grade."""
    if value is None:
        return MUTED_LABEL
    try:
        value = float(value)
    except (TypeError, ValueError):
        return MUTED_LABEL
    for i, t in enumerate(thresholds):
        if value < t:
            return GRADE_COLOR.get(grades[i], MUTED_LABEL)
    return GRADE_COLOR.get(grades[-1], MUTED_LABEL)


def color_for_column(key, value):
    if key in ("IAQI", "PM25", "PM10"):
        band = aqi_band(value)
        return band[1] if band else MUTED_LABEL
    if key == "TEMPERATURE":
        return band_color(*TEMPERATURE_BANDS, value)
    if key == "HUMIDITY":
        return band_color(*HUMIDITY_BANDS, value)
    if key == "CO2":
        return band_color(*CO2_BANDS, value)
    return MUTED_LABEL


def latest_value(index_block):
    values = (index_block or {}).get("values") or []
    return values[-1] if values else None


def _font_height(font):
    """PIL has no direct equivalent of pygame's Font.get_height() (a fixed
    per-size line height, not a per-string bounding box) -- ascent+descent
    from the font's own metrics is the same thing, and SDL_ttf (what
    pygame.font used) is itself built on FreeType, the same engine PIL
    uses here, so metrics between the two are effectively identical for a
    given TTF file."""
    ascent, descent = font.getmetrics()
    return ascent + descent


def pack_pixels(arr, bpp):
    """numpy uint8 (h, w, 3) RGB -> raw bytes in a framebuffer's pixel
    format. The real depth is read from sysfs, not assumed: the vc4 driver
    runs 16bpp (RGB565) on the bench, while the firmware's early "simple"
    framebuffer seen for the first seconds of boot is 32bpp."""
    if bpp == 16:
        r = arr[:, :, 0].astype(np.uint16) >> 3
        g = arr[:, :, 1].astype(np.uint16) >> 2
        b = arr[:, :, 2].astype(np.uint16) >> 3
        return ((r << 11) | (g << 5) | b).astype("<u2").tobytes()
    if bpp == 24:
        return np.ascontiguousarray(arr).tobytes()
    # 32bpp: little-endian BGRA with an opaque alpha byte (the simple
    # framebuffer is declared a8r8g8b8).
    h, w, _ = arr.shape
    out = np.full((h, w, 4), 255, dtype=np.uint8)
    out[:, :, 0] = arr[:, :, 2]
    out[:, :, 1] = arr[:, :, 1]
    out[:, :, 2] = arr[:, :, 0]
    return out.tobytes()


def boot_raw_path(w, h, bpp):
    return os.path.join(BOOT_RAW_DIR, f"boot-{w}x{h}-{bpp}.raw")


class Framebuffer:
    """Direct /dev/fb0 writer -- bypasses SDL2/DRM/KMS entirely. See the
    module docstring for why. Geometry (size, bit depth, row stride) is
    read from sysfs rather than assumed, so this adapts to whatever the
    actual connected display negotiates, the same "24 inch monitor to a 55
    inch TV" goal the vw/vh layout fractions already serve."""

    def __init__(self, device="/dev/fb0"):
        sys_path = os.path.join("/sys/class/graphics", os.path.basename(device))
        with open(os.path.join(sys_path, "virtual_size")) as f:
            w_str, h_str = f.read().strip().split(",")
        with open(os.path.join(sys_path, "bits_per_pixel")) as f:
            bpp = int(f.read().strip())
        with open(os.path.join(sys_path, "stride")) as f:
            stride = int(f.read().strip())

        self.w, self.h = int(w_str), int(h_str)
        self.bpp = bpp
        self.stride = stride
        if bpp not in (16, 24, 32):
            raise RuntimeError(f"Unsupported framebuffer depth: {bpp}bpp")

        self._fd = os.open(device, os.O_RDWR)
        self._mmap = mmap.mmap(self._fd, stride * self.h, mmap.MAP_SHARED,
                                mmap.PROT_READ | mmap.PROT_WRITE)

    def show(self, image):
        """Write a full-screen PIL RGB Image to the framebuffer."""
        if image.size != (self.w, self.h):
            image = image.resize((self.w, self.h))
        packed = pack_pixels(np.asarray(image.convert("RGB")), self.bpp)
        bytes_per_row = self.w * (self.bpp // 8)
        if bytes_per_row == self.stride:
            self._mmap.seek(0)
            self._mmap.write(packed)
        else:
            for y in range(self.h):
                self._mmap.seek(y * self.stride)
                self._mmap.write(packed[y * bytes_per_row:(y + 1) * bytes_per_row])


def choose_layout(kiosk_cfg, config, timeseries):
    """'indoor' (TYPE_INDOOR design) or 'comparison' (indoor + outdoor panels).
    The installer's outdoor answer wins; "auto" follows the widget."""
    outdoor = (kiosk_cfg or {}).get("outdoor", "auto")
    if outdoor == "none":
        return "indoor"
    if outdoor == "widget":
        return "comparison"
    resp = (config or {}).get("response") or {}
    if resp:
        if resp.get("layout") in ("TYPE_INDOOR_OUTDOOR_CMP", "TYPE_INDOOR_OUTDOOR") or resp.get("outdoors"):
            return "comparison"
        return "indoor"
    return "comparison" if dig(timeseries, ("response", "outdoor")) else "indoor"


class Renderer(IndoorLayout):
    def __init__(self):
        self.fb = Framebuffer()
        self.w, self.h = self.fb.w, self.fb.h

        # Real CSS renders almost everything at font-weight:700 (widget name,
        # date pill, the big status word, metric numbers, the description
        # line) and only a few things (column name labels, units) at the
        # inherited body weight 400. Two real STATIC weights are instanced
        # out of the bundled variable font with fonttools (see
        # assets/Inter-Bold.ttf / Inter-Regular.ttf) and the correct one is
        # used per element -- a variable font's default instance renders
        # everything at one weight regardless of what's requested, which
        # was a real, visible bug in an earlier build.
        self.font_path_bold = self._resolve_font("Inter-Bold.ttf", "DejaVuSans-Bold.ttf")
        self.font_path_regular = self._resolve_font("Inter-Regular.ttf", "DejaVuSans.ttf")
        self._font_cache = {}

        with open(CONFIG_PATH, "r") as f:
            self.cfg = json.load(f)

        # Real Airvoice lockup ("Air quality monitoring by" + "airvoice",
        # with its two accent dots), extracted byte-for-byte from a full
        # saved copy of the real page's own DOM and recolored to match its
        # own real dark-mode CSS variable -- not hand-typeset text. This is
        # Airvoice's own mark, so it stays bundled; the client's assets don't.
        self.airvoice_logo = self._load_scaled_by_width(os.path.join(ASSETS_DIR, "airvoice-logo-white.png"), 0.139)
        self._glow_images = {}
        for label, filename in GLOW_ASSET_BY_LABEL.items():
            self._glow_images[label] = self._load_scaled_by_width(os.path.join(ASSETS_DIR, "glow", filename), 0.30)

        # Vertical "Powered by airvoice" mark of the indoor-only layout,
        # rasterized from the web widget's own SVG; drawn 38.4px wide at 1920.
        self.powered_by = self._load_scaled_by_width(os.path.join(ASSETS_DIR, "powered-by-airvoice-vertical.png"), 0.02)

        self._config = None  # latest widget config from the API snapshot
        self._layout = None
        self._ind_base = None
        self._ind_base_key = None
        self._assets_changed = threading.Event()
        self._asset_thread = None
        self._asset_retry_at = 0.0
        self._load_client_assets()

    def _load_client_assets(self):
        """(Re)load the client's background, logo and QR from the synced cache
        and rebuild everything that depends on them. Before the first sync
        (or if a widget has no such asset) this falls back to a plain dark
        background and simply leaves the logo/QR out -- nothing
        client-specific is bundled in the product itself."""
        self.bg_photo = self._load_cover(client_assets.cached_path("background_img"))
        # QR/logo are square/fixed-aspect real assets sized by a single
        # real vw width -- both dimensions must scale off the SAME axis (w)
        # so they don't stretch on non-16:9 screens.
        self.qr_surface = self._load_qr(client_assets.cached_path("qr_code_img"), 0.06)
        logo_path = client_assets.cached_path("logo")
        self.client_logo = self._load_scaled_by_width(logo_path, 0.16)
        try:
            self.client_logo_src = Image.open(logo_path).convert("RGBA") if logo_path else None
        except Exception:
            self.client_logo_src = None
        self._boot_logo = self._load_fitted(logo_path, 0.34, 0.30) or self._load_fitted(
            os.path.join(ASSETS_DIR, "airvoice-logo-white.png"), 0.34, 0.30)
        self._manifest = client_assets.load_manifest()
        self._ind_base_key = None

        # Everything that never changes at runtime (background photo, both
        # darkening overlays, the legend gradient, QR/branding) is composited
        # ONCE here instead of on every redraw -- see _build_static_base.
        self._static_base = self._build_static_base()
        self._boot_base, self._boot_logo_bottom = self._build_boot_base()
        self._composited = None
        self._data_key = None
        self._write_boot_raw()

    # ---------- small helpers ----------

    def fx(self, frac):
        return round(frac * self.w)

    def fy(self, frac):
        return round(frac * self.h)

    def _resolve_font(self, bundled_name, system_fallback):
        candidates = [
            os.path.join(ASSETS_DIR, bundled_name),
            os.path.join("/usr/share/fonts/truetype/dejavu", system_fallback),
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        return None

    def _font(self, px, bold=True):
        px = max(1, round(px))
        key = (px, bold)
        cached = self._font_cache.get(key)
        if cached is None:
            path = self.font_path_bold if bold else self.font_path_regular
            cached = ImageFont.truetype(path, px) if path else ImageFont.load_default()
            self._font_cache[key] = cached
        return cached

    def _font_w(self, width_frac, bold=True):
        """Font sized from a real `vw` CSS value -- MUST scale off screen
        WIDTH. (An earlier build scaled every font off height, which only
        happened to look right at exactly 16:9.)"""
        return self._font(self.fx(width_frac), bold=bold)

    def _load_cover(self, path):
        if not path or not os.path.exists(path):
            return None
        try:
            img = Image.open(path).convert("RGB")
        except Exception:
            return None
        iw, ih = img.size
        scale = max(self.w / iw, self.h / ih)
        new_size = (round(iw * scale), round(ih * scale))
        img = img.resize(new_size, Image.LANCZOS)
        x = (new_size[0] - self.w) // 2
        y = (new_size[1] - self.h) // 2
        return img.crop((x, y, x + self.w, y + self.h))

    def _load_scaled_by_width(self, path, width_frac):
        if not path or not os.path.exists(path):
            return None
        try:
            img = Image.open(path).convert("RGBA")
        except Exception:
            return None
        iw, ih = img.size
        target_w = self.fx(width_frac)
        target_h = round(target_w * ih / iw)
        return img.resize((max(1, target_w), max(1, target_h)), Image.LANCZOS)

    def _load_fitted(self, path, max_w_frac, max_h_frac):
        """Scale an image (aspect kept) to fit inside a box -- for the boot
        screen logo, where client logos can be any shape."""
        if not path or not os.path.exists(path):
            return None
        try:
            img = Image.open(path).convert("RGBA")
        except Exception:
            return None
        scale = min(self.fx(max_w_frac) / img.width, self.fy(max_h_frac) / img.height)
        return img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)

    def _load_qr(self, path, width_frac):
        """The QR asset ships with its own 40px white border baked in, which
        stacked with the card's padding into a margin of over 30%. Crop the
        pattern out so the card's padding is the only margin, and scale by a
        whole number of pixels per module -- uneven module sizes scan badly."""
        if not path or not os.path.exists(path):
            return None
        try:
            img = Image.open(path).convert("L")
        except Exception:
            return None
        bbox = img.point(lambda p: 255 if p < 128 else 0).getbbox()
        if not bbox:
            return None
        img = img.crop(bbox)
        finder = 0
        while finder < img.width and img.getpixel((finder, 0)) < 128:
            finder += 1
        modules = round(img.width / (finder / 7)) if finder else img.width  # finder pattern is 7 modules wide
        px = max(1, round(self.fx(width_frac) / modules))
        return img.resize((modules * px, modules * px), Image.NEAREST).convert("RGBA")

    def _fit_font_size(self, text, max_width_frac, max_line_width):
        """The real page auto-shrinks the big status word so long ones like
        "Satisfactory" or "Very Poor" still fit -- confirmed visually (its
        rendered font-size is smaller than "Good"'s for the longer word)."""
        size = self.fx(max_width_frac)
        font = self._font(size, bold=True)
        while font.getlength(text) > max_line_width and size > 12:
            size -= 2
            font = self._font(size, bold=True)
        return font

    # ---------- boot screen ----------

    def _build_boot_base(self):
        """Boot screen built from the client's own assets: their background
        photo darkened like the widget's, with their logo centered. Client
        logos in the widget settings are white (made for the dark widget),
        so the boot screen is dark too rather than the old white one."""
        if self.bg_photo:
            base = self.bg_photo.copy()
        else:
            base = Image.new("RGB", (self.w, self.h), FALLBACK_BG_COLOR)
        base = Image.alpha_composite(base.convert("RGBA"),
                                     Image.new("RGBA", base.size, (0, 0, 0, 150))).convert("RGB")
        logo_bottom = self.h // 2
        if self._boot_logo:
            pos = (self.w // 2 - self._boot_logo.width // 2, round(self.h * 0.44) - self._boot_logo.height // 2)
            base.paste(self._boot_logo, pos, self._boot_logo)
            logo_bottom = pos[1] + self._boot_logo.height
        return base, logo_bottom

    def _boot_frame(self, frame):
        canvas = self._boot_base.copy()
        font = self._font_w(0.018)
        text = f"{LOADING_LABEL}{'.' * (1 + frame % 6)}"
        ImageDraw.Draw(canvas).text((self.w // 2 - int(font.getlength(LOADING_LABEL + '...')) // 2,
                                     self._boot_logo_bottom + self.fy(0.05)), text, font=font, fill=WHITE)
        return canvas

    def show_boot_logo(self, frame=0):
        """Held until the first successful data fetch, then a hard cutover to
        draw_widget(). Same image kiosk_splash.py puts up earlier in boot,
        so the handover between them is seamless."""
        self.fb.show(self._boot_frame(frame))

    def _write_boot_raw(self):
        """Save the boot screen as raw framebuffer bytes for kiosk_splash.py.
        Both depths are written: the firmware's early framebuffer is 32bpp,
        the vc4 driver that replaces it a few seconds later is 16bpp."""
        arr = np.asarray(self._boot_frame(2))  # "..." -- static, dots don't animate before the renderer runs
        for bpp in (16, 32):
            path = boot_raw_path(self.w, self.h, bpp)
            data = pack_pixels(arr, bpp)
            try:
                with open(path, "rb") as f:
                    if f.read() == data:
                        continue
            except OSError:
                pass
            try:
                with open(path + ".tmp", "wb") as f:
                    f.write(data)
                os.replace(path + ".tmp", path)
            except OSError as e:
                api_client.api_log.info("boot frame %s: write failed: %s", path, e)

    def load_snapshot(self):
        try:
            with open(CACHE_PATH, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    # ---------- static layer (never changes at runtime) ----------

    def _build_static_base(self):
        """Everything on this screen that never depends on live data --
        the background photo, both darkening overlays, the AQI legend, and
        the QR/branding block -- composited ONCE here instead of on every
        redraw. See the CPU-fix note in draw_widget()/_build_data_layer()
        for why this matters (this same "only build it once" principle
        carried straight over from the pygame build)."""
        w, h = self.w, self.h

        if self.bg_photo:
            base = self.bg_photo.copy()
        else:
            base = Image.new("RGB", (w, h), (30, 32, 38))

        # Real page uses linear-gradient(rgba(0,0,0,0.5), rgba(0,0,0,0.5))
        # over the photo -- a flat 50% black overlay, confirmed via computed
        # style, not a guess.
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 128))
        base = Image.alpha_composite(base.convert("RGBA"), overlay)

        # Real CSS applies a SECOND, separate darkening specifically over
        # the outdoor panel, on top of the shared 50% overlay above
        # (._backgroundImageMode_ ._outdoor_{background-color:#00000059;
        # border-radius:4vw 4vw 0 0}) -- a ~35% black card with rounded top
        # corners sitting over just the bottom 45%.
        outdoor_h = round(h * 0.45)
        mask_radius = self.fx(0.04)
        mask_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(mask_layer).rounded_rectangle(
            [0, h - outdoor_h, w, h], radius=mask_radius, fill=(0, 0, 0, 89),
            corners=(True, True, False, False))
        base = Image.alpha_composite(base, mask_layer).convert("RGB")

        # NOTE: the rotated "indoor"/"outdoor" edge labels are NOT drawn
        # here even though their text never changes -- they must paint
        # AFTER the status-word glow (glow is a background-image, always
        # the lowest layer; see _build_data_layer), which does depend on
        # live data. Baking them into this truly-static base would put
        # them UNDER the glow every time the data layer gets recomposited.
        # So they're drawn in _build_data_layer instead, right after the
        # two glow pastes.

        # --- AQI color legend, bottom-left (real 6-stop gradient pill) ---
        self._draw_legend(base, self.fx(0.0097), self.fy(0.9004), self.fx(0.4005))

        # --- QR + "Learn more" and client/airvoice branding, bottom-right.
        # Real CSS anchors BOTH independently from the screen's BOTTOM edge
        # (QR container: bottom:20vh; branding container: bottom:5vh) --
        # NOT one stacked below the other.
        if self.qr_surface:
            draw = ImageDraw.Draw(base)
            qr_size = self.qr_surface.width
            card_pad = max(2, round(qr_size * 0.10))  # 10% of the pattern, the card's only margin
            card_size = qr_size + card_pad * 2
            card_cx = self.fx(0.8976)  # same horizontal center the card has always had
            card_bottom = h - self.fy(0.20)
            card_x, card_y = card_cx - card_size // 2, card_bottom - card_size
            draw.rounded_rectangle([card_x, card_y, card_x + card_size, card_bottom],
                                   radius=self.fx(0.006), fill=WHITE)
            base.paste(self.qr_surface, (card_x + card_pad, card_y + card_pad), self.qr_surface)
            lm_font = self._font_w(0.0115)
            lm_text = "Learn more"
            tw = lm_font.getlength(lm_text)
            draw.text((card_cx - int(tw) // 2, card_bottom + self.fy(0.012)), lm_text, font=lm_font, fill=WHITE)

        brand_x = self.fx(0.6598)
        brand_bottom = h - self.fy(0.05)
        text_x = brand_x
        if self.client_logo:
            logo_y = brand_bottom - self.client_logo.height
            base.paste(self.client_logo, (brand_x, logo_y), self.client_logo)
            text_x = brand_x + self.client_logo.width + self.fx(0.02)
        if self.airvoice_logo:
            base.paste(self.airvoice_logo, (text_x, brand_bottom - self.airvoice_logo.height), self.airvoice_logo)

        return base

    # ---------- live widget, built to match the real page ----------

    def _compute_data_key(self, is_rest_time, name, indexes, outdoor_indexes):
        """Cheap fingerprint of everything _build_data_layer draws, so a
        redraw can skip rebuilding it on every 1s tick and only do it when
        the underlying values actually changed -- which in practice is
        roughly every 20s, when the API-polling thread writes a new
        snapshot. (This caching strategy is unchanged from the pygame
        build -- it's what fixed a real ~100%+ CPU bug there, and applies
        just as much here: Pillow's drawing is not free either.)"""
        def col_vals(idx):
            return tuple(latest_value(idx.get(key)) for key, _, _, _ in COLUMNS)
        co2 = latest_value(indexes.get("CO2")) if is_rest_time else None
        return (is_rest_time, name, col_vals(indexes), col_vals(outdoor_indexes), co2)

    def _build_data_layer(self, target, is_rest_time, indexes, outdoor_indexes, name):
        """Everything that depends on the live snapshot -- the status-word
        glows, the tower name, the status words + description, both metric
        grids, and the rest-time right panel -- drawn directly onto
        `target` (a fresh copy of the static base; see draw_widget), which
        draw_widget then caches and reuses across redraws where the data
        hasn't actually changed."""
        w = self.w
        layer = target
        draw = ImageDraw.Draw(layer)

        # Real markup: the glow is a background-image (z-index -1) on the
        # panel, so it always sits BEHIND every other element. Both
        # statuses are resolved and their glows pasted FIRST, before any
        # text on this layer, to match that real stacking order.
        indoor_iaqi = latest_value(indexes.get("IAQI"))
        indoor_band = aqi_band(indoor_iaqi)
        indoor_status = indoor_band[0] if indoor_band else "--"
        status_x, status_y = self.fx(0.070), self.fy(0.1547)
        status_max_line = round(w * 0.55)
        status_font = self._fit_font_size(indoor_status, 0.08, status_max_line)  # 8vw
        status_h = _font_height(status_font)
        self._draw_glow(layer, indoor_status, status_x, status_y, status_h)

        # Real 55% / 45% vertical split -- confirmed exactly on the live
        # page (._indoor_c1opi_55{height:55%} / ._outdoor_c1opi_1{height:45%}).
        indoor_top, outdoor_top = 0.0, 0.55

        outdoor_iaqi = latest_value(outdoor_indexes.get("IAQI"))
        outdoor_band = aqi_band(outdoor_iaqi)
        outdoor_status = outdoor_band[0] if outdoor_band else "--"
        out_status_y = self.fy(outdoor_top + 0.1094)  # ~11vh into the outdoor panel, mirrors indoor's own offset
        out_status_font = self._fit_font_size(outdoor_status, 0.08, status_max_line)
        out_status_h = _font_height(out_status_font)
        self._draw_glow(layer, outdoor_status, status_x, out_status_y, out_status_h)

        # --- rotated "indoor" / "outdoor" edge labels, drawn with a soft
        # dark drop-shadow so they stay readable regardless of what part of
        # the photo ends up behind them. Text-wise this never changes, but
        # it must paint AFTER the glow above (glow = background layer,
        # always lowest) -- see the note in _build_static_base. ---
        side_font = self._font_w(0.025)  # 2.5vw
        for label, panel_center_y in (("indoor", indoor_top + 0.275), ("outdoor", outdoor_top + 0.225)):
            tw = int(side_font.getlength(label))
            th = _font_height(side_font)
            pad = 4
            text_img = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
            ImageDraw.Draw(text_img).text((pad, pad), label, font=side_font, fill=MUTED_LABEL + (255,))
            rotated = text_img.rotate(90, expand=True)
            center = (self.fx(0.03), self.fy(panel_center_y))
            pos = (center[0] - rotated.width // 2, center[1] - rotated.height // 2)
            layer.paste(rotated, pos, rotated)

        # --- widget name (indoor panel title) ---
        tower_font = self._font_w(0.015)  # 1.5vw
        draw.text((self.fx(0.070), self.fy(0.060)), str(name), font=tower_font, fill=WHITE)

        # --- indoor panel: status word text -> description -> metric grid
        # (glow for this word was already pasted above, behind everything) ---
        draw.text((status_x, status_y), indoor_status, font=status_font, fill=WHITE)

        # --- description line under the status word: "Weekly averages
        # shown below" during rest time; a best-effort working-hours line
        # otherwise (real i18n copy not yet confirmed live during working
        # hours -- flagged for comparison once seen).
        desc_font = self._font_w(0.02)  # 2vw
        if is_rest_time:
            desc_text = "Weekly averages shown below"
        else:
            desc_text = f"Indoor air quality is {indoor_status.lower()}"
        draw.text((self.fx(0.0802), self.fy(0.3435)), desc_text, font=desc_font, fill=WHITE)

        # --- indoor metric grid (5 columns, WITH labels) ---
        # Real column x-positions measured directly at 1920x1080: 134, 346,
        # 557, 768, 979 -- a consistent ~211px (0.1099w) step.
        grid_x0 = self.fx(0.070)
        grid_y = self.fy(0.4657)  # real: 503/1080
        col_w = round(w * 0.1099)
        self._draw_metric_row(layer, indexes, grid_x0, grid_y, col_w, show_labels=True)

        # --- right panel: rest-time ventilation message + CO2, only shown
        # during rest time (matches the real page) ---
        if is_rest_time:
            self._draw_right_panel(layer, indexes)

        # --- outdoor panel: metric grid (NO labels) ABOVE the status word
        # -- confirmed by direct measurement this is the real stacking
        # order for the outdoor panel, the reverse of indoor's ---
        out_grid_y = self.fy(0.5704)  # real: 616/1080 (not exactly the panel's own top edge)
        self._draw_metric_row(layer, outdoor_indexes, grid_x0, out_grid_y, col_w, show_labels=False)

        # (outdoor status/glow were already resolved and pasted above, behind everything)
        draw.text((status_x, out_status_y), outdoor_status, font=out_status_font, fill=WHITE)

        return layer

    def draw_widget(self, snapshot):
        w, h = self.w, self.h

        timeseries = snapshot.get("timeseries") or {}

        is_rest_time = bool(dig(timeseries, ("meta", "extra", "is_rest_time"), False))
        if is_rest_time:
            indexes = dig(timeseries, ("meta", "extra", "weekly_averages", "indoor", "indexes"), {}) or {}
            outdoor_indexes = dig(timeseries, ("meta", "extra", "weekly_averages", "outdoor", "indexes"), {}) or {}
        else:
            indexes = dig(timeseries, ("response", "data", "indexes"), {}) or {}
            outdoor_indexes = dig(timeseries, ("response", "outdoor", "indexes"), {}) or {}

        name = dig(timeseries, ("response", "name"), self.cfg.get("device_label", "Kiosk"))

        # Rebuild the composited frame only when the values it draws
        # actually changed (in practice ~every 20s, on a fresh API poll)
        # instead of on every 1s tick.
        key = self._compute_data_key(is_rest_time, name, indexes, outdoor_indexes)
        if key != self._data_key or self._composited is None:
            self._composited = self._static_base.copy()
            self._build_data_layer(self._composited, is_rest_time, indexes, outdoor_indexes, name)
            self._data_key = key

        frame = self._composited.copy()
        draw = ImageDraw.Draw(frame)

        # --- date/time pill, top-right -- the one thing on screen that
        # genuinely needs to be re-rendered on every tick. Composited only
        # over its own small region, not the whole frame, to keep this
        # cheap. ---
        now_text = time.strftime("%A %B %d, %Y | %I:%M %p")
        pill_font = self._font_w(0.015)  # 1.5vw, real CSS value
        pill_text_h = _font_height(pill_font)
        pill_text_w = pill_font.getlength(now_text)
        pill_pad_x, pill_pad_y = self.fx(0.02), self.fy(0.007)
        pill_w, pill_h = int(pill_text_w) + pill_pad_x * 2, pill_text_h + pill_pad_y * 2
        pill_x, pill_y = w - self.fx(0.05) - pill_w, self.fy(0.05)

        pill_region = frame.crop((pill_x, pill_y, pill_x + pill_w, pill_y + pill_h)).convert("RGBA")
        pill_shape = Image.new("RGBA", (pill_w, pill_h), (0, 0, 0, 0))
        ImageDraw.Draw(pill_shape).rounded_rectangle(
            [0, 0, pill_w, pill_h], radius=self.fx(0.01), fill=CARD_WHITE_ALPHA)
        pill_region = Image.alpha_composite(pill_region, pill_shape).convert("RGB")
        frame.paste(pill_region, (pill_x, pill_y))
        draw.text((pill_x + pill_pad_x, pill_y + pill_pad_y), now_text, font=pill_font, fill=NEAR_BLACK)

        # --- staleness note (own addition, not on the real page -- this is
        # what happens here specifically when the API can't be reached) ---
        last_good_at = snapshot.get("last_good_at")
        if last_good_at:
            age = time.time() - last_good_at
            if age > STALE_WARN_SEC:
                mins = int(age // 60)
                stale_font = self._font_w(0.0104)
                draw.text((self.fx(0.0097), h - self.fy(0.022)),
                          f"Data stale -- last update {mins}m ago", font=stale_font, fill=(255, 140, 120))

        self.fb.show(frame)

    def _draw_glow(self, target, status_label, status_x, status_y, text_height):
        """The real glow: two overlapping Gaussian-blurred circles behind
        the status word, extracted from the real page's own SVG asset and
        rasterized (assets/glow/*.png) -- not a hand-drawn approximation.
        The text itself is plain white; all the color comes from this."""
        glow = self._glow_images.get(status_label)
        if not glow:
            return
        cx = status_x + round(glow.width * 0.12)
        cy = status_y + text_height // 2
        pos = (cx - glow.width // 2, cy - glow.height // 2)
        target.paste(glow, pos, glow)

    def _draw_metric_row(self, target, indexes, x0, y, col_w, show_labels):
        # Real CSS: elementName is font-weight:400 (regular), number is 700
        # (bold), unit inherits the regular body weight too.
        draw = ImageDraw.Draw(target)
        label_font = self._font_w(0.01, bold=False)   # 1vw (only used when show_labels)
        value_font = self._font_w(0.022, bold=True)   # 2.2vw
        unit_font = self._font_w(0.008, bold=False)   # ~0.8vw (element-font-size - 0.2vw)
        for i, (key, label, unit, decimals) in enumerate(COLUMNS):
            col_x = x0 + i * col_w
            value = latest_value(indexes.get(key))
            color = color_for_column(key, value)
            row_y = y
            if show_labels:
                draw.text((col_x, row_y), label, font=label_font, fill=MUTED_LABEL)
                row_y += _font_height(label_font) + self.fy(0.003)
            self._draw_metric_value(target, col_x, row_y, value, unit, decimals, color, value_font, unit_font)

    def _draw_metric_value(self, target, x, y, value, unit, decimals, color, font_value, font_unit):
        # Real CSS colorLegend is a small rounded-rect PILL (width ~
        # number-font-size/3, capped 7px real; height ~ number-font-size/1.5;
        # border-radius:5px), not a circle -- confirmed via computed style.
        draw = ImageDraw.Draw(target)
        value_h = _font_height(font_value)
        pill_h = round(value_h / 1.5)
        pill_w = max(2, min(self.fx(0.0036), round(value_h / 3)))
        pill_y = y + (value_h - pill_h) // 2
        draw.rounded_rectangle([x, pill_y, x + pill_w, pill_y + pill_h],
                                radius=max(1, pill_w // 2), fill=color)
        if value is None:
            text = "--"
        elif decimals == 0:
            text = str(round(value))  # round(x, 0) returns a float (e.g. "5.0") -- real page shows plain ints
        else:
            text = str(round(value, decimals))
        vx = x + pill_w + self.fx(0.006)
        draw.text((vx, y), text, font=font_value, fill=WHITE)
        val_w = font_value.getlength(text)
        unit_h = _font_height(font_unit)
        draw.text((vx + int(val_w) + self.fx(0.005), y + value_h - unit_h), unit, font=font_unit, fill=MUTED_LABEL)

    def _draw_right_panel(self, target, indexes):
        """Real right-panel block: a left border divider, the rest-time
        ventilation message as plain wrapped text (no card -- the real
        page has no background box here), and the CO2 reading by itself
        below it, using the same dot+number+unit style as the metric grid.
        Real message text measured directly on the live page at 1920x1080:
        x=1346 (0.701w), y=231 (0.2139h), width=574 (0.299w), font-size
        38.4px (2vw)."""
        draw = ImageDraw.Draw(target)
        divider_x_anchor = self.fx(0.6803)
        y = self.fy(0.2139)
        x = self.fx(0.701)
        max_width = self.fx(0.299)

        # Real markup: a circled lowercase "i" (info) icon precedes the
        # message text -- a circle, a small dot near the top, and a
        # separate stem below it: literally an "i" glyph.
        icon_r = self.fx(0.0075)
        icon_cx, icon_cy = x + icon_r, y + icon_r
        text_x = x + round(icon_r * 2.8)
        text_max_width = max_width - (text_x - x)

        font = self._font_w(0.02, bold=False)  # real restTimeDesc: font-size 2vw, weight 500
        words = REST_TIME_MESSAGE.split(" ")
        lines, current = [], ""
        for word in words:
            trial = f"{current} {word}".strip()
            if font.getlength(trial) > text_max_width and current:
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)
        # Real line-height for this text is ~1.14x the font size (measured:
        # the real message box is 219px tall for 5 lines at 38.4px font =
        # 43.8px/line), plus the real page's own fixed 53px margin-bottom
        # after this paragraph -- reproduces the real, safely-clear CO2
        # position (503/1080) without needing to clamp anything.
        line_h = round(self.fx(0.02) * 1.14)
        message_gap = self.fy(53 / 1080)

        label_font = self._font_w(0.01, bold=False)
        value_font = self._font_w(0.022, bold=True)
        unit_font = self._font_w(0.008, bold=False)
        co2_label_h = _font_height(label_font)
        co2_value_h = _font_height(value_font)
        co2_y = y + len(lines) * line_h + message_gap
        divider_h = (co2_y - y) + co2_label_h + self.fy(0.003) + co2_value_h

        draw.line([(divider_x_anchor, y), (divider_x_anchor, y + divider_h)], fill=(189, 208, 242), width=2)
        draw.ellipse([icon_cx - icon_r, icon_cy - icon_r, icon_cx + icon_r, icon_cy + icon_r],
                     outline=WHITE, width=max(1, round(icon_r * 0.15)))
        dot_r = max(1, round(icon_r * 0.075))  # real SVG: outer r=10, dot r=0.75 -> 0.075x, not 0.16x
        dot_cy = icon_cy - round(icon_r * 0.45)
        draw.ellipse([icon_cx - dot_r, dot_cy - dot_r, icon_cx + dot_r, dot_cy + dot_r], fill=WHITE)
        draw.line([(icon_cx, icon_cy - round(icon_r * 0.2)), (icon_cx, icon_cy + round(icon_r * 0.5))],
                  fill=WHITE, width=max(1, round(icon_r * 0.18)))
        for i, line in enumerate(lines):
            draw.text((text_x, y + i * line_h), line, font=font, fill=WHITE)

        # Real page shows a "CO2" label above the value, same as every
        # other metric column.
        co2_value = latest_value(indexes.get("CO2"))
        co2_color = color_for_column("CO2", co2_value)
        draw.text((x, co2_y), "CO2", font=label_font, fill=MUTED_LABEL)
        self._draw_metric_value(target, x, co2_y + co2_label_h + self.fy(0.003), co2_value, "ppm", 0,
                                 co2_color, value_font, unit_font)

    def _draw_legend(self, target, x, y, bar_w):
        """A single capsule (rounded both ends) filled with a SMOOTH
        gradient through the real 6 colors (green/good -> olive/
        satisfactory -> yellow/moderate -> orange/poor -> red/veryPoor ->
        purple/severe), with the boundary numbers written in bold white
        directly on top, and "AQI"/"points" flanking it in bold white.
        CONFIRMED smooth, not hard-edged: the real legend's own SVG
        <linearGradient> has a single color stop at each offset (no two
        stops sharing an offset, which is what CSS/SVG needs to produce a
        hard edge)."""
        bar_h = self.fy(0.034)
        draw = ImageDraw.Draw(target)
        label_font = self._font_w(0.0115, bold=True)
        aqi_text = "AQI"
        aqi_w = label_font.getlength(aqi_text)
        aqi_h = _font_height(label_font)
        draw.text((x, y + (bar_h - aqi_h) // 2), aqi_text, font=label_font, fill=WHITE)

        bar_x = x + round(aqi_w) + self.fx(0.015)
        # Real stop positions (converted from the SVG's own offsets, which
        # run right-to-left, to a left-to-right 0..1 fraction of the bar).
        stops = [
            (0.11, (135, 175, 27)),   # good / green
            (0.25, (190, 198, 23)),   # satisfactory / olive
            (0.42, (255, 184, 0)),    # moderate / yellow
            (0.58, (255, 115, 68)),   # poor / orange
            (0.72, (200, 62, 73)),    # veryPoor / red
            (0.86, (171, 93, 205)),   # severe / purple
        ]
        width_px = max(1, round(bar_w))

        # One-time cost only (this whole method runs once, as part of the
        # cached static base) -- a plain per-column loop is plenty fast
        # enough here, no need to vectorize.
        gradient_row = np.zeros((width_px, 3), dtype=np.uint8)
        for px in range(width_px):
            t = px / width_px
            if t <= stops[0][0]:
                color = stops[0][1]
            elif t >= stops[-1][0]:
                color = stops[-1][1]
            else:
                for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
                    if t0 <= t <= t1:
                        frac = (t - t0) / (t1 - t0)
                        color = tuple(round(c0[k] + (c1[k] - c0[k]) * frac) for k in range(3))
                        break
            gradient_row[px] = color
        gradient_img = Image.fromarray(np.repeat(gradient_row[np.newaxis, :, :], bar_h, axis=0), "RGB")

        mask = Image.new("L", (width_px, bar_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, width_px - 1, bar_h - 1], radius=bar_h // 2, fill=255)
        gradient_rgba = gradient_img.convert("RGBA")
        gradient_rgba.putalpha(mask)
        target.paste(gradient_rgba, (bar_x, y), gradient_rgba)

        tick_font = self._font_w(0.0094, bold=True)
        tick_labels = ["<50", "100", "200", "300", ">400"]
        n = len(tick_labels) + 1
        seg_w = bar_w / n
        for i, label in enumerate(tick_labels):
            tw = tick_font.getlength(label)
            th = _font_height(tick_font)
            boundary_x = bar_x + round((i + 1) * seg_w)
            draw.text((boundary_x - int(tw) // 2, y + bar_h // 2 - th // 2), label, font=tick_font, fill=WHITE)
        pts_h = _font_height(label_font)
        draw.text((bar_x + bar_w + self.fx(0.012), y + (bar_h - pts_h) // 2), "points", font=label_font, fill=WHITE)

    def _set_screen_state(self, state):
        """Tell kiosk-console-guard whether the boot screen or the widget is up."""
        try:
            with open(SCREEN_STATE_PATH, "w") as f:
                f.write(state)
        except OSError:
            pass

    def run(self):
        self._set_screen_state("boot")
        self.show_boot_logo(frame=0)

        api_thread = threading.Thread(target=self._run_api_client, daemon=True)
        api_thread.start()

        showing_widget = False
        frame = 0
        while True:
            loop_start = time.time()

            snapshot = self.load_snapshot()
            self._maybe_sync_assets(snapshot)
            if snapshot and snapshot.get("timeseries"):
                if snapshot.get("config"):
                    self._config = snapshot["config"]
                layout = choose_layout(self.cfg, self._config, snapshot["timeseries"])
                if layout != self._layout:
                    self._layout, self._composited, self._data_key = layout, None, None
                if layout == "indoor":
                    self.draw_indoor(snapshot)
                else:
                    self.draw_widget(snapshot)
                if not showing_widget:
                    self._set_screen_state("widget")
                showing_widget = True
            elif not showing_widget:
                # Still waiting on the first successful fetch -- keep the
                # boot screen up, animating the trailing dots each redraw.
                self.show_boot_logo(frame=frame)

            frame += 1
            elapsed = time.time() - loop_start
            # Redraw about once a second (this is not a video) -- sooner if the
            # console guard just had to take the screen back from the text console.
            deadline = loop_start + 1.0
            while time.time() < deadline:
                if os.path.exists(REDRAW_REQUEST_PATH):
                    try:
                        os.remove(REDRAW_REQUEST_PATH)
                    except OSError:
                        pass
                    break
                time.sleep(0.05)

    def _maybe_sync_assets(self, snapshot):
        """Download new client assets in the background when the wanted
        sources (installer choice or the widget's settings) differ from
        what's cached; reload on this (the drawing) thread once they've
        landed. A failed download retries every ASSET_RETRY_SEC and keeps
        showing the previous assets meanwhile."""
        if self._assets_changed.is_set():
            self._assets_changed.clear()
            self._load_client_assets()
        wanted = client_assets.desired_sources((snapshot or {}).get("config"), self.cfg)
        if not wanted or not client_assets.needs_sync(wanted, self._manifest) or time.time() < self._asset_retry_at:
            return
        if self._asset_thread and self._asset_thread.is_alive():
            return
        self._asset_retry_at = time.time() + ASSET_RETRY_SEC

        def work():
            if client_assets.sync(wanted, log=api_client.api_log.info):
                self._assets_changed.set()

        self._asset_thread = threading.Thread(target=work, daemon=True)
        self._asset_thread.start()

    def _run_api_client(self):
        api_client.CONFIG_PATH = CONFIG_PATH
        api_client.CACHE_PATH = CACHE_PATH
        api_client.run(poll_config_every_sec=self.cfg.get("poll_config_every_sec", 1800))


if __name__ == "__main__":
    Renderer().run()
