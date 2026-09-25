"""Indoor-only layout: the web widget's TYPE_INDOOR design, for widgets with
no outdoor station (or when the installer said "no outdoor").

  header  (top-left)    widget name, date | time, "Last update: ..." if stale
  quality (lower half)  "Air quality is" / huge grade word | divider | advice
  metrics (bottom)      one rounded bar with every reading the widget has

Background is the grade's colour field with six soft circles (static first
frame of the web widget's animation), or the client photo under 50% black
when one is set. All sizes are the web CSS values at 1920x1080, scaled by
screen width (vw) or height (vh) the same way the CSS is. Source:
docs/web-widget-reference.md.
"""
import math
import os
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from widget_rules import (GRADE_TEXT, Grading, dig, format_value, latest_value,
                          ordered_species, species_label, unit_label)

WHITE = (255, 255, 255)
UNIT_COLOR = (230, 236, 242)  # #e6ecf2

BASE_COLOR = {
    "good": [(0, "#01aa64"), (1, "#01aa64")],
    "fair": [(0, "#ffa33b"), (1, "#ffa33b")],
    "bad": [(-0.2833, "#e100ff"), (1, "#7f00ff")],
}
BASE_ANGLE = {"good": 0, "fair": 0, "bad": 70}

# (size vw, top vh, left vw, blur px, opacity, {grade: (angle, stops)}),
# listed bottom to top.
def _solid(c):
    return (0, [(0, c), (1, c)])


CIRCLES = [
    (42, -60, 0, 180, 1.0, {g: _solid("#ffd729") for g in ("good", "fair", "bad")}),       # 5
    (42, -55, -14, 180, 0.4, {"good": _solid("#61ff00"), "fair": _solid("#ffa33b"),
                              "bad": _solid("#ee4f46")}),                                  # 6
    (35, 33, 1, 180, 0.4, {"good": (254, [(-0.121, "#3dd496"), (0.9948, "#63cd9a")]),
                           "fair": _solid("#ffcc32"), "bad": _solid("#ffcc32")}),          # 4
    (42, 7, 9, 180, 1.0, "gradient23"),                                                    # 3
    (36, -16, 42, 12, 0.7, "gradient23"),                                                  # 2
    (42, 25, 30, 180, 1.0, {"good": _solid("#47bb00"), "fair": _solid("#fd744c"),
                            "bad": _solid("#ee4f46")}),                                    # 1
]
GRADIENT_23 = {
    "good": (102, [(0.1549, "#bec617"), (0.848, "#97ba1e")]),
    "fair": (247, [(0.1471, "#ffb800"), (0.9012, "#ffd729")]),
    "bad": (60, [(0.0857, "#ff5f5f"), (0.8168, "#c83fbe")]),
}
BG_SCALE = 4  # background is drawn at 1/4 size -- it's all soft blur, upscales invisibly

REST_HEADING = "Air quality over\nthe last week is"
QUALITY_HEADING = "Air quality is"


