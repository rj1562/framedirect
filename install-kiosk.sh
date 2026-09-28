#!/bin/bash
###############################################################################
# Airvoice Native Kiosk installer (v0.2.0, testing)
#
# Provisions a Raspberry Pi (already on Ethernet and/or WiFi, already running
# Raspberry Pi OS Trixie) as an unattended kiosk showing the AQI widget --
# WITHOUT Chromium, labwc, or XWayland. A small Python program renders the
# widget directly to the Linux framebuffer (/dev/fb0), polling the backend JSON
# API instead of loading a web page. See README.md for the full architecture
# writeup, tradeoffs, and the six log streams this installs.
#
# WHAT THIS ASKS YOU:
#   1. Widget ID and secret key (from the myindoor.global widget URL)
#   2. A label for this device (defaults to the hostname)
#   3. Background image, client logo, QR code: shown or not, and a link (or a
#      file on the Pi) for each -- defaults to what the widget settings have
#   4. Whether the display shows an outdoor section (indoor-only otherwise)
#
# WHAT THIS ASSUMES IS ALREADY DONE (out of scope for this script):
#   - Raspberry Pi OS Trixie is flashed and booted
#   - The device already has a working network connection -- Ethernet, WiFi,
#     or both. Unlike versions/v9, this installer does NOT assume "the"
#     network is WiFi and does not do any WiFi-specific setup at install
#     time -- net-watchdog discovers and monitors whatever's actually
#     connected at RUNTIME, every cycle, so both Ethernet-only and
#     Ethernet+WiFi devices are supported without different install paths.
#
# OFFLINE: if offline-packages/armhf-trixie/ (with its Packages.gz index) is
# present next to this script, step 2 installs every package from that local
# bundle and never touches the network -- apt's online sources are moved
# aside for the duration of the step and restored afterward regardless of
# outcome. The bundle is the full recursive dependency closure of the
# PACKAGES list below (257 .debs, ~230MB), downloaded once on a device with
# internet via offline-packages/build-offline-bundle.sh, not generated here.
# Without the bundle present, this falls back to plain `apt-get install`
# (network required at install time only, not at runtime) so a lighter,
# bundle-free zip still works on a device that has internet.
#
# Safe to re-run: existing config/services are stopped and replaced cleanly.
###############################################################################

set -uo pipefail

# Same fix v9 already needed for the identical reason (see its own header):
# a non-interactive invocation (e.g. over SSH without a login shell) gets
# PATH=/usr/local/bin:/usr/bin:/bin:/usr/games -- missing /sbin entirely,
# where iw/rfkill actually live. Confirmed again on the bench here: without
# this, require_bin fails on 'iw' even though the package installed fine.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

INSTALL_LOG="/tmp/architect2-install.log"
INSTALL_DATE=$(date '+%Y-%m-%d %H:%M:%S')
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

c_green() { printf '\033[0;32m%s\033[0m\n' "$1"; }
c_red()   { printf '\033[0;31m%s\033[0m\n' "$1"; }
c_yellow(){ printf '\033[0;33m%s\033[0m\n' "$1"; }
c_blue()  { printf '\033[0;34m%s\033[0m\n' "$1"; }

# $INSTALL_LOG lives in /tmp (world-writable) and is only ever this script's
# own diagnostic log for the 'pi' user -- no step needs it root-owned. Real
# bug found on the bench: an earlier draft wrote here via `sudo tee -a`
# (creating it root-owned on first write) while OTHER steps appended with
# plain `>> "$INSTALL_LOG"` as plain 'pi' -- which then failed with
# "Permission denied" against a root-owned file. Plain, unprivileged
# appends everywhere fixes it at the root instead of special-casing one path.
log_install() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$INSTALL_LOG"; }
step()  { echo ""; c_blue "==> $1"; log_install "STEP: $1"; }
fatal() { c_red "FATAL: $1"; log_install "FATAL: $1"; exit 1; }
warn()  { c_yellow "WARNING: $1"; log_install "WARNING: $1"; }
ok()    { c_green "  OK: $1"; log_install "OK: $1"; }

backup_if_exists() {
    local f="$1" bak
    if [ -e "$f" ]; then
        bak="${f}.bak-$(date +%Y%m%d-%H%M%S)"
        sudo cp -a "$f" "$bak"
        log_install "Backed up $f -> $bak"
    fi
}

