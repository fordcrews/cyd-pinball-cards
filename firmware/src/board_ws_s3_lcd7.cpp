// Waveshare ESP32-S3-Touch-LCD-7 board layer (LovyanGFX).
//
// Sources (see README "Waveshare ESP32-S3-Touch-LCD-7"):
//  * https://www.waveshare.com/wiki/ESP32-S3-Touch-LCD-7  (pinout tables, I2C address scan, BOOT/UART notes)
//  * Waveshare demo ESP32-S3-Touch-LCD-7-Demo.zip: Arduino/examples/08_DrawColorBar/waveshare_lcd_port.h
//    (RGB pins, 16 MHz PCLK, HPW/HBP/HFP = 4/8/8, VPW/VBP/VFP = 4/8/8, EXIO numbers) and
//    ESP-IDF/08_lvgl_Porting/main/waveshare_rgb_lcd_port.c (CH422G raw writes: 0x01 -> 0x24,
//    backlight on 0x1E / off 0x1A -> 0x38, GT911 reset sequence with INT held low)
//  * ESP32_Display_Panel BOARD_WAVESHARE_ESP32_S3_TOUCH_LCD_7.h (pclk_active_neg = 1, GT911 on
//    SDA 8 / SCL 9 / INT 4, CH422G at 0x20, backlight = expander pin 2, LCD_RST = pin 3, TP_RST = pin 1)
//  * ESP32_IO_Expander esp_io_expander_ch422g.c (WR_SET 0x24, WR_OC 0x23, WR_IO 0x38, RD_IO 0x26)
#include "board.h"
#if defined(BOARD_WS_S3_LCD7)
#include <lgfx/v1/platforms/esp32s3/Panel_RGB.hpp>
#include <lgfx/v1/platforms/esp32s3/Bus_RGB.hpp>

// ---- pins (ESP32-S3 GPIO) ----
static constexpr int PIN_I2C_SDA = 8, PIN_I2C_SCL = 9, PIN_TP_INT = 4;
static constexpr int I2C_PORT = 1;               // I2C_NUM_1 (touch + CH422G share the bus)
static constexpr uint32_t I2C_FREQ = 400000;

// ---- CH422G: it has no register pointer, each "register" is its own 7-bit I2C address ----
static constexpr uint8_t CH422G_WR_SET = 0x24;   // system parameters (bit0 IO_OE: IO0-7 as outputs)
static constexpr uint8_t CH422G_WR_IO = 0x38;    // EXIO0..EXIO7 output levels
static constexpr uint8_t EXIO_TP_RST = 1 << 1;   // GT911 reset (active low)
static constexpr uint8_t EXIO_BL = 1 << 2;       // DISP: backlight boost enable (on/off only)
static constexpr uint8_t EXIO_LCD_RST = 1 << 3;  // LCD reset (active low)
static constexpr uint8_t EXIO_SD_CS = 1 << 4;    // TF card CS (active low) - kept high (deselected)
static constexpr uint8_t EXIO_USB_SEL = 1 << 5;  // low = GPIO19/20 on USB (high would switch them to CAN)
static uint8_t exio = EXIO_TP_RST | EXIO_LCD_RST | EXIO_SD_CS;  // USB_SEL stays low

class LGFX_WS7 : public lgfx::LGFX_Device {
 public:
  lgfx::Bus_RGB bus;
  lgfx::Panel_RGB panel;
  lgfx::Touch_GT911 touch;

  LGFX_WS7() {
    {
      auto cfg = panel.config();
      cfg.memory_width = cfg.panel_width = 800;
      cfg.memory_height = cfg.panel_height = 480;
      cfg.offset_x = cfg.offset_y = 0;
      panel.config(cfg);
      auto det = panel.config_detail();
      det.use_psram = 2;  // 800x480x2 = 750 KB frame buffer in PSRAM
      panel.config_detail(det);
    }
    {
      auto cfg = bus.config();
      cfg.panel = &panel;
      // D0..D15 = B3..B7, G2..G7, R3..R7 (Waveshare wiki LCD table / demo DATA0..DATA15)
      const int8_t d[16] = {14, 38, 18, 17, 10, 39, 0, 45, 48, 47, 21, 1, 2, 42, 41, 40};
      for (int i = 0; i < 16; i++) cfg.pin_data[i] = d[i];
      cfg.pin_henable = 5;  // DE
      cfg.pin_vsync = 3;
      cfg.pin_hsync = 46;
      cfg.pin_pclk = 7;
      cfg.freq_write = 16000000;
      cfg.hsync_polarity = 0;
      cfg.hsync_pulse_width = 4;
      cfg.hsync_back_porch = 8;
      cfg.hsync_front_porch = 8;
      cfg.vsync_polarity = 0;
      cfg.vsync_pulse_width = 4;
      cfg.vsync_back_porch = 8;
      cfg.vsync_front_porch = 8;
      cfg.pclk_active_neg = 1;
      cfg.de_idle_high = 0;
      cfg.pclk_idle_high = 0;
      bus.config(cfg);
      panel.setBus(&bus);
    }
    {
      auto cfg = touch.config();
      cfg.x_min = 0;
      cfg.x_max = 799;
      cfg.y_min = 0;
      cfg.y_max = 479;
      cfg.pin_int = -1;  // polled (GPIO4 is only used to pick the I2C address during reset)
      cfg.pin_rst = -1;  // reset is on the CH422G (EXIO1), done before init()
      cfg.bus_shared = false;
      cfg.offset_rotation = 0;
      cfg.i2c_port = I2C_PORT;
      cfg.pin_sda = PIN_I2C_SDA;
      cfg.pin_scl = PIN_I2C_SCL;
      cfg.i2c_addr = 0x5D;  // INT low during reset -> 0x5D (matches the wiki's I2C scan)
      cfg.freq = I2C_FREQ;
      touch.config(cfg);
      panel.setTouch(&touch);
    }
    setPanel(&panel);
  }
};

