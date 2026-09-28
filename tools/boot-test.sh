#!/bin/bash
# Test boot-line changes with the Raspberry Pi "tryboot" mechanism: the
# normal cmdline.txt/config.txt are never modified. `sudo reboot '0 tryboot'`
# boots ONCE using tryboot.txt, which points at cmdline-try.txt; the next
# reboot or power cycle goes back to the normal files. If a test boot hangs,
# unplug and replug the power -- that's the whole rollback.
#
#   sudo tools/boot-test.sh marker   harmless test: proves tryboot works here
#   sudo tools/boot-test.sh fbcon    the real test: kernel text off the screen
#   tools/boot-test.sh status        what the current boot used
#   sudo tools/boot-test.sh clean    remove the test files
#   sudo tools/boot-test.sh apply-fbcon  make fbcon=map:1 permanent (after tryboot passed)
#   sudo tools/boot-test.sh revert   put the original cmdline.txt back
set -euo pipefail
BOOT=/boot/firmware
BACKUP=/home/pi/boot-backup

need_root() { [ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }; }

prepare() {  # $1 = extra kernel arguments
    need_root
    mkdir -p "$BACKUP"
    for f in cmdline.txt config.txt; do
        [ -f "$BOOT/$f.orig" ] || cp -a "$BOOT/$f" "$BOOT/$f.orig"
        [ -f "$BACKUP/$f.orig" ] || cp -a "$BOOT/$f" "$BACKUP/$f.orig"
    done
    chown -R pi:pi "$BACKUP"
    # cmdline must stay ONE line
    printf '%s %s\n' "$(tr -d '\n' < "$BOOT/cmdline.txt")" "$1" > "$BOOT/cmdline-try.txt"
    { cat "$BOOT/config.txt"; printf '\n[all]\ncmdline=cmdline-try.txt\n'; } > "$BOOT/tryboot.txt"
    sync
    echo "cmdline-try.txt:"; cat "$BOOT/cmdline-try.txt"
    echo "tryboot.txt ends with:"; tail -3 "$BOOT/tryboot.txt"
    echo
    echo "Normal files unchanged:"; diff -q "$BOOT/cmdline.txt" "$BOOT/cmdline.txt.orig" && diff -q "$BOOT/config.txt" "$BOOT/config.txt.orig" && echo "  yes"
    echo
    echo "Now run:   sudo reboot '0 tryboot'"
    echo "If it doesn't come back within 3 minutes: unplug the power, wait 5s, plug it back in."
}

case "${1:-}" in
    marker) prepare "airvoice.try=1" ;;
    fbcon) prepare "fbcon=map:1 airvoice.try=1" ;;
    status)
        echo "booted at: $(uptime -s)"
        if grep -q airvoice.try=1 /proc/cmdline; then echo "this boot used the TEST line (tryboot worked)"; else echo "this boot used the NORMAL line"; fi
        grep -o 'fbcon=[^ ]*' /proc/cmdline || true
        journalctl -u kiosk-splash -b --no-pager -o cat 2>/dev/null | grep kiosk-splash || true
        ;;
    clean) need_root; rm -f "$BOOT/tryboot.txt" "$BOOT/cmdline-try.txt"; sync; echo "test files removed" ;;
    apply-fbcon)
        # Make fbcon=map:1 part of the NORMAL boot line, after it passed tryboot.
        need_root
        grep -q "fbcon=map:1" "$BOOT/cmdline.txt" && { echo "already in cmdline.txt"; exit 0; }
        mkdir -p "$BACKUP"
        [ -f "$BOOT/cmdline.txt.orig" ] || cp -a "$BOOT/cmdline.txt" "$BOOT/cmdline.txt.orig"
        [ -f "$BACKUP/cmdline.txt.orig" ] || cp -a "$BOOT/cmdline.txt" "$BACKUP/cmdline.txt.orig"
        chown -R pi:pi "$BACKUP"
        new="$(tr -d '\n' < "$BOOT/cmdline.txt") fbcon=map:1"
        printf '%s\n' "$new" > "$BOOT/cmdline.txt.new"
        [ "$(wc -l < "$BOOT/cmdline.txt.new")" -eq 1 ] || { echo "refusing: new line is not a single line"; rm -f "$BOOT/cmdline.txt.new"; exit 1; }
        mv "$BOOT/cmdline.txt.new" "$BOOT/cmdline.txt"
        rm -f "$BOOT/tryboot.txt" "$BOOT/cmdline-try.txt"
        sync
        echo "cmdline.txt now:"; cat "$BOOT/cmdline.txt"
        echo "difference from the original (should be only ' fbcon=map:1'):"
        diff <(tr -d '\n' < "$BOOT/cmdline.txt.orig"; echo) "$BOOT/cmdline.txt" || true
        echo
        echo "Undo: sudo $0 revert   (or on a PC: copy cmdline.txt.orig over cmdline.txt on the SD card)"
        ;;
    revert)
        need_root
        [ -f "$BOOT/cmdline.txt.orig" ] || { echo "no cmdline.txt.orig to restore"; exit 1; }
        cp -a "$BOOT/cmdline.txt.orig" "$BOOT/cmdline.txt"; sync
        echo "cmdline.txt restored to the original"
        ;;
    *) sed -n '2,13p' "$0"; exit 2 ;;
esac