# Modern apt sandboxes its download methods -- including the local `file://`
# one this offline install path uses -- to run as the unprivileged `_apt`
# user, even though apt-get itself runs under sudo/root. Confirmed live on a
# fresh device: `sudo apt-get update` against the bundled offline repo
# failed with a raw "Permission denied" on a temp file under
# /var/lib/apt/lists/partial, traced to /home/pi being Raspberry Pi OS's
# default 700 (drwx------) -- which blocks `_apt` from even traversing into
# it to reach the bundle, despite every file and subdirectory inside it
# already being world-readable. This grants only the minimum fix (the 'x'
# search bit, not read/write) on whichever ancestor directories actually
# need it, and records original modes so they can be restored afterward --
# it does not permanently loosen the user's home directory permissions.
ensure_path_traversable() {
    local path modes_file="$1" dir mode
    path=$(cd "$2" && pwd) || return 1
    : > "$modes_file"
    dir="$path"
    while [ "$dir" != "/" ] && [ -n "$dir" ]; do
        mode=$(stat -c '%a' "$dir" 2>/dev/null) || break
        case "$mode" in
            *[1357]) : ;;  # 'other' execute bit already set
            *)
                echo "$dir $mode" >> "$modes_file"
                sudo chmod o+x "$dir"
                ;;
        esac
        dir=$(dirname "$dir")
    done
}
restore_path_modes() {
    local modes_file="$1" dir mode
    [ -f "$modes_file" ] || return 0
    while read -r dir mode; do
        [ -n "$dir" ] && sudo chmod "$mode" "$dir"
    done < "$modes_file"
    rm -f "$modes_file"
}

resolve_bin() { command -v "$1" 2>/dev/null; }
require_bin() {
    local __varname="$1" __binname="$2" __resolved
    __resolved=$(resolve_bin "$__binname")
    [ -n "$__resolved" ] || fatal "Required command '$__binname' not found on this system (needed for sudoers setup)."
    printf -v "$__varname" '%s' "$__resolved"
}

TOTAL_STEPS=11
CHECKS_PASSED=0
CHECKS_FAILED=0
check() {
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then
        ok "$desc"; CHECKS_PASSED=$((CHECKS_PASSED + 1))
    else
        c_red "  FAIL: $desc"; CHECKS_FAILED=$((CHECKS_FAILED + 1))
    fi
}

[ "$(id -u)" -ne 0 ] || fatal "Run this as the 'pi' user, not root (it calls sudo itself where needed)."
command -v sudo >/dev/null || fatal "sudo not found."

step "1/$TOTAL_STEPS — Collecting widget details"
read -rp "Widget ID (from the widget URL, e.g. widget_abcdefgh): " WIDGET_ID
read -rp "Secret key (from the widget URL): " SECRET_KEY
DEFAULT_LABEL=$(hostname)
read -rp "Label for this device [$DEFAULT_LABEL]: " DEVICE_LABEL
DEVICE_LABEL="${DEVICE_LABEL:-$DEFAULT_LABEL}"
[ -n "$WIDGET_ID" ] && [ -n "$SECRET_KEY" ] || fatal "Widget ID and secret key are both required."
DASHBOARD_HOST="myindoor.global"
ok "Widget: $WIDGET_ID, device label: $DEVICE_LABEL"

# What the widget itself has set up (name, outdoor station, branding links),
# offered as the default answer to each question below.
command -v python3 >/dev/null || fatal "python3 not found (it ships with Raspberry Pi OS)."
W_OK=no; W_NAME=""; W_LAYOUT=""; W_OUTDOOR=no; W_BG=""; W_LOGO=""; W_QR=""; W_ERROR=""
eval "$(python3 "$SCRIPT_DIR/renderer/setup_helper.py" widget-info "https://$DASHBOARD_HOST" "$WIDGET_ID" "$SECRET_KEY")"
if [ "$W_OK" = yes ]; then
    ok "Found widget \"$W_NAME\" ($W_LAYOUT)"
else
    warn "Could not read this widget's settings (${W_ERROR:-no response}) -- check the ID and key. Continuing; you'll be asked everything."
fi

# ask_asset <var prefix> <what> <link from the widget settings>
# Sets <prefix>_MODE (url/none) and <prefix>_URL. A link is downloaded and
# checked right away, so a typo is caught here rather than on the screen.
ask_asset() {
    local prefix="$1" what="$2" current="$3" answer link result default_yn="N"
    echo ""
    if [ -n "$current" ]; then
        echo "  The widget settings have a $what: $current"
        default_yn="Y"
    fi
    while true; do
        read -rp "Show a $what on this display? [$( [ "$default_yn" = Y ] && echo "Y/n" || echo "y/N" )]: " answer
        answer="${answer:-$default_yn}"
        case "$answer" in
            [Nn]*) printf -v "${prefix}_MODE" '%s' none; printf -v "${prefix}_URL" '%s' ""; ok "$what: none"; return ;;
            [Yy]*) break ;;
        esac
    done
    while true; do
        if [ -n "$current" ]; then
            read -rp "  Link to the $what (Enter = use the widget's): " link
            link="${link:-$current}"
        else
            read -rp "  Link to the $what (https://... or a file path on this Pi): " link
        fi
        [ -n "$link" ] || continue
        result=$(python3 "$SCRIPT_DIR/renderer/setup_helper.py" probe "$link")
        if [ "${result%% *}" = OK ]; then
            if [ "$link" = "$current" ]; then
                # the widget's own image: keep following the dashboard if it changes
                printf -v "${prefix}_MODE" '%s' widget
                printf -v "${prefix}_URL" '%s' ""
            else
                printf -v "${prefix}_MODE" '%s' url
                printf -v "${prefix}_URL" '%s' "$link"
            fi
            ok "$what: ${result#OK } -- $link"
            return
        fi
        c_red "  ${result#FAIL }"
        read -rp "  Try another link? [Y/n] (n = no $what): " answer
        case "$answer" in
            [Nn]*) printf -v "${prefix}_MODE" '%s' none; printf -v "${prefix}_URL" '%s' ""; ok "$what: none"; return ;;
        esac
    done
}
ask_asset BG "background image" "$W_BG"
ask_asset LOGO "client logo" "$W_LOGO"
ask_asset QR "QR code" "$W_QR"

