#!/bin/bash
###############################################################################
# Builds the offline package bundle that install-kiosk.sh installs from with
# NO network access at kiosk-install time.
#
# Run this ONCE, on a device that:
#   - HAS internet access
#   - Is the same OS/architecture as the kiosks (Raspberry Pi OS Trixie,
#     armhf) -- .deb files are architecture- and (usually) release-specific,
#     so a bundle built on the wrong OS/arch will not install cleanly.
#
# It does NOT touch the kiosk-facing install-kiosk.sh at all -- it only
# (re)populates offline-packages/armhf-trixie/ next to it. Re-run this
# whenever the PACKAGES list in install-kiosk.sh changes, or periodically to
# pick up security updates, then re-zip the installer.
#
# What it does:
#   1. Computes the FULL recursive dependency closure of install-kiosk.sh's
#      PACKAGES list (not just those packages themselves -- everything they
#      transitively depend on), so the bundle works on a genuinely fresh
#      device, not just one that already has most of this installed.
#   2. Downloads every one of those packages as a .deb with `apt-get
#      download` (fetches the file regardless of current install state,
#      unlike `apt-get install`).
#   3. Builds a standard local apt repo index (Packages.gz via
#      dpkg-scanpackages) over those .debs, so install-kiosk.sh can point
#      apt at this folder as an ordinary (if `file://`) repo and let apt's
#      own dependency resolver do the installing/ordering, rather than
#      hand-rolling `dpkg -i` ordering ourselves.
###############################################################################
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
OUT_DIR="$SCRIPT_DIR/armhf-trixie"

PACKAGES=(python3 python3-pip python3-pygame fonts-dejavu-core network-manager v4l-utils libsdl2-2.0-0 libegl1 libgles2 iw rfkill usbutils)

command -v dpkg-scanpackages >/dev/null 2>&1 || {
    echo "dpkg-scanpackages not found -- installing dpkg-dev (needs network, one-time, on THIS build machine only)"
    sudo apt-get update -y
    sudo apt-get install -y dpkg-dev
}

echo "==> Updating apt package lists"
sudo apt-get update -y

echo "==> Computing full recursive dependency closure of: ${PACKAGES[*]}"
CLOSURE_RAW=$(mktemp)
apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts \
    --no-breaks --no-replaces --no-enhances "${PACKAGES[@]}" \
    | grep -oP '(?<=Depends: )\S+|(?<=PreDepends: )\S+' \
    > "$CLOSURE_RAW"

CLOSURE=$(mktemp)
# Drop virtual-package alternatives (shown in <angle brackets> -- not a real,
# individually downloadable package name) and foreign-arch suffixes (the
# unsuffixed name is already in the list on a native armhf system).
grep -v '^<' "$CLOSURE_RAW" | sed 's/:armhf$//; s/:arm64$//; s/:all$//' | sort -u > "$CLOSURE"
for pkg in "${PACKAGES[@]}"; do echo "$pkg"; done >> "$CLOSURE"
sort -u "$CLOSURE" -o "$CLOSURE"
rm -f "$CLOSURE_RAW"

TOTAL=$(wc -l < "$CLOSURE")
echo "==> $TOTAL packages to fetch (top-level + full transitive closure)"

echo "==> Downloading .deb files to $OUT_DIR"
mkdir -p "$OUT_DIR"
cd "$OUT_DIR"
FAILED=()
N=0
while read -r pkg; do
    N=$((N + 1))
    if ls "${pkg}"_*.deb >/dev/null 2>&1; then
        echo "  [$N/$TOTAL] $pkg already downloaded, skipping"
        continue
    fi
    echo "  [$N/$TOTAL] downloading $pkg"
    if ! apt-get download "$pkg" >/tmp/offline-bundle-download.log 2>&1; then
        FAILED+=("$pkg")
        echo "    FAILED (see /tmp/offline-bundle-download.log) -- continuing"
    fi
done < "$CLOSURE"

if [ "${#FAILED[@]}" -gt 0 ]; then
    echo ""
    echo "WARNING: ${#FAILED[@]} package(s) failed to download and are NOT in the bundle:"
    printf '  - %s\n' "${FAILED[@]}"
    echo "This bundle is INCOMPLETE. Investigate before shipping it (a common cause"
    echo "is a package name apt no longer recognizes after an OS release change --"
    echo "check for a renamed replacement)."
fi

echo "==> Building apt repo index (Packages.gz)"
dpkg-scanpackages . /dev/null | gzip -9c > Packages.gz

DEB_COUNT=$(ls -1 ./*.deb 2>/dev/null | wc -l)
TOTAL_SIZE=$(du -sh . | cut -f1)
echo ""
echo "==> Done: $DEB_COUNT .deb files, $TOTAL_SIZE total, in $OUT_DIR"
echo "    install-kiosk.sh will use this automatically (no changes needed there)."