static LGFX_WS7 lcd;
Gfx &tft = lcd;
HostLink hostLink;

static bool ch422gWrite(uint8_t addr, uint8_t val) {
  return lgfx::i2c::transactionWrite(I2C_PORT, addr, &val, 1, I2C_FREQ).has_value();
}

static void exioWrite() { ch422gWrite(CH422G_WR_IO, exio); }

// ---------- host link: native USB CDC + UART0 (CH343) ----------
void HostLink::begin(unsigned long baud, size_t rxBuf) {
  Serial.setRxBufferSize(rxBuf);  // HWCDC (USB-Serial/JTAG)
  Serial.setTxBufferSize(2048);   // HWCDC skips writing (never blocks) while no host reads the port
  Serial.begin(baud);
  Serial0.setRxBufferSize(rxBuf);
  Serial0.begin(baud);            // UART0 = GPIO43/44 = CH343 "UART" USB-C (DIP switch on UART1)
}
int HostLink::available() { return Serial.available() + Serial0.available(); }
int HostLink::read() { return Serial.available() ? Serial.read() : Serial0.read(); }
int HostLink::peek() { return Serial.available() ? Serial.peek() : Serial0.peek(); }
size_t HostLink::write(uint8_t c) { return write(&c, 1); }
size_t HostLink::write(const uint8_t *buf, size_t n) {
  Serial.write(buf, n);   // dropped by HWCDC while the CDC port is not being read
  Serial0.write(buf, n);
  return n;
}
void HostLink::flush() {
  Serial.flush();
  Serial0.flush();
}

void boardBeginSerial(size_t rxBuf) { hostLink.begin(115200, rxBuf); }

bool boardUsbHostOpen() { return (bool)Serial; }  // HWCDC: true while a host has the native USB port open

// ---------- display / backlight / touch ----------
void boardSetBacklight(uint8_t v) {
  uint8_t n = v ? (exio | EXIO_BL) : (exio & ~EXIO_BL);
  if (n != exio) {
    exio = n;
    exioWrite();
  }
}

// Protocol rotation (same meaning as on the CYD): 1/3 landscape, 0/2 portrait.
// The panel is natively landscape, so LovyanGFX rotation = protocol rotation - 1.
static uint8_t lgfxRot(uint8_t r) { return (r + 3) & 3; }

void boardSetRotation(uint8_t rotation) { lcd.setRotation(lgfxRot(rotation)); }

void boardInitDisplay(uint8_t rotation, uint8_t brightness) {
  lgfx::i2c::init(I2C_PORT, PIN_I2C_SDA, PIN_I2C_SCL);
  ch422gWrite(CH422G_WR_SET, 0x01);  // IO0-7 push-pull outputs
  // LCD reset pulse + GT911 reset with INT held low (-> I2C address 0x5D), backlight off meanwhile
  pinMode(PIN_TP_INT, OUTPUT);
  digitalWrite(PIN_TP_INT, LOW);
  exio = EXIO_SD_CS;  // TP_RST low, LCD_RST low, BL off, USB_SEL low
  exioWrite();
  delay(20);
  exio |= EXIO_LCD_RST;
  exioWrite();
  delay(100);
  exio |= EXIO_TP_RST;
  exioWrite();
  delay(200);
  pinMode(PIN_TP_INT, INPUT);
  lcd.init();
  lcd.setRotation(lgfxRot(rotation));
  lcd.fillScreen(TFT_BLACK);
  boardSetBacklight(brightness);
}

bool boardReadTouch(TouchSample &t) {
  lgfx::touch_point_t tp;
  if (!lcd.getTouchRaw(&tp, 1)) return false;  // GT911 native panel coordinates (landscape)
  t.rawX = tp.x;
  t.rawY = tp.y;
  lcd.convertRawXY(&tp, 1);                     // -> current rotation
  t.x = tp.x;
  t.y = tp.y;
  t.z = 1000;  // capacitive: no pressure; treat every sample as a firm press
  return true;
}
#endif