echo ""
if [ "$W_OUTDOOR" = yes ]; then
    read -rp "Does this display show an outdoor section? (the widget has an outdoor station) [Y/n]: " OUTDOOR_INPUT
    OUTDOOR_INPUT="${OUTDOOR_INPUT:-Y}"
else
    read -rp "Does this display show an outdoor section? (the widget has no outdoor station) [y/N]: " OUTDOOR_INPUT
    OUTDOOR_INPUT="${OUTDOOR_INPUT:-N}"
fi
case "$OUTDOOR_INPUT" in
    [Yy]*) OUTDOOR=widget; ok "Layout: indoor + outdoor"
        [ "$W_OUTDOOR" = yes ] || warn "This widget has no outdoor station, so the outdoor panel stays empty until another outdoor source (e.g. CPCB) is set up." ;;
    *) OUTDOOR=none; ok "Layout: indoor only" ;;
esac

# HDMI-CEC watchdog (auto-wake the TV, keep it on the correct input) only
# makes sense on a CEC-capable display wired via HDMI -- not every kiosk
# screen is one, and not every device should carry the extra sudoers rules,
# service, and timer for it. Previously this was unconditionally installed
# and enabled everywhere, relying on cec-watchdog.sh's own SKIP logic to
# quietly no-op on non-CEC hardware -- harmless, but not actually optional
# at install time the way it should be.
read -rp "Enable the HDMI-CEC display watchdog on this device? [y/N]: " ENABLE_CEC_INPUT
case "$ENABLE_CEC_INPUT" in
    [Yy]*) ENABLE_CEC=true ;;
    *) ENABLE_CEC=false ;;
esac
ok "HDMI-CEC watchdog: $([ "$ENABLE_CEC" = true ] && echo "enabled" || echo "disabled for this device")"

step "2/$TOTAL_STEPS — Installing packages"
# python3-pygame/libsdl2-2.0-0/libegl1/libgles2 (the old SDL2/KMSDRM/EGL
# stack) are GONE from this list -- confirmed on a fresh Trixie image
# (2026-09-23) that SDL2's kmsdrm backend no longer produces any visible
# output at all on this hardware (a from-scratch, zero-app-logic pygame
# script reproduced the identical failure, ruling out this project's own
# code -- see kiosk_renderer.py's module docstring for the full
# investigation). The renderer now draws with Pillow and writes straight
# to /dev/fb0, which needs python3-pil and python3-numpy (fast RGB->RGB565
# packing) instead.
# iw/rfkill/usbutils are used by net-watchdog.sh and wifi-scan-watchdog.sh
# (iw scan/link, rfkill list, lsusb in incident bundles) and by the sudoers
# step's require_bin checks -- real bug found on the bench: left out of this
# list even though v9 carried them, and the install got as far as step 8
# before failing on "'iw' not found" during sudoers generation.
PACKAGES=(python3 python3-pip python3-pil python3-numpy librsvg2-bin fonts-dejavu-core network-manager v4l-utils iw rfkill usbutils)

