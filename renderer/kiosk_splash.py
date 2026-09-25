#!/usr/bin/env python3
"""Early-boot splash: hide the text console and put the boot screen up.

Puts tty1 into KD_GRAPHICS mode -- the same switch X servers and Plymouth
use -- so the kernel's text console stops drawing boot messages, the login
prompt, and its blinking cursor onto /dev/fb0; boot carries on normally
behind the image. Nothing on the boot partition is touched: earlier
attempts that edited cmdline.txt and masked getty@tty1 left the device
unbootable on three separate reboots, while the stock boot config rebooted
cleanly every time. The mode lives only in the running kernel, so a reboot
without this service is plain stock boot.

Speed is the point, so this imports nothing heavy: loading PIL + numpy costs
~4s on a Pi Zero 2W. The renderer pre-renders the boot screen as raw
framebuffer bytes (boot-<w>x<h>-<bpp>.raw, see kiosk_renderer._write_boot_raw)
and this just copies the matching file onto the screen. It's done twice,
because on the bench the firmware's 32bpp "simple" framebuffer appears ~12s
into boot and the real vc4 driver replaces it ~3s later, discarding what
was drawn on it.

Runs once per boot as root (/dev/tty1 is root-only), ordered before the
renderer, which then takes over the same framebuffer.
"""
import fcntl
import os
import sys
import time

KDSETMODE = 0x4B3A
KDGETMODE = 0x4B3B
KD_GRAPHICS = 0x01
# The renderer writes "boot" or "widget" here so the guard knows whether
# repainting the boot screen is right (it isn't once the widget is up).
STATE_FILE = os.path.join(os.environ.get("KIOSK_BOOT_RAW_DIR", "/var/lib/kiosk-renderer"), "screen-state")
REDRAW_FILE = os.path.join(os.path.dirname(STATE_FILE), "redraw-request")
GUARD_POLL_SEC = 0.05
FB_SYS = "/sys/class/graphics/fb0"
RAW_DIR = os.environ.get("KIOSK_BOOT_RAW_DIR", "/var/lib/kiosk-renderer")
FB_WAIT_SEC = 30
REAL_DRIVER_WAIT_SEC = 25
REPAINTS_AFTER_SWAP = 3  # a few extra copies in case the new driver clears the screen as it settles


def uptime():
    with open("/proc/uptime") as f:
        return f.read().split()[0]


def wait_for(predicate, timeout):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def read_sys(name):
    try:
        with open(os.path.join(FB_SYS, name)) as f:
            return f.read().strip()
    except OSError:
        return ""


def hide_console():
    try:
        with open("/sys/class/graphics/fbcon/cursor_blink", "w") as f:
            f.write("0")
    except OSError:
        pass
    fd = os.open("/dev/tty1", os.O_RDWR | os.O_NOCTTY)
    try:
        fcntl.ioctl(fd, KDSETMODE, KD_GRAPHICS)
    finally:
        os.close(fd)


def paint_raw():
    """Copy the pre-rendered boot screen matching fb0's current geometry.
    Returns False if there's no matching frame (e.g. first boot before the
    renderer has ever run) -- the console is still hidden, just no image."""
    try:
        w, h = (int(v) for v in read_sys("virtual_size").split(","))
        bpp, stride = int(read_sys("bits_per_pixel")), int(read_sys("stride"))
    except ValueError:
        return False
    if stride != w * bpp // 8:
        return False
    try:
        with open(os.path.join(RAW_DIR, f"boot-{w}x{h}-{bpp}.raw"), "rb") as f:
            data = f.read()
        if len(data) != stride * h:
            return False
        with open("/dev/fb0", "r+b", buffering=0) as fb:
            fb.write(data)
        return True
    except OSError:
        return False


def main():
    if not wait_for(lambda: os.path.exists("/dev/fb0"), FB_WAIT_SEC):
        print("kiosk-splash: /dev/fb0 never appeared, nothing to do", file=sys.stderr)
        return 0
    hide_console()
    print(f"kiosk-splash: console hidden at {uptime()}s on '{read_sys('name')}'")

    painted_on = None
    deadline = time.monotonic() + REAL_DRIVER_WAIT_SEC
    while time.monotonic() < deadline:
        name = read_sys("name")
        if name and name != painted_on and paint_raw():
            painted_on = name
            print(f"kiosk-splash: boot screen painted at {uptime()}s on '{name}'")
        if painted_on and not painted_on.startswith("simple"):
            break
        time.sleep(0.01)  # catch the vc4 driver swap fast: the screen is black until we repaint
    if not painted_on or painted_on.startswith("simple"):
        print(f"kiosk-splash: never painted on the real driver (fb0 is '{read_sys('name')}')", file=sys.stderr)
        return 0
    for _ in range(REPAINTS_AFTER_SWAP):
        time.sleep(0.5)
        paint_raw()
    return 0


def console_mode():
    fd = os.open("/dev/tty1", os.O_RDWR | os.O_NOCTTY)
    try:
        buf = bytearray(4)
        fcntl.ioctl(fd, KDGETMODE, buf)
        return int.from_bytes(buf, sys.byteorder)
    finally:
        os.close(fd)


def request_redraw():
    """Ask the renderer to redraw now rather than on its next 1s tick."""
    try:
        open(REDRAW_FILE, "w").close()
    except OSError:
        pass


def widget_showing():
    try:
        with open(STATE_FILE) as f:
            return f.read().strip() == "widget"
    except OSError:
        return False


def guard():
    """Keep tty1 in graphics mode for good. getty@tty1 starts ~40s into boot
    with TTYReset=yes, which puts the console back into text mode and
    flashes the login prompt over the boot screen; anything else that resets
    the VT would do the same. Undo it within GUARD_POLL_SEC and repaint the
    boot screen if the widget isn't up yet (once it is, the renderer redraws
    within a second anyway)."""
    while True:
        try:
            if console_mode() != KD_GRAPHICS:
                hide_console()
                repainted = not widget_showing() and paint_raw()
                request_redraw()
                print(f"kiosk-splash: console was reset to text mode at {uptime()}s -- hidden again"
                      f"{', boot screen repainted' if repainted else ''}", flush=True)
        except OSError:
            pass
        time.sleep(GUARD_POLL_SEC)


if __name__ == "__main__":
    if sys.argv[1:] == ["--guard"]:
        guard()
    sys.exit(main())
