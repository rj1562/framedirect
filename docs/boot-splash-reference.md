# Early boot splash -- research (2026-09-25)

Target: Pi Zero 2 W, Raspberry Pi OS Trixie armhf Lite, kernel 6.18 rpi-v7, vc4-kms-v3d.
Nothing below is verified on the Zero 2 W yet.

## Facts
- No stock way to show an image over HDMI before Linux starts. `disable_splash=1`
  only removes the rainbow; the new bootloader splash is beta, SPI/I2C screens,
  Pi 4/5 only.
- The kernel can only draw a logo on the firmware "simple" framebuffer: logo data
  is freed at the end of kernel init and vc4 loads later as a module, so any
  kernel/official splash is wiped at the vc4 takeover (~15.5 s). Our repaint
  after vc4 (kiosk-splash.service) is needed whatever else is done.
- `quiet`, `loglevel=3` and `logo.nologo` all disable the kernel logo (and so the
  official splash).
- The ~12.3 s "fb0 appears" figure may be wall-clock rather than kernel time --
  check `dmesg | grep -iE "simple|fb0|fbcon|vc4"` and `systemd-analyze`.

## Options, ranked
1. **`fbcon=map:1` + existing painter (lowest risk).** One token added to
   cmdline.txt, `console=tty1` kept. The text console never attaches to the HDMI
   framebuffer: no kernel text, raspberries or cursor -> screen stays black until
   kiosk-splash paints. Console output still reachable over SSH/serial; emergency
   shell would be invisible on screen.
2. **Official splash** (`rpi-splash-screen-support`, `fullscreen_logo=1
   fullscreen_logo_name=logo.tga`): 24-bit uncompressed TGA, no alpha, <= 1920x1080,
   <= 224 colours, must be in the initramfs (Trixie has `auto_initramfs=1`).
   Appears where the raspberries appear now. `configure-splash` removes
   `console=tty1` and `quiet` unless `--no-cmdline` is used. A missing/bad TGA can
   read an uninitialised pointer (fits open issue raspberrypi/linux#7086: silent
   boot failure). Only via tryboot, only after option 1 is stable.
3. Not recommended: Plymouth (v9.1 bench: 50.2 s boot vs 33-38 s), `auto_initramfs=0`,
   masking getty, removing `console=tty1` (sends emergency/fsck prompts to serial
   only, so a failed boot looks like a hang -- plausible cause of the earlier 3 hangs).

Cosmetic, safe: `vt.global_cursor_default=0`, `consoleblank=0`.

## Rollback-safe testing: tryboot
- `sudo reboot '0 tryboot'` boots once with `tryboot.txt` instead of `config.txt`;
  a power cycle returns to normal. `tryboot.txt` = full copy of config.txt +
  `cmdline=cmdline-try.txt`. Docs: "All Raspberry Pi models support tryboot", but
  one 2026 report says it does nothing on a Zero 2 W -> prove it with a harmless
  marker first.
- Fallbacks: GPIO jumper filter (`[gpio5=0]` + `cmdline=cmdline-try.txt`, untested),
  or SD card in a PC and restore `cmdline.txt.orig`.
- For diagnosing a hang: persistent journal (`journalctl -b -1`), USB-serial on GPIO14/15.

## Test plan (bench device only, one change per step)
0. Backups: `cmdline.txt` and `config.txt` -> `.orig` on the Pi and on the PC;
   `wc -l cmdline.txt` must be 1. Record `dmesg`, `systemd-analyze`, a snapshot;
   enable persistent journal.
1. Prove tryboot: `cmdline-try.txt` = original + ` airvoice.try=1`; reboot with
   tryboot; `grep airvoice.try /proc/cmdline`; normal reboot -> marker gone.
2. Via tryboot: `cmdline-try.txt` = original + ` fbcon=map:1`. Check screen, SSH on
   eth + WiFi, painter log. Unplug/replug = rollback. 3 boots incl. 1 cold.
3. Permanent: add the token to cmdline.txt; `diff` vs `.orig` = exactly one token;
   3 warm + 3 cold boots, `uptime -s` each time.
4. Optional, tryboot only: official splash (`configure-splash --no-cmdline logo.tga`,
   check `lsinitramfs /boot/firmware/initramfs7 | grep logo.tga`).

## Boot-time levers (listed, not recommended)
Confirm `boot_delay` is 0 (`vcgencmd get_config boot_delay`); `camera_auto_detect=0`
/ `display_auto_detect=0` if unused.

Sources: raspberrypi.com/documentation (configuration, config_txt), raspberrypi/documentation
bootflow-eeprom.adoc + conditional.adoc, github.com/raspberrypi/rpi-splash-screen-support,
raspberrypi/linux rpi-6.18.y fb_logo.c / fbcon.c / logo.c / bcm2709_defconfig, issues
7081/7086/7087/7147, kernel.org/doc/html/latest/fb/fbcon.html.
