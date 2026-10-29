# Raspberry Pi browser display

This sets up any Raspberry Pi that runs Chromium (a Pi 3B+, 4, 5 or Zero 2 W) with an HDMI TV, monitor or photo-frame
panel as a full-screen cabinet display. It shows the web hub page (`host/web_hub.py`), so it works like a CYD board:
pick a role and it shows that part of the game that's playing. The `photoframe` role shows your Immich photos when no game is running.

## You need

* Raspberry Pi OS **Bookworm with desktop** (32- or 64-bit). Both Wayland (labwc or wayfire, the default) and X11 work.
* The cabinet PC running `cyd_daemon.py` with the web hub turned on (`"web_hub": {"enabled": true}`, see
  [docs/TV_DISPLAYS.md](../../docs/TV_DISPLAYS.md)), and its LAN or Tailscale address.

## Install

```bash
git clone https://github.com/fordcrews/cyd-pinball-cards.git
cd cyd-pinball-cards/frontends/pi-display
bash install.sh
```

The script asks for the cabinet PC's address, the port (47321), a display name and the role, then:

1. Installs Chromium (and `unclutter` to hide the mouse pointer on X11) if they aren't there.
2. Turns screen blanking off (`raspi-config nonint do_blanking 1`, plus the wayfire idle settings and `xset` on X11).
3. Writes `~/.config/cyd-kiosk/kiosk.env` (the URL) and `~/.local/bin/cyd-kiosk` (the launcher).
4. Installs the user service `cyd-kiosk.service` and starts it with the desktop session. It also sets the Pi to boot
   to the desktop with auto-login.

Reboot when it's done. To skip the questions, pass the URL instead:

```bash
bash install.sh --url "http://192.168.1.50:47321/?id=hall-frame&role=photoframe"
```

## Day to day

| task | command |
|---|---|
| change the URL or role | edit `~/.config/cyd-kiosk/kiosk.env`, then `systemctl --user restart cyd-kiosk` |
| see what it's doing | `journalctl --user -u cyd-kiosk -f` |
| stop for now | `systemctl --user stop cyd-kiosk` |
| remove | `bash install.sh --uninstall` |

When the Pi starts, the launcher waits up to 5 minutes for the cabinet PC. After that the page reconnects by itself.
To get to the desktop, plug in a keyboard and press Alt+F4.

## Tips

* **Photo frame:** use `role=photoframe`. In quiet hours (the `night` settings in `config.json`) it shows a dim clock,
  dimmed photos, or a black screen, depending on `web_hub.photoframe_quiet` (`clock`, `dim` or `off`).
* **Portrait panel:** rotate it in *Preferences > Screen Configuration*. The page fits any shape.
* **Over Tailscale:** install Tailscale on the Pi and use the cabinet PC's `100.x` address.
* **Pi Zero 2 W:** it works, but use the `picture` or `photoframe` role and a 1080p or smaller screen.
