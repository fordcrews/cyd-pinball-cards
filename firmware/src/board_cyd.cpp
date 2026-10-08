// ESP32-2432S028R ("Cheap Yellow Display") board layer: TFT_eSPI (ILI9341 on HSPI),
// XPT2046 resistive touch on VSPI, PWM backlight on TFT_BL. Pins come from platformio.ini [env:cyd].
#include "board.h"
#if !defined(BOARD_WS_S3_LCD7)
#include <TJpg_Decoder.h>
#if !defined(CYD_TOUCH_SHARED_SPI)
#include <XPT2046_Touchscreen.h>
#endif

#define BL_CHANNEL 0

TFT_eSPI tft;
#if defined(CYD_TOUCH_SHARED_SPI)
// ESP32-3248S035R / E32R35T: the XPT2046 sits on the display's SPI bus (TOUCH_CS), so TFT_eSPI's
// own touch reader is used (it shares the bus safely). Raw axes match XPT2046_Touchscreen rotation 1.
#ifndef TOUCH_Z_MIN
#define TOUCH_Z_MIN 350
#endif
#else
static SPIClass touchSpi(VSPI);
static XPT2046_Touchscreen ts(XPT2046_CS, XPT2046_IRQ);
#endif

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
#if !defined(CYD_TOUCH_SHARED_SPI)
  touchSpi.begin(XPT2046_CLK, XPT2046_MISO, XPT2046_MOSI, XPT2046_CS);
  ts.begin(touchSpi);
  ts.setRotation(1);  // always native orientation; main.cpp touchToScreen() applies the display rotation
#endif
}

void boardSetRotation(uint8_t rotation) { tft.setRotation(rotation); }

bool boardReadTouch(TouchSample &t) {
#if defined(CYD_TOUCH_SHARED_SPI)
  uint16_t z = tft.getTouchRawZ();
  if (z < TOUCH_Z_MIN) return false;
  uint16_t rx = 0, ry = 0;
  tft.getTouchRaw(&rx, &ry);
  t.rawX = rx;
  t.rawY = ry;
  t.z = z;
#else
  if (!ts.touched()) return false;
  TS_Point p = ts.getPoint();
  t.rawX = p.x;
  t.rawY = p.y;
  t.z = p.z;
#endif
  t.x = t.y = 0;  // mapped with the calibration in main.cpp
  return true;
}
bool boardUsbHostOpen() { return false; }  // CH340/CP210x: the UART is up whenever the cable is powered

// TJpgDec hands over one decoded MCU block (16x16 or 8x8 RGB565) at a time, so only a ~3 KB work
// area is needed besides the JPEG itself: fine without PSRAM.
static bool jpegBlock(int16_t x, int16_t y, uint16_t w, uint16_t h, uint16_t *bitmap) {
  if (y >= tft.height()) return false;  // below the screen: stop decoding
  tft.pushImage(x, y, w, h, bitmap);
  return true;
}

bool boardDrawJpeg(const uint8_t *data, size_t len, int x, int y) {
  TJpgDec.setJpgScale(1);
  TJpgDec.setSwapBytes(true);
  TJpgDec.setCallback(jpegBlock);
  return TJpgDec.drawJpg(x, y, data, len) == JDR_OK;
}
#endif
