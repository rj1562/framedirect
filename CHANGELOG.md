# Changelog

## 0.2.0 — 2026-09-28 (testing, not for release)

The whole boot is now hidden: black, then the boot image, then the widget.

### Boot
- `fbcon=map:1` is now part of the normal boot line. It passed three
  `tryboot` test boots, then a normal boot on the bench (no kernel text on
  screen, all services up, networks up, no throttling).
- The installer adds it: one word at the end of `cmdline.txt`, only on a
  stock one-line boot line with `console=tty1`, original kept as
  `cmdline.txt.orig` (also in `~/boot-backup/`). The self-test checks it.
- The old, disabled quiet-boot code (removed `console=tty1`, masked
  getty; caused hangs) is deleted from the installer.
- `tools/boot-test.sh apply-fbcon` / `revert` to add or undo it by hand.
- Faster repaint after the display-driver switch at ~15 s: black blink down
  from ~0.3 s to ~0.2 s.
- The console guard asks the renderer to redraw immediately after catching
  a console reset, instead of waiting up to a second.

### Tools
- `tools/measure-usage.sh`: read-only CPU / memory (incl. swap) /
  temperature / throttling snapshot, identical for any kiosk version, used
  to compare against the Chromium kiosk (v8.2: ~277 MB for the display
  stack, ~211 MB of it in swap; FrameDirect ~95 MB).
- `tools/make-demo-images.py`: documentation screenshots from made-up data.

### Docs
- `docs/ARCHITECTURE.md` with diagrams; README with purpose, capabilities
  and a screenshot gallery; all company details removed.

## 0.1.0 — 2026-09-25 (testing, not for release)

First version under version control. Bench-tested on one Raspberry Pi
Zero 2 W (Raspberry Pi OS Trixie Lite 32-bit, kernel 6.18) at 1920×1080.

### Rendering
- Pillow drawing written straight to `/dev/fb0` (no browser, no SDL2: SDL2's
  KMSDRM output stopped showing anything on a fresh Trixie image).
- Indoor + outdoor comparison layout, rebuilt from the web widget's code.
- New indoor-only layout (`TYPE_INDOOR`), matched to the live web page by
  measuring its elements: grade word and advice, every reading the widget
  has (incl. CO₂ and Total VOC), grade-coloured background with soft
  circles, or the client photo.
- Grades and colours read from each widget's own settings (`aq_settings`,
  `is_aqi_500`) instead of fixed values.
- Weekly-average mode when the sensors send empty readings or outside
  working hours.
- "Last update: over 15 minutes ago / over 1 hour ago" when data is stale.

### Branding
- Background, logo and QR code chosen in the installer: the widget's own,
  another link or file, or none. Links are checked on the spot.
- Cached on the device; images that are no longer configured are removed
  (previously a previous client's branding could stay on screen).
- SVG logos converted with `rsvg-convert`.

### Boot
- `kiosk-splash.service`: client boot image from ~11 s, no image libraries.
- `kiosk-console-guard.service`: stops the login prompt flashing over the
  boot screen when getty resets the console (~45 s).
- `fbcon=map:1` tested through `tryboot` (kernel text gone from 2–11 s);
  not yet in the normal boot line.
- `tools/boot-test.sh` for safe `tryboot` testing, with backups.

### Data
- Polling every minute plus just after each expected update; retries after
  15/30/60 s; 20 s timeout (was 8 s, ~2% of calls timed out).
- Health log records value changes and gaps.

### Installer
- New questions: branding (3), outdoor section.
- Falls back to `archive.raspbian.org` when the country mirror is down.
- `tools/update-renderer.sh` to update code without reinstalling.
