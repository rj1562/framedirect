#!/bin/bash
# Push a new renderer build onto an installed kiosk without re-running the
# installer (keeps config.json and the cached branding as they are).
#   sudo tools/update-renderer.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for py in kiosk_renderer.py indoor_layout.py widget_rules.py api_client.py kiosk_splash.py client_assets.py setup_helper.py; do
    install -o pi -g pi -m 644 "$SRC/renderer/$py" "/opt/kiosk-renderer/$py"
done
cp -r "$SRC/renderer/assets/." /opt/kiosk-renderer/assets/
chown -R pi:pi /opt/kiosk-renderer
for unit in kiosk-splash.service kiosk-console-guard.service kiosk-renderer.service; do
    install -m 644 "$SRC/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable kiosk-splash.service kiosk-console-guard.service kiosk-renderer.service
systemctl restart kiosk-console-guard.service kiosk-renderer.service
sleep 2
systemctl is-active kiosk-console-guard.service kiosk-renderer.service
echo "updated"