OFFLINE_PKG_DIR="$SCRIPT_DIR/offline-packages/armhf-trixie"
if [ -f "$OFFLINE_PKG_DIR/Packages.gz" ]; then
    ok "Offline package bundle found at $OFFLINE_PKG_DIR -- installing with no network access"

    # Move every existing apt source out of the way for the duration of this
    # step (restored unconditionally via the trap below, even on failure),
    # so `apt-get update`/`install` here can ONLY see the bundled local
    # repo and genuinely cannot reach the network -- not just "won't need
    # to" but structurally can't.
    APT_SOURCES_BAK=$(mktemp -d)
    mkdir -p "$APT_SOURCES_BAK/sources.list.d"
    [ -f /etc/apt/sources.list ] && sudo mv /etc/apt/sources.list "$APT_SOURCES_BAK/sources.list"
    if [ -d /etc/apt/sources.list.d ] && [ -n "$(ls -A /etc/apt/sources.list.d 2>/dev/null)" ]; then
        sudo mv /etc/apt/sources.list.d/* "$APT_SOURCES_BAK/sources.list.d/"
    fi
    PERM_RESTORE_FILE=$(mktemp)
    restore_apt_sources() {
        sudo rm -f /etc/apt/sources.list.d/architect2-offline.list
        [ -f "$APT_SOURCES_BAK/sources.list" ] && sudo mv "$APT_SOURCES_BAK/sources.list" /etc/apt/sources.list
        if [ -n "$(ls -A "$APT_SOURCES_BAK/sources.list.d" 2>/dev/null)" ]; then
            sudo mv "$APT_SOURCES_BAK/sources.list.d/"* /etc/apt/sources.list.d/
        fi
        rm -rf "$APT_SOURCES_BAK"
        restore_path_modes "$PERM_RESTORE_FILE"
    }
    trap restore_apt_sources EXIT

    ensure_path_traversable "$PERM_RESTORE_FILE" "$OFFLINE_PKG_DIR"

    echo "deb [trusted=yes] file://$OFFLINE_PKG_DIR ./" | sudo tee /etc/apt/sources.list.d/architect2-offline.list > /dev/null
    sudo apt-get update >> "$INSTALL_LOG" 2>&1 || fatal "Failed to read the bundled offline package repo -- see $INSTALL_LOG"

    for pkg in "${PACKAGES[@]}"; do
        if dpkg -s "$pkg" >/dev/null 2>&1; then
            ok "$pkg already installed"
        else
            sudo apt-get install -y "$pkg" >> "$INSTALL_LOG" 2>&1 && ok "installed $pkg (offline)" || fatal "Failed to install $pkg from the offline bundle -- see $INSTALL_LOG"
        fi
    done

    restore_apt_sources
    trap - EXIT
else
    warn "No offline package bundle at $OFFLINE_PKG_DIR -- falling back to apt-get over the network"
    sudo apt-get update -y >> "$INSTALL_LOG" 2>&1 || warn "apt-get update failed -- continuing with cached package lists"

    # raspbian.raspberrypi.com is only a redirector: it hands each download
    # to a "nearby" mirror, and when that mirror is down every install fails
    # (seen on the bench: raspbian.mirror.net.in unreachable for all of
    # India). On failure, retry against archive.raspbian.org directly, which
    # serves the same signed repo itself. Uses a throwaway source list and
    # package-list cache, so the device's own apt config is never modified.
    MIRROR_FALLBACK_DIR=""
    apt_fallback() {
        if [ -z "$MIRROR_FALLBACK_DIR" ]; then
            MIRROR_FALLBACK_DIR="$(mktemp -d)"
            mkdir -p "$MIRROR_FALLBACK_DIR/parts" "$MIRROR_FALLBACK_DIR/lists/partial"
            sed 's#http://raspbian.raspberrypi.com/raspbian/#http://archive.raspbian.org/raspbian/#' \
                /etc/apt/sources.list.d/raspbian.sources > "$MIRROR_FALLBACK_DIR/parts/raspbian.sources"
            [ -f /etc/apt/sources.list.d/raspi.sources ] && cp /etc/apt/sources.list.d/raspi.sources "$MIRROR_FALLBACK_DIR/parts/"
            sudo apt-get -o Dir::Etc::SourceList=/dev/null -o Dir::Etc::SourceParts="$MIRROR_FALLBACK_DIR/parts" \
                -o Dir::State::Lists="$MIRROR_FALLBACK_DIR/lists" update >> "$INSTALL_LOG" 2>&1 || return 1
        fi
        sudo apt-get -o Dir::Etc::SourceList=/dev/null -o Dir::Etc::SourceParts="$MIRROR_FALLBACK_DIR/parts" \
            -o Dir::State::Lists="$MIRROR_FALLBACK_DIR/lists" install -y "$1" >> "$INSTALL_LOG" 2>&1
    }

    for pkg in "${PACKAGES[@]}"; do
        if dpkg -s "$pkg" >/dev/null 2>&1; then
            ok "$pkg already installed"
        elif sudo apt-get install -y "$pkg" >> "$INSTALL_LOG" 2>&1; then
            ok "installed $pkg"
        else
            warn "$pkg: default mirror failed, retrying via archive.raspbian.org"
            apt_fallback "$pkg" && ok "installed $pkg (archive.raspbian.org)" || fatal "Failed to install $pkg -- see $INSTALL_LOG"
        fi
    done
    if [ -n "$MIRROR_FALLBACK_DIR" ]; then sudo rm -rf "$MIRROR_FALLBACK_DIR"; fi
fi

step "3/$TOTAL_STEPS — Ensuring pi has framebuffer access"
# Only "video" is needed now -- /dev/fb0 is group-owned by "video"
# (crw-rw---- root:video). "render" and "input" were only ever needed for
# the old DRM/GBM (SDL2/KMSDRM) rendering path, which this build no longer
# uses at all (see kiosk_renderer.py's module docstring).
for grp in video; do
    if getent group "$grp" >/dev/null 2>&1; then
        sudo usermod -aG "$grp" pi
        ok "pi is in group '$grp'"
    else
        warn "group '$grp' does not exist on this system -- skipping (kernel/board dependent)"
    fi
done

step "4/$TOTAL_STEPS — Boot screen and clock"
# Kill the firmware rainbow splash (the very first thing shown on power-on,
# before Linux even loads) -- documented, one-line fix. It becomes a plain
# black screen for that brief pre-kernel moment instead; the renderer's own
# white/logo boot screen (kiosk-renderer.service) takes over from there as
# soon as systemd + DRM are ready, which is now the very next thing the
# display shows -- there's no labwc/XWayland/Chromium layer to wait on
# in this architecture, so this is a much shorter gap than v9's.
BOOT_CONFIG="/boot/firmware/config.txt"
[ -f "$BOOT_CONFIG" ] || BOOT_CONFIG="/boot/config.txt"
if [ -f "$BOOT_CONFIG" ]; then
    backup_if_exists "$BOOT_CONFIG"
    if grep -q '^disable_splash=1' "$BOOT_CONFIG"; then
        ok "disable_splash=1 already set in $BOOT_CONFIG"
    elif grep -q '^disable_splash=' "$BOOT_CONFIG"; then
        sudo sed -i 's/^disable_splash=.*/disable_splash=1/' "$BOOT_CONFIG"
        ok "disable_splash= updated to 1 in $BOOT_CONFIG (rainbow splash removed)"
    else
        echo "disable_splash=1" | sudo tee -a "$BOOT_CONFIG" > /dev/null
        ok "disable_splash=1 added to $BOOT_CONFIG (rainbow splash removed)"
    fi
