#!/usr/bin/env bash
# Turn a Raspberry Pi (Raspberry Pi OS Bookworm with desktop, Wayland or X11) into a full-screen
# cabinet display: Chromium in kiosk mode on the web hub, screen blanking off, starts at boot.
#
#   curl -O .../install.sh   (or copy this folder)   then:   bash install.sh
#   bash install.sh --url "http://192.168.1.50:47321/?id=hall-frame&role=photoframe"   (no prompt)
#   bash install.sh --uninstall
#
# Run as the desktop (auto-login) user, not root; it uses sudo for packages and raspi-config only.
set -euo pipefail

URL=""
UNINSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --url) URL="${2:-}"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" = "0" ]; then
  echo "Run this as the desktop user (for example pi), not with sudo." >&2
  exit 1
fi

CONF="$HOME/.config/cyd-kiosk"
BIN="$HOME/.local/bin/cyd-kiosk"
UNIT="$HOME/.config/systemd/user/cyd-kiosk.service"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ "$UNINSTALL" = 1 ]; then
  systemctl --user disable --now cyd-kiosk 2>/dev/null || true
  rm -f "$UNIT" "$BIN" "$HOME/.config/autostart/cyd-kiosk.desktop"
  systemctl --user daemon-reload || true
  echo "Removed. Settings kept in $CONF (delete it by hand if you want)."
  exit 0
fi

# ---------------------------------------------------------------- the URL
if [ -z "$URL" ]; then
  echo "Cabinet browser display setup"
  echo "-----------------------------"
  read -r -p "Cabinet PC address (LAN or Tailscale IP / name, e.g. 192.168.1.50): " HOST
  read -r -p "Web hub port [47321]: " PORT
  PORT="${PORT:-47321}"
  read -r -p "Display name (id), e.g. hall-frame [$(hostname)]: " DID
  DID="${DID:-$(hostname)}"
  echo "Roles: photoframe, picture, pictureboxart, control_panel, howtoplay, gallery, all"
  read -r -p "Role [photoframe]: " ROLE
  ROLE="${ROLE:-photoframe}"
  [ -n "$HOST" ] || { echo "An address is needed." >&2; exit 1; }
  URL="http://$HOST:$PORT/?id=$DID&role=$ROLE"
fi
case "$URL" in http://*|https://*) ;; *) echo "URL must start with http:// or https://" >&2; exit 1 ;; esac
echo "Display URL: $URL"

# ---------------------------------------------------------------- packages
BROWSER="$(command -v chromium-browser || command -v chromium || true)"
if [ -z "$BROWSER" ]; then
  sudo apt-get update
  sudo apt-get install -y chromium-browser || sudo apt-get install -y chromium
  BROWSER="$(command -v chromium-browser || command -v chromium)"
fi
command -v unclutter >/dev/null 2>&1 || sudo apt-get install -y unclutter || true   # hides the X11 mouse pointer

# ---------------------------------------------------------------- screen blanking off
if command -v raspi-config >/dev/null 2>&1; then
  sudo raspi-config nonint do_blanking 1 || true          # 1 = disable blanking (X11 and Wayland)
fi
mkdir -p "$HOME/.config/wayfire" 2>/dev/null || true
if [ -f "$HOME/.config/wayfire.ini" ] && ! grep -q "^\[idle\]" "$HOME/.config/wayfire.ini"; then
  printf '\n[idle]\ndpms_timeout = -1\nscreensaver_timeout = -1\n' >> "$HOME/.config/wayfire.ini"
fi

# ---------------------------------------------------------------- launcher + service
mkdir -p "$CONF" "$(dirname "$BIN")" "$(dirname "$UNIT")"
printf 'CYD_KIOSK_URL=%s\nCYD_KIOSK_BROWSER=%s\n' "$URL" "$BROWSER" > "$CONF/kiosk.env"
cat > "$BIN" <<'LAUNCH'
#!/usr/bin/env bash
# Started by cyd-kiosk.service. Works on Wayland (labwc / wayfire) and X11.
URL="${CYD_KIOSK_URL:?set in ~/.config/cyd-kiosk/kiosk.env}"
BROWSER="${CYD_KIOSK_BROWSER:-chromium-browser}"
PROFILE="$HOME/.config/cyd-kiosk/chromium"
# A crash or power cut must not bring up the "restore pages?" bar.
sed -i 's/"exited_cleanly":false/"exited_cleanly":true/; s/"exit_type":"[^"]*"/"exit_type":"Normal"/' \
  "$PROFILE/Default/Preferences" 2>/dev/null || true
EXTRA=()
if [ -n "${WAYLAND_DISPLAY:-}" ]; then
  EXTRA+=(--ozone-platform=wayland)
else
  export DISPLAY="${DISPLAY:-:0}"
  xset s off -dpms s noblank 2>/dev/null || true
  command -v unclutter >/dev/null && (unclutter -idle 1 -root &) || true
fi
# Wait for the cabinet PC (it may boot after the Pi); the page itself reconnects after that.
HOSTPORT="$(echo "$URL" | sed -E 's#^[a-z]+://([^/]+).*#\1#')"
for _ in $(seq 1 60); do
  curl -s -m 3 -o /dev/null "http://$HOSTPORT/api/clients" && break
  sleep 5
done
exec "$BROWSER" --kiosk --noerrdialogs --disable-infobars --no-first-run --incognito \
  --disable-session-crashed-bubble --disable-features=Translate --check-for-update-interval=31536000 \
  --autoplay-policy=no-user-gesture-required --user-data-dir="$PROFILE" "${EXTRA[@]}" "$URL"
LAUNCH
chmod +x "$BIN"
cp "$HERE/cyd-kiosk.service" "$UNIT"

# The user service needs the graphical session's environment (WAYLAND_DISPLAY / DISPLAY).
mkdir -p "$HOME/.config/autostart"
cat > "$HOME/.config/autostart/cyd-kiosk.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=CYD kiosk
Exec=sh -c 'systemctl --user import-environment WAYLAND_DISPLAY DISPLAY XAUTHORITY; systemctl --user restart cyd-kiosk'
X-GNOME-Autostart-enabled=true
DESK
LABWC="$HOME/.config/labwc/autostart"
if [ -d /etc/xdg/labwc ] && ! grep -qs cyd-kiosk "$LABWC"; then
  mkdir -p "$(dirname "$LABWC")"
  echo "sh -c 'systemctl --user import-environment WAYLAND_DISPLAY DISPLAY; systemctl --user restart cyd-kiosk' &" >> "$LABWC"
fi
systemctl --user daemon-reload
systemctl --user enable cyd-kiosk >/dev/null

if command -v raspi-config >/dev/null 2>&1; then
  sudo raspi-config nonint do_boot_behaviour B4 || true   # desktop, auto-login
fi
echo
echo "Done. Reboot to start the display:  sudo reboot"
echo "Change the URL later: edit $CONF/kiosk.env, then  systemctl --user restart cyd-kiosk"
