# GRS Build-A-Cade FU: hardware notes for the CYD display

What the Kickstarter / GRS material says about the cabinet, what it means for the display, and
what is still unknown. Sources at the end; "unverified" means only third-party write-ups say so.

## The cabinet
* Full-size, modular DIY arcade cabinet by Glen's Retro Show / Thunderstick Studios (Kickstarter
  "GRS BUILD-A-CADE FU – The Revolution in Arcade Gaming", funded Jun–Aug 2025).
* Computer: **GRS Viper Venom** single-board computer, **Rockchip RK3588** (ARM), running
  **R-Cade OS** "specially designed for Viper Venom SBC". Not a PC and not a Raspberry Pi (the
  Raspberry Pi only appears in the bonus 1:6-scale mini kit).
* Screens: 19" with automatic rotation, or 26" 4:3 (UNICO) IPS; R-Cade rotates the picture and
  switches the joystick for vertical games.
* Swappable control panels that plug in over USB (trackball, spinner, dual rotary sticks, steering
  wheels, flight yoke, FU-Tron panel "with a built-in encoder board"), all "plug-and-play" with
  R-Cade.
* GRS encoders (Zero Delay V2, Universal Encoder Hub) enumerate as **standard USB game controllers**
  (spinners/trackballs as mouse axes), not keyboards. So the cabinet's own controls send gamepad
  input, which is why the kit's R-Cade keypad drives a virtual *gamepad* for R-Cade's hotkey
  combos. (Which encoder the FU's standard panel uses is **unknown**: check with
  `cat /proc/bus/input/devices` over SSH.)
* Unverified (third-party review): front USB ports on the control panel, "plenty of USB ports
  inside the machine too, including a powered USB hub", and a keyboard drawer.
* Viper Venom port list: **unknown** (not published that we found). For comparison, the older
  RK3566 GRS Viper SBC has 2 × USB 2.0 A, 1 × USB 3.0 A, USB-C (OTG/power), HDMI, DSI, 40-pin GPIO,
  12 V DC in.

## Where to plug the display in
* Use a **USB-A port inside the cabinet**: the internal powered hub if there is one (best: it also
  powers several displays), otherwise a free port on the Viper Venom. Keep the front control-panel
  ports free for guests' controllers and light guns.
* One CYD draws about 100–150 mA (backlight full); a Waveshare 7" about 450 mA, so give the 7"
  board a powered hub port. 3–5 displays: powered USB 2.0 hub (main README, "USB and power").
* A **data** cable, 1 m or shorter. The CYD's CH340/CP2102 is USB 2.0 full-speed; any port works.
* Don't use the SBC's USB-C/OTG port (power/firmware on the Viper boards).
* Power sequencing: the display is powered from USB, so it goes dark when the cabinet powers the
  SBC/hub down, and comes back with the idle playlist when R-Cade's `system-ready` script runs.

## Mounting ideas
* **Control-panel "palm" displays** (roles `left` / `right`): the FU's control decks are
  swappable, so don't cut the decks. Mount the CYD in a small printed pod on the cabinet side or
  on the fixed part of the front, and route its cable inside to the hub. Portrait rotation
  (`--assign ... --rotation 1/3`).
* **Coin door / below the screen:** a single CYD in landscape next to the coin door or under the
  screen bezel as a "now playing" card; the keypad is within reach there.
* **Topper / marquee area:** the FU has an illuminated marquee (and Pixelcade support in R-Cade);
  a Waveshare 7" works as a small secondary marquee/info screen above it.
* The screen rotates: don't mount anything on the rotating monitor assembly of the 19" version.
* See the main README "Mounting notes" for bezels, glass and strain relief.

## Sources
* Kickstarter page (blurb, reward and add-on texts; the story body could not be fetched, it sits
  behind Cloudflare): https://www.kickstarter.com/projects/tstick/grs-build-a-cade-fu-the-revolution-in-arcade-gaming
  (read via the Internet Archive copy of 2026-08-30)
* BackerKit tracker (funding dates): https://www.backerkit.com/projects/tstick/grs-build-a-cade-fu-the-revolution-in-arcade-gaming
* GRS Viper SBC (RK3566) product page: https://thunderstickstudio.com/products/grs-viper-sbc
* Thunderstick encoder guide: https://thunderstickstudio.com/blogs/news/arcade-encoder-boards-usb-wiring-setup-guide-2026
* R-Cade 2.0.4 release notes, "Added Support for GRS Build-A-Cade with Viper board (plug-and-play)":
  https://github.com/retro-center/rcade_releases/releases
* Third-party review (unverified claims: USB hub, front USB, keyboard drawer):
  https://www.acciyo.com/grs-build-a-cade-fu-arcade-gaming-machine-review-your-ultimate-guide-to-home-arcade-bliss/