else
    warn "Could not find config.txt (checked /boot/firmware and /boot) -- rainbow splash not addressed"
fi

# Keep kernel text and the raspberry logos off the screen (they showed from
# ~2 s to ~11 s): add the single kernel argument fbcon=map:1, so the text
# console never attaches to the HDMI framebuffer. Nothing else on the line
# changes -- console=tty1 stays and getty@tty1 is not touched. (An earlier
# build removed console=tty1, added quiet flags and masked getty; the
# device then hung with a blank screen on three reboots. That code is gone.)
# Tested first with tryboot on the bench, then as the normal boot line.
# Undo: sudo tools/boot-test.sh revert, or on a PC copy cmdline.txt.orig
# over cmdline.txt on the SD card's boot partition.
CMDLINE="/boot/firmware/cmdline.txt"
[ -f "$CMDLINE" ] || CMDLINE="/boot/cmdline.txt"
if [ ! -f "$CMDLINE" ]; then
    warn "Could not find cmdline.txt (checked /boot/firmware and /boot) -- boot text will be visible for a few seconds"
elif grep -q 'fbcon=map:1' "$CMDLINE"; then
    ok "fbcon=map:1 already in $CMDLINE"
elif [ "$(grep -c . "$CMDLINE")" -ne 1 ] || ! grep -q 'console=tty1' "$CMDLINE"; then
    warn "$CMDLINE doesn't look like a stock one-line Raspberry Pi OS boot line -- left unchanged (boot text will show for a few seconds)"
else
    [ -f "$CMDLINE.orig" ] || sudo cp -a "$CMDLINE" "$CMDLINE.orig"
    mkdir -p /home/pi/boot-backup && [ -f /home/pi/boot-backup/cmdline.txt.orig ] || cp "$CMDLINE" /home/pi/boot-backup/cmdline.txt.orig
    NEW_LINE="$(tr -d '\n' < "$CMDLINE") fbcon=map:1"
    printf '%s\n' "$NEW_LINE" | sudo tee "$CMDLINE.new" > /dev/null
    if [ "$(wc -l < "$CMDLINE.new")" -eq 1 ]; then
        sudo mv "$CMDLINE.new" "$CMDLINE" && sync
        ok "fbcon=map:1 added to $CMDLINE (original saved as cmdline.txt.orig)"
    else
        sudo rm -f "$CMDLINE.new"
        warn "Refused to change $CMDLINE (result was not a single line) -- left unchanged"
    fi
fi

# Widget's own clock is IST regardless of physical site -- confirmed live
# against the real widget page (timezone: Asia/Calcutta in its API payload).
sudo timedatectl set-timezone Asia/Kolkata && ok "System timezone set to Asia/Kolkata (IST)" \
    || warn "Failed to set timezone -- check manually with 'timedatectl'"

# NTP sync when online; the kernel's own clock keeps ticking from its last
# known value when offline (normal Linux behavior, nothing to configure) --
# this Pi has no battery-backed RTC, so NTP-on-boot is what keeps the clock
# accurate across power cycles rather than drifting from a stale value.
sudo timedatectl set-ntp true && ok "NTP time sync enabled (systemd-timesyncd)" \
    || warn "Failed to enable NTP sync -- check manually with 'timedatectl'"