def _hex(c):
    c = c.lstrip("#")
    return np.array([int(c[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)


def css_linear_gradient(w, h, angle_deg, stops):
    """RGB float array for a CSS linear-gradient(angle, stops) over a w x h
    box: 0deg points up, angles run clockwise, stops at fractions of the
    gradient line (which may be below 0 or above 1)."""
    a = math.radians(angle_deg)
    dx, dy = math.sin(a), -math.cos(a)
    length = abs(w * dx) + abs(h * dy) or 1.0
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    t = ((xs + 0.5 - w / 2) * dx + (ys + 0.5 - h / 2) * dy) / length + 0.5
    pos = np.array([s[0] for s in stops], dtype=np.float32)
    cols = np.stack([_hex(s[1]) for s in stops])
    out = np.empty((h, w, 3), dtype=np.float32)
    for ch in range(3):
        out[:, :, ch] = np.interp(t, pos, cols[:, ch])
    return out


def indoor_background(w, h, grade):
    sw, sh = max(1, w // BG_SCALE), max(1, h // BG_SCALE)
    angle = BASE_ANGLE[grade]
    canvas = Image.fromarray(css_linear_gradient(sw, sh, angle, BASE_COLOR[grade]).astype(np.uint8)).convert("RGBA")
    vw, vh = sw / 100.0, sh / 100.0
    for size_vw, top_vh, left_vw, blur_px, opacity, colors in CIRCLES:
        angle_stops = (GRADIENT_23 if colors == "gradient23" else colors)[grade]
        d = max(2, round(size_vw * vw))
        sigma = blur_px * sw / 1920.0
        pad = int(3 * sigma) + 2
        fill = css_linear_gradient(d, d, *angle_stops).astype(np.uint8)
        layer = Image.new("RGBA", (d + 2 * pad, d + 2 * pad), (0, 0, 0, 0))
        mask = Image.new("L", (d, d), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, d - 1, d - 1], fill=255)
        circle = Image.fromarray(fill).convert("RGBA")
        circle.putalpha(mask)
        layer.paste(circle, (pad, pad), circle)
        if sigma > 0.3:
            layer = layer.filter(ImageFilter.GaussianBlur(sigma))
        if opacity < 1:
            alpha = layer.getchannel("A").point(lambda p: round(p * opacity))
            layer.putalpha(alpha)
        x, y = round(left_vw * vw) - pad, round(top_vh * vh) - pad
        canvas.alpha_composite(layer, (max(0, x), max(0, y)),
                               (max(0, -x), max(0, -y), layer.width, layer.height))
    return canvas.convert("RGB").resize((w, h), Image.BICUBIC)


class IndoorLayout:
    """Mixin for Renderer: needs self.w/h, fx/fy, _font, bg_photo, client_logo_src,
    qr_surface, powered_by, cfg."""

    def _ind_px(self, px):
        """A CSS px value at 1920 wide, scaled to this screen's width."""
        return px * self.w / 1920.0

    def _ind_py(self, px):
        return px * self.h / 1080.0

    def _text_ls(self, draw, x, line_top, text, font, line_h, spacing=0.0, fill=WHITE):
        """Draw one line the way CSS places it in a line box (glyphs centred
        by half-leading), with CSS letter-spacing. Returns the drawn width."""
        ascent, descent = font.getmetrics()
        y = line_top + (line_h - (ascent + descent)) / 2
        if not spacing:
            draw.text((x, y), text, font=font, fill=fill)
            return font.getlength(text)
        for i, ch in enumerate(text):
            draw.text((x + font.getlength(text[:i]) + spacing * i, y), ch, font=font, fill=fill)
        return font.getlength(text) + spacing * len(text)

    def _ind_geometry(self):
        px, py = self._ind_px, self._ind_py
        g = {}
        g["pad_x"] = px(72)
        g["pad_y"] = py(49.5)
        g["head_x"] = g["pad_x"] + px(47.04)
        g["content_w"] = self.w - 2 * g["pad_x"]
        g["bottom"] = self.h - g["pad_y"]
        g["bar_pad_x"], g["bar_pad_y"] = px(72), py(22.46)
        g["label_px"], g["num_px"], g["unit_px"] = px(26.88), px(57.6), px(23.04)
        g["label_lh"] = g["label_px"] * 1.4
        g["bar_h"] = 2 * g["bar_pad_y"] + g["label_lh"] + g["num_px"]
        g["bar_top"] = g["bottom"] - g["bar_h"]
        g["quality_bottom"] = g["bar_top"] - px(99.84)
        return g

    # ---------- per-grade base (background + static header/branding) ----------

    def _ind_build_base(self, grade):
        w, h = self.w, self.h
        if self.bg_photo:
            base = Image.alpha_composite(self.bg_photo.convert("RGBA"),
                                         Image.new("RGBA", (w, h), (0, 0, 0, 128)))
        else:
            base = indoor_background(w, h, grade).convert("RGBA")

        if self.client_logo_src is not None:
            logo = self.client_logo_src.copy()
            logo.thumbnail((round(self._ind_px(384)), round(self._ind_py(162))), Image.LANCZOS)
            base.alpha_composite(logo, (round(w - self._ind_px(19.2) - logo.width), round(self._ind_py(49.5))))

        disable_branding = bool(dig(self._config, ("response", "disable_branding"), False))
        if self.powered_by is not None and not disable_branding:
            pb = self.powered_by
            box_x, box_top, box_h = w - self._ind_px(76.8), h - self._ind_py(54) - self._ind_py(270), self._ind_py(270)
            base.alpha_composite(pb, (round(box_x), round(box_top + (box_h - pb.height) / 2)))
        return base.convert("RGB")

    # ---------- data layer ----------

    def _ind_metrics(self, indexes):
        return [(k, indexes[k]) for k in ordered_species(indexes)]

    def _ind_draw_data(self, frame, name, grade, indexes, rest, show_bar):
        g = self._ind_geometry()
        px, py = self._ind_px, self._ind_py
        draw = ImageDraw.Draw(frame, "RGBA")
        grading = Grading(self._config)

        # header title
        title_font = self._font(px(57.6))
        self._text_ls(draw, g["head_x"], g["pad_y"], str(name), title_font, px(57.6) * 0.99, spacing=-px(3.84))

        # --- metric bar, measured first so the photo panel can wrap it ---
        metrics = self._ind_metrics(indexes) if show_bar else []
        label_font = self._font(g["label_px"], bold=False)
        num_font = self._font(g["num_px"])
        unit_font = self._font(g["unit_px"], bold=False)
        tick_w, tick_h, gap = px(7), px(38.4), px(57.6)
        ls_label, ls_unit = -px(0.96), -px(0.841)
        items = []
        for key, block in metrics:
            label = species_label(key)
            num = format_value(latest_value(block))
            unit = unit_label(block)
            # The tick sits inside the number's 19.2px left padding; the unit
            # box has its own 19.2px padding (measured on the live page).
            row_w = px(19.2) + num_font.getlength(num) + (px(19.2) + self._ls_len(unit, unit_font, ls_unit) if unit else 0)
            items.append((key, block, label, num, unit, max(row_w, self._ls_len(label, label_font, ls_label))))
        max_right = self.w - g["pad_x"]
        if self.qr_surface is not None:
            max_right = self._ind_qr_box(g)[0] - px(38.4)
        inner_w = sum(i[5] for i in items) + gap * max(0, len(items) - 1)
        scale = 1.0
        avail = max_right - g["pad_x"] - 2 * g["bar_pad_x"]
        if items and inner_w > avail:
            scale = avail / inner_w  # more readings than fit: shrink gaps and widths evenly
        bar_x0 = g["pad_x"]
        bar_x1 = bar_x0 + 2 * g["bar_pad_x"] + inner_w * scale

        # --- quality block geometry ---
        q_bottom = g["quality_bottom"]
        heading = REST_HEADING if rest else QUALITY_HEADING
        head_px = px(96) if rest else px(76.8)
        head_lines = heading.split("\n")
        head_lh = head_px * 1.2
        status_px = px(288)
        status_lh = status_px * 0.77
        if rest:
            # weekly-average mode: heading and grade word side by side
            block_h = head_lh * len(head_lines) + py(54)
        else:
            block_h = head_lh * len(head_lines) + py(48.6) + status_lh
        q_top = q_bottom - block_h

        if self.bg_photo:
            panel = [g["pad_x"], q_top - py(54), self.w - g["pad_x"] - px(38.4), g["bottom"]]
            draw.rounded_rectangle(panel, radius=px(38.4), fill=(0, 0, 0, 89))
        elif items:
            draw.rounded_rectangle([bar_x0, g["bar_top"], bar_x1, g["bottom"]], radius=px(38.4), fill=(0, 0, 0, 77))

        # heading + grade word
        head_font = self._font(head_px)
        y = q_top
        head_w = 0
        for line in head_lines:
            head_w = max(head_w, self._text_ls(draw, g["head_x"], y, line, head_font, head_lh, spacing=-px(5.2992)))
            y += head_lh
        word, advice = GRADE_TEXT[grade]
        if rest:
            word_x, word_y = g["head_x"] + head_w + px(115.2), q_top
        else:
            word_x, word_y = g["head_x"], y + py(48.6)
        self._text_ls(draw, word_x, word_y, word, self._font(status_px), status_lh, spacing=-px(1.152))

        if not rest:
            quality_w = g["content_w"] - 2 * px(47.04)
            divider_x = g["head_x"] + quality_w * 0.52
            draw.line([(divider_x, q_top), (divider_x, q_bottom)], fill=WHITE, width=max(1, round(px(1))))
            desc_x = divider_x + px(96)
            desc_w = quality_w * 0.48 - px(96)
            desc_font = self._font(px(76.8))
            lines = self._wrap(advice, desc_font, desc_w, -px(5.2992))
            desc_lh = px(76.8) * 0.94
            y = q_top + (block_h - desc_lh * len(lines)) / 2
            for line in lines:
                self._text_ls(draw, desc_x, y, line, desc_font, desc_lh, spacing=-px(5.2992))
                y += desc_lh

        # metric items
        x = bar_x0 + g["bar_pad_x"]
        label_top = g["bar_top"] + g["bar_pad_y"]
        row_top = label_top + g["label_lh"]
        for key, block, label, num, unit, item_w in items:
            self._text_ls(draw, x, label_top, label, label_font, g["label_lh"], spacing=ls_label)
            color = grading.species_color(key, latest_value(block))
            ty = row_top + (g["num_px"] - tick_h) / 2
            draw.rounded_rectangle([x, ty, x + tick_w, ty + tick_h], radius=px(5), fill=color)
            nx = x + px(19.2)
            self._text_ls(draw, nx, row_top, num, num_font, g["num_px"])
            if unit:
                # unit line box (1.345 x font) bottom-aligned with the number's
                unit_lh = g["unit_px"] * 1.345
                self._text_ls(draw, nx + num_font.getlength(num) + px(19.2), row_top + g["num_px"] - unit_lh,
                              unit, unit_font, unit_lh, spacing=ls_unit, fill=UNIT_COLOR)
            x += (item_w + gap) * scale

        if self.qr_surface is not None:
            self._ind_draw_qr(frame, g)

    def _ind_qr_box(self, g):
        qr = self.qr_surface
        pad = max(2, round(qr.width * 0.10))
        size = qr.width + 2 * pad
        lm_h = self._ind_px(22) * 1.3
        right = self.w - self._ind_px(76.8) - self._ind_px(19.2)
        bottom = g["bottom"] - lm_h - self._ind_py(8)
        return right - size, bottom - size, size, pad

    def _ind_draw_qr(self, frame, g):
        x, y, size, pad = self._ind_qr_box(g)
        draw = ImageDraw.Draw(frame)
        draw.rounded_rectangle([x, y, x + size, y + size], radius=self._ind_px(11.5), fill=WHITE)
        frame.paste(self.qr_surface, (round(x + pad), round(y + pad)), self.qr_surface)
        font = self._font(self._ind_px(22))
        tw = font.getlength("Learn more")
        draw.text((x + size / 2 - tw / 2, y + size + self._ind_py(8)), "Learn more", font=font, fill=WHITE)

    def _ls_len(self, text, font, spacing):
        return font.getlength(text) + spacing * len(text)

    def _wrap(self, text, font, max_w, spacing=0.0):
        lines, cur = [], ""
        for word in text.split():
            trial = f"{cur} {word}".strip()
            if cur and self._ls_len(trial, font, spacing) > max_w:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        if cur:
            lines.append(cur)
        return lines

    # ---------- per-second parts ----------

    def _ind_draw_clock(self, frame, snapshot):
        g = self._ind_geometry()
        px, py = self._ind_px, self._ind_py
        draw = ImageDraw.Draw(frame)
        font = self._font(px(38.4))
        top = g["pad_y"] + px(57.6) * 0.99 + py(11.23)
        now = time.localtime()
        date_text = time.strftime("%A %B ", now) + str(now.tm_mday) + time.strftime(", %Y", now)
        if dig(self._config, ("response", "time_format")) == "HOURS_24":
            time_text = time.strftime("%H:%M", now)
        else:
            time_text = time.strftime("%I:%M %p", now).lstrip("0")
        x = g["head_x"]
        w_date = self._text_ls(draw, x, top, date_text, font, px(38.4))
        bar_x = x + w_date + px(8)
        draw.rectangle([bar_x, top, bar_x + max(1, round(px(3))) - 1, top + px(38.4)], fill=WHITE)
        self._text_ls(draw, bar_x + px(3) + px(8), top, time_text, font, px(38.4))

        last_good = (snapshot or {}).get("last_good_at")
        if last_good:
            age = time.time() - last_good
            msg = None
            if age > 3600:
                msg = "Last update: over 1 hour ago"
            elif age > 15 * 60:
                msg = "Last update: over 15 minutes ago"
            if msg:
                draw.text((x, top + px(38.4) + px(13)), msg, font=self._font(px(34.56), bold=False), fill=WHITE)

    # ---------- entry point ----------

    def draw_indoor(self, snapshot):
        timeseries = snapshot.get("timeseries") or {}
        live = dig(timeseries, ("response", "data", "indexes"), {}) or {}
        all_null = not live or all(latest_value(b) is None for b in live.values())
        rest = bool(dig(timeseries, ("meta", "extra", "is_rest_time"), False)) or all_null
        if rest:
            indexes = dig(timeseries, ("meta", "extra", "weekly_averages", "indoor", "indexes"), {}) or {}
        else:
            indexes = live
        rooms = dig(self._config, ("response", "rooms"))
        show_bar = bool(indexes) and (rooms is None or bool(rooms))
        grading = Grading(self._config)
        grade = grading.overall(indexes) if indexes else "good"
        name = dig(timeseries, ("response", "name"), self.cfg.get("device_label", "Kiosk"))

        if self._ind_base_key != grade:
            self._ind_base = self._ind_build_base(grade)
            self._ind_base_key = grade
            self._composited = None
        key = ("indoor", grade, name, rest, show_bar,
               tuple((k, format_value(latest_value(b))) for k, b in sorted(indexes.items())))
        if key != self._data_key or self._composited is None:
            self._composited = self._ind_base.copy()
            self._ind_draw_data(self._composited, name, grade, indexes, rest, show_bar)
            self._data_key = key
        frame = self._composited.copy()
        self._ind_draw_clock(frame, snapshot)
        self.fb.show(frame)
