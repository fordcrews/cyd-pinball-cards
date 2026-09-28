// Board abstraction: everything that differs between the supported displays lives behind this
// header, so the card / idle / keypad / protocol code in main.cpp is shared.
//
//   cyd       ESP32-2432S028R "Cheap Yellow Display": 2.8" 320x240 ILI9341 (TFT_eSPI, SPI),
//             XPT2046 resistive touch, PWM backlight, CH340/CH9102 USB-UART          (env: cyd)
//   ws-s3-7   Waveshare ESP32-S3-Touch-LCD-7: 7" 800x480 RGB parallel (LovyanGFX Bus_RGB),
//             GT911 capacitive touch, CH422G IO expander (backlight on/off, resets),
//             native USB CDC + CH343 UART port                                      (env: waveshare_s3_lcd7)
//
// The drawing object is called `tft` on both boards (TFT_eSPI and LovyanGFX share the API used here).
// Layout constants in main.cpp go through S(): 1x on the CYD, 2x on the 800x480 panel.
#pragma once
#include <Arduino.h>

#if defined(BOARD_WS_S3_LCD7)
  #include <LovyanGFX.hpp>
  #define BOARD_KIND "ws-s3-7"
  #define UI_SCALE 2
  #define TOUCH_CAPACITIVE 1
  #define BACKLIGHT_DIMMABLE 0          // CH422G EXIO2 only switches the backlight boost on/off
  using Gfx = lgfx::LGFX_Device;
  extern Gfx &tft;
  // Host link: reads the native USB CDC (Serial, 303A:1001) and UART0 (the CH343 "UART" USB-C
  // port, Serial0); writes to both.
  class HostLink : public Stream {
   public:
    void begin(unsigned long baud, size_t rxBuf);
    int available() override;
    int read() override;
    int peek() override;
    size_t write(uint8_t c) override;
    size_t write(const uint8_t *buf, size_t n) override;
    void flush() override;
  };
  extern HostLink hostLink;
  #define HOST hostLink
#else
  #include <SPI.h>
  #include <TFT_eSPI.h>
  #define BOARD_KIND "cyd"
  #define UI_SCALE 1
  #define TOUCH_CAPACITIVE 0
  #define BACKLIGHT_DIMMABLE 1
  using Gfx = TFT_eSPI;
  extern Gfx tft;
  #define HOST Serial
#endif

#define S(v) ((v) * UI_SCALE)

// One touch sample. x/y are screen coordinates in the current rotation; raw_x/raw_y are the
// controller values (XPT2046 12-bit raw, GT911 native panel pixels); z is pressure (XPT2046) or
// a fixed "strong" value for capacitive touch.
struct TouchSample {
  int x, y, rawX, rawY, z;
};

void boardBeginSerial(size_t rxBuf);     // Serial / host link at 115200
void boardInitDisplay(uint8_t rotation, uint8_t brightness); // panel, backlight, touch controller
void boardSetBacklight(uint8_t v);       // 0-255 (on/off only when BACKLIGHT_DIMMABLE == 0)
void boardSetRotation(uint8_t rotation); // protocol rotation: 0/2 portrait, 1/3 landscape (both boards)
bool boardReadTouch(TouchSample &t);     // true while touched; raw values only (CYD maps them in main.cpp)