step "5/$TOTAL_STEPS — Installing the renderer"
sudo mkdir -p /opt/kiosk-renderer/assets /etc/kiosk-renderer /var/lib/kiosk-renderer
for py in kiosk_renderer.py indoor_layout.py widget_rules.py api_client.py kiosk_splash.py client_assets.py setup_helper.py; do
    sudo cp "$SCRIPT_DIR/renderer/$py" "/opt/kiosk-renderer/$py" || fatal "Failed to copy $py"
done
if [ -d "$SCRIPT_DIR/renderer/assets" ]; then
    sudo cp -r "$SCRIPT_DIR/renderer/assets/." /opt/kiosk-renderer/assets/
fi
sudo chown -R pi:pi /opt/kiosk-renderer /var/lib/kiosk-renderer
ok "renderer installed to /opt/kiosk-renderer"

# Real bug found on the bench: api_client.py opens these two log files
# directly as the unprivileged 'pi' user (it never calls sudo itself), but
# /var/log/ is root-owned -- kiosk-renderer.service crash-looped with
# "PermissionError: [Errno 13] Permission denied: '/var/log/kiosk-api.log'"
# until these were pre-created and handed to pi here, the same way every
# other watchdog's log already was.
sudo touch /var/log/kiosk-api.log /var/log/kiosk-widget-health.log
sudo chown pi:pi /var/log/kiosk-api.log /var/log/kiosk-widget-health.log
ok "api/widget-health log files created"

backup_if_exists /etc/kiosk-renderer/config.json
CONFIG_TMP=$(mktemp)
python3 "$SCRIPT_DIR/renderer/setup_helper.py" write-config "$SCRIPT_DIR/renderer/config.json.template" "$CONFIG_TMP" \
    "WIDGET_ID=$WIDGET_ID" "SECRET_KEY=$SECRET_KEY" "DEVICE_LABEL=$DEVICE_LABEL" \
    "BG_MODE=$BG_MODE" "BG_URL=$BG_URL" "LOGO_MODE=$LOGO_MODE" "LOGO_URL=$LOGO_URL" \
    "QR_MODE=$QR_MODE" "QR_URL=$QR_URL" "OUTDOOR=$OUTDOOR" || fatal "Failed to build config.json"
sudo install -o pi -g pi -m 600 "$CONFIG_TMP" /etc/kiosk-renderer/config.json || fatal "Failed to write config.json"
rm -f "$CONFIG_TMP"
ok "config written to /etc/kiosk-renderer/config.json"

# Download the branding chosen in step 1 into the kiosk's local cache now, so
# the very first boot shows it (the kiosk keeps it through reboots/offline).
# Clearing the old cache first means a previous client's images never linger.
sudo rm -rf /var/lib/kiosk-renderer/assets
if (cd /opt/kiosk-renderer && sudo -u pi python3 client_assets.py --sync) >> "$INSTALL_LOG" 2>&1; then
    ok "branding downloaded (background: $BG_MODE, logo: $LOGO_MODE, QR: $QR_MODE)"
else
    warn "Could not download all of the branding -- the kiosk retries every 5 minutes (see $INSTALL_LOG)"
fi

step "6/$TOTAL_STEPS — Installing watchdog scripts"
sed "s|__INSTALL_DATE__|$INSTALL_DATE|g; s|__DASHBOARD_HOST__|$DASHBOARD_HOST|g; s|__USB_VID__||g; s|__USB_PID__||g" \
    "$SCRIPT_DIR/watchdogs/net-watchdog.sh.template" | sudo tee /usr/local/bin/net-watchdog.sh > /dev/null
sudo chmod +x /usr/local/bin/net-watchdog.sh
ok "net-watchdog.sh installed"

if [ "$ENABLE_CEC" = true ]; then
    sed "s|__INSTALL_DATE__|$INSTALL_DATE|g" \
        "$SCRIPT_DIR/watchdogs/cec-watchdog.sh.template" | sudo tee /usr/local/bin/cec-watchdog.sh > /dev/null
    sudo chmod +x /usr/local/bin/cec-watchdog.sh
    ok "cec-watchdog.sh installed (ported from v9, unchanged)"
else
    ok "cec-watchdog.sh skipped (not enabled for this device)"
fi

sed "s|__INSTALL_DATE__|$INSTALL_DATE|g" \
    "$SCRIPT_DIR/watchdogs/wifi-scan-watchdog.sh.template" | sudo tee /usr/local/bin/wifi-scan-watchdog.sh > /dev/null
sudo chmod +x /usr/local/bin/wifi-scan-watchdog.sh
ok "wifi-scan-watchdog.sh installed"

sed "s|__INSTALL_DATE__|$INSTALL_DATE|g" \
    "$SCRIPT_DIR/watchdogs/tamper-watchdog.sh.template" | sudo tee /usr/local/bin/tamper-watchdog.sh > /dev/null
