// ESP32-2432S028R ("Cheap Yellow Display") board layer: TFT_eSPI (ILI9341 on HSPI),
// XPT2046 resistive touch on VSPI, PWM backlight on TFT_BL. Pins come from platformio.ini [env:cyd].
#include "board.h"
#if !defined(BOARD_WS_S3_LCD7)
#include <XPT2046_Touchscreen.h>

#define BL_CHANNEL 0

TFT_eSPI tft;
static SPIClass touchSpi(VSPI);
static XPT2046_Touchscreen ts(XPT2046_CS, XPT2046_IRQ);

void boardBeginSerial(size_t rxBuf) {
  // Big RX ring buffer (must be set before begin): a full 6 KB line can arrive while a screen
  // is being drawn without overflowing the default 256-byte UART buffer.
  Serial.setRxBufferSize(rxBuf);
  Serial.begin(115200);
}

void boardSetBacklight(uint8_t v) { ledcWrite(BL_CHANNEL, v); }

void boardInitDisplay(uint8_t rotation, uint8_t brightness) {
  // Backlight PWM (Arduino-ESP32 core 2.x API), same order as firmware <= 1.3.0
  ledcSetup(BL_CHANNEL, 5000, 8);
  ledcAttachPin(TFT_BL, BL_CHANNEL);
  boardSetBacklight(brightness);
  tft.init();
  tft.setRotation(rotation);
  tft.fillScreen(TFT_BLACK);
  touchSpi.begin(XPT2046_CLK, XPT2046_MISO, XPT2046_MOSI, XPT2046_CS);
  ts.begin(touchSpi);
  ts.setRotation(1);  // always native orientation; main.cpp touchToScreen() applies the display rotation
}

void boardSetRotation(uint8_t rotation) { tft.setRotation(rotation); }

bool boardReadTouch(TouchSample &t) {
  if (!ts.touched()) return false;
  TS_Point p = ts.getPoint();
  t.rawX = p.x;
  t.rawY = p.y;
  t.z = p.z;
  t.x = t.y = 0;  // mapped with the calibration in main.cpp
  return true;
}
bool boardUsbHostOpen() { return false; }  // CH340/CP210x: the UART is up whenever the cable is powered
#endif