sudo chmod +x /usr/local/bin/tamper-watchdog.sh
ok "tamper-watchdog.sh installed"

step "7/$TOTAL_STEPS — Installing systemd units"
sudo cp "$SCRIPT_DIR/kiosk-renderer.service" /etc/systemd/system/kiosk-renderer.service
sudo cp "$SCRIPT_DIR/kiosk-splash.service" /etc/systemd/system/kiosk-splash.service
sudo cp "$SCRIPT_DIR/kiosk-console-guard.service" /etc/systemd/system/kiosk-console-guard.service
for unit in net-watchdog wifi-scan-watchdog; do
    sudo cp "$SCRIPT_DIR/watchdogs/$unit.service" "/etc/systemd/system/$unit.service"
    sudo cp "$SCRIPT_DIR/watchdogs/$unit.timer" "/etc/systemd/system/$unit.timer"
done
sudo cp "$SCRIPT_DIR/watchdogs/tamper-watchdog.service" /etc/systemd/system/tamper-watchdog.service
ENABLE_UNITS="kiosk-splash.service kiosk-console-guard.service kiosk-renderer.service net-watchdog.timer wifi-scan-watchdog.timer tamper-watchdog.service"
if [ "$ENABLE_CEC" = true ]; then
    sudo cp "$SCRIPT_DIR/watchdogs/cec-watchdog.service" /etc/systemd/system/cec-watchdog.service
    sudo cp "$SCRIPT_DIR/watchdogs/cec-watchdog.timer" /etc/systemd/system/cec-watchdog.timer
    ENABLE_UNITS="$ENABLE_UNITS cec-watchdog.timer"
fi
sudo systemctl daemon-reload
sudo systemctl enable $ENABLE_UNITS >> "$INSTALL_LOG" 2>&1
ok "systemd units installed and enabled"

step "8/$TOTAL_STEPS — Scoped sudoers (not a blanket NOPASSWD:ALL)"
require_bin BIN_MKDIR mkdir
require_bin BIN_CHOWN chown
require_bin BIN_TOUCH touch
require_bin BIN_NMCLI nmcli
require_bin BIN_TEE tee
require_bin BIN_SYSTEMCTL systemctl
require_bin BIN_TIMEOUT timeout
require_bin BIN_IW iw
require_bin BIN_RFKILL rfkill
require_bin BIN_JOURNALCTL journalctl
BIN_REBOOT="/sbin/reboot"

SUDOERS_TMP=$(mktemp)
cat > "$SUDOERS_TMP" << EOF
# architect2 kiosk -- generated by install-kiosk.sh -- $INSTALL_DATE
# Scoped NOPASSWD rules for the specific commands the watchdogs need to run
# non-interactively. Deliberately NOT a blanket NOPASSWD:ALL.
pi ALL=(root) NOPASSWD: $BIN_MKDIR -p /var/lib/kiosk-net-watchdog /var/log/kiosk-net-watchdog-incidents
pi ALL=(root) NOPASSWD: $BIN_MKDIR -p /var/lib/kiosk-tamper
pi ALL=(root) NOPASSWD: $BIN_MKDIR -p /var/lib/kiosk-wifi-scan-watchdog
pi ALL=(root) NOPASSWD: $BIN_CHOWN pi\:pi /var/lib/kiosk-net-watchdog /var/log/kiosk-net-watchdog-incidents
pi ALL=(root) NOPASSWD: $BIN_CHOWN pi\:pi /var/lib/kiosk-tamper
pi ALL=(root) NOPASSWD: $BIN_CHOWN pi\:pi /var/lib/kiosk-wifi-scan-watchdog
pi ALL=(root) NOPASSWD: $BIN_TOUCH /var/log/kiosk-net-watchdog.log /var/log/kiosk-net-watchdog-alerts.log /var/log/kiosk-wifi-scan.log /var/log/kiosk-tamper.log
pi ALL=(root) NOPASSWD: $BIN_CHOWN pi\:pi /var/log/kiosk-net-watchdog.log /var/log/kiosk-net-watchdog-alerts.log /var/log/kiosk-wifi-scan.log /var/log/kiosk-tamper.log
pi ALL=(root) NOPASSWD: $BIN_NMCLI connection up *
pi ALL=(root) NOPASSWD: $BIN_NMCLI device disconnect *
pi ALL=(root) NOPASSWD: $BIN_TEE /sys/bus/usb/drivers/usb/unbind
pi ALL=(root) NOPASSWD: $BIN_TEE /sys/bus/usb/drivers/usb/bind
pi ALL=(root) NOPASSWD: $BIN_SYSTEMCTL restart kiosk-renderer.service
pi ALL=(root) NOPASSWD: $BIN_SYSTEMCTL restart NetworkManager
pi ALL=(root) NOPASSWD: $BIN_REBOOT
pi ALL=(root) NOPASSWD: $BIN_TIMEOUT * $BIN_IW *
pi ALL=(root) NOPASSWD: $BIN_RFKILL list
pi ALL=(root) NOPASSWD: $BIN_JOURNALCTL -u NetworkManager --since * --no-pager
pi ALL=(root) NOPASSWD: $BIN_JOURNALCTL -u kiosk-renderer.service --since * --no-pager
EOF

# Only grant the cec-watchdog-specific rules when it's actually going to run
# on this device -- no point holding sudo permissions for a script that was
# never installed (see the ENABLE_CEC prompt in step 1).
if [ "$ENABLE_CEC" = true ]; then
    cat >> "$SUDOERS_TMP" << EOF
pi ALL=(root) NOPASSWD: $BIN_MKDIR -p /var/lib/pi-cec-watchdog
pi ALL=(root) NOPASSWD: $BIN_CHOWN pi\:pi /var/lib/pi-cec-watchdog
pi ALL=(root) NOPASSWD: $BIN_TOUCH /var/log/pi-cec-watchdog.log /var/log/pi-cec-watchdog-alerts.log
pi ALL=(root) NOPASSWD: $BIN_CHOWN pi\:pi /var/log/pi-cec-watchdog.log /var/log/pi-cec-watchdog-alerts.log
EOF
fi

if sudo visudo -c -f "$SUDOERS_TMP" > /tmp/sudoers-validate.log 2>&1; then
    sudo install -m 0440 -o root -g root "$SUDOERS_TMP" /etc/sudoers.d/architect2-kiosk
    ok "sudoers rules installed"
else
    cp "$SUDOERS_TMP" /tmp/sudoers-failed-draft.txt
    fatal "Generated sudoers file failed visudo validation -- see /tmp/sudoers-validate.log and /tmp/sudoers-failed-draft.txt. Nothing applied; existing sudo behavior unchanged."
fi
rm -f "$SUDOERS_TMP"

step "9/$TOTAL_STEPS — cec-ctl availability check (display, if CEC-capable)"
if [ "$ENABLE_CEC" != true ]; then
    ok "HDMI-CEC watchdog not enabled for this device -- skipping cec-ctl check"
elif command -v cec-ctl >/dev/null 2>&1; then
    ok "cec-ctl present (from v4l-utils)"
else
    warn "cec-ctl not found -- cec-watchdog.sh will self-disable harmlessly (see its SKIP logic)"
fi

step "10/$TOTAL_STEPS — Starting services"
sudo systemctl restart kiosk-renderer.service
sleep 3
START_UNITS="net-watchdog.timer wifi-scan-watchdog.timer"
[ "$ENABLE_CEC" = true ] && START_UNITS="$START_UNITS cec-watchdog.timer"
sudo systemctl start $START_UNITS
sudo systemctl start tamper-watchdog.service
ok "services started"

step "11/$TOTAL_STEPS — Self-test"
check "kiosk-renderer.service is active"        systemctl is-active --quiet kiosk-renderer.service
check "kiosk-splash.service is enabled"         systemctl is-enabled --quiet kiosk-splash.service
check "kiosk-console-guard.service is enabled"  systemctl is-enabled --quiet kiosk-console-guard.service
check "fbcon=map:1 in the boot line (no kernel text on screen)" grep -q "fbcon=map:1" /boot/firmware/cmdline.txt
check "net-watchdog.timer is active"            systemctl is-active --quiet net-watchdog.timer
if [ "$ENABLE_CEC" = true ]; then
    check "cec-watchdog.timer is active"        systemctl is-active --quiet cec-watchdog.timer
else
    ok "cec-watchdog.timer not enabled for this device (skipped, as configured)"
fi
check "wifi-scan-watchdog.timer is active"      systemctl is-active --quiet wifi-scan-watchdog.timer
check "tamper-watchdog ran"                     test -f /var/log/kiosk-tamper.log
check "/etc/kiosk-renderer/config.json exists"  test -f /etc/kiosk-renderer/config.json
check "Pillow importable"                       python3 -c "import PIL"
check "numpy importable"                        python3 -c "import numpy"
check "/dev/fb0 exists"                         test -e /dev/fb0
check "sudoers file present"                    test -f /etc/sudoers.d/architect2-kiosk
check "cache dir writable by pi"                sudo -u pi test -w /var/lib/kiosk-renderer
check "kiosk-api.log writable by pi"            sudo -u pi test -w /var/log/kiosk-api.log
check "kiosk-widget-health.log writable by pi"  sudo -u pi test -w /var/log/kiosk-widget-health.log

echo ""
c_blue "==> Self-test: $CHECKS_PASSED passed, $CHECKS_FAILED failed"
if [ "$CHECKS_FAILED" -gt 0 ]; then
    fatal "One or more self-test checks failed -- see above and $INSTALL_LOG. Do not consider this install complete."
fi
ok "Install complete. Full log: $INSTALL_LOG"
