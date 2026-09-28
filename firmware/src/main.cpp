// CYD Pinball Cards - companion card display for a virtual pinball cabinet
// Board: ESP32-2432S028R (Cheap Yellow Display)
//
// Serial protocol (115200 baud, newline-delimited JSON, one object per line):
//   {"cmd":"table","title":"Medieval Madness","cards":[{"type":"instructions","title":"Rules","text":"..."}, ...]}
//   {"cmd":"idle"}
//   {"cmd":"brightness","value":0-255}
//   {"cmd":"ping"}
//   {"cmd":"rotation","value":0-3}
// Replies: {"ack":"<cmd>","ok":true[, ...]} or {"ack":"<cmd>","ok":false,"err":"..."}

#include <Arduino.h>
#include <SPI.h>
#include <TFT_eSPI.h>
#include <XPT2046_Touchscreen.h>
#include <ArduinoJson.h>
#include <Preferences.h>

#ifndef CYD_ROTATION
#define CYD_ROTATION 1
#endif
#ifndef CARD_ROTATE_MS
#define CARD_ROTATE_MS 12000
#endif
#ifndef DEFAULT_BRIGHTNESS
#define DEFAULT_BRIGHTNESS 220
#endif

#define FW_VERSION "1.0.0"
#define BL_CHANNEL 0
#define MAX_CARDS 8
#define MAX_LINE 6144  // longest accepted JSON line (bytes)

TFT_eSPI tft;
SPIClass touchSpi(VSPI);
XPT2046_Touchscreen ts(XPT2046_CS, XPT2046_IRQ);
Preferences prefs;

// ---------- Colours (high contrast) ----------
static const uint16_t COL_BG = TFT_BLACK;
static const uint16_t COL_TEXT = TFT_WHITE;
static const uint16_t COL_ACCENT = 0xFD20;  // orange
static const uint16_t COL_DIM = 0x8410;     // grey

struct Card {
  String type;   // title | instructions | cost | idle | custom
  String title;
  String text;
};

struct State {
  bool idle = true;
  String tableTitle;
  Card cards[MAX_CARDS];
  int cardCount = 0;
  int current = 0;
  uint8_t brightness = DEFAULT_BRIGHTNESS;
  uint8_t rotation = CYD_ROTATION;
} st;

String rxLine;
bool rxOverflow = false;
unsigned long lastRotate = 0;
unsigned long lastTouch = 0;
unsigned long idleAnimT = 0;
int idleAnimPhase = 0;

// ---------- Helpers ----------
uint16_t headerColourFor(const String &type) {
  if (type == "title") return TFT_RED;
  if (type == "instructions" || type == "rules") return TFT_BLUE;
  if (type == "cost") return 0x03E0;  // dark green
  if (type == "idle") return TFT_PURPLE;
  return 0x39E7;  // dark grey
}

void setBrightness(uint8_t v) {
  st.brightness = v;
  ledcWrite(BL_CHANNEL, v);
}

// Word-wrap text inside a box using the currently selected font.
// Honours '\n'. Returns the y after the last line.
int drawWrapped(const String &text, int x, int y, int w, int maxY, int lineH, bool dryRun = false) {
  int start = 0;
  const int n = text.length();
  while (start < n && (dryRun || y + lineH <= maxY + 2)) {
    // hard line break
    int nl = text.indexOf('\n', start);
    int paraEnd = (nl < 0) ? n : nl;
    String para = text.substring(start, paraEnd);
    if (para.length() == 0) {
      y += lineH / 2;
    } else {
      int p = 0;
      while (p < (int)para.length() && (dryRun || y + lineH <= maxY + 2)) {
        int lastFit = -1;
        int i = p;
        while (true) {
          int sp = para.indexOf(' ', i);
          int end = (sp < 0) ? para.length() : sp;
          if (tft.textWidth(para.substring(p, end)) <= w) {
            lastFit = end;
            if (sp < 0) break;
            i = sp + 1;
          } else {
            break;
          }
        }
        if (lastFit < 0) {
          // single word longer than the line: hard split
          int end = p + 1;
          while (end < (int)para.length() && tft.textWidth(para.substring(p, end + 1)) <= w) end++;
          lastFit = end;
        }
        if (!dryRun) tft.drawString(para.substring(p, lastFit), x, y);
        y += lineH;
        p = lastFit;
        while (p < (int)para.length() && para[p] == ' ') p++;
      }
    }
    start = paraEnd + 1;
  }
  return y;
}

// Picks the largest font where the text fits the box.
void drawFittedText(const String &text, int x, int y, int w, int h, uint16_t colour) {
  const GFXfont *fonts[] = {&FreeSansBold18pt7b, &FreeSansBold12pt7b, &FreeSans12pt7b, &FreeSans9pt7b};
  const int lineHs[] = {34, 26, 26, 20};
  tft.setTextColor(colour, COL_BG);
  tft.setTextDatum(TL_DATUM);
  for (int f = 0; f < 4; f++) {
    tft.setFreeFont(fonts[f]);
    int usedH = drawWrapped(text, x, y, w, y + h, lineHs[f], true) - y;
    if (usedH <= h || f == 3) {
      drawWrapped(text, x, y, w, y + h, lineHs[f]);
      return;
    }
  }
}

void drawHeader(const String &label, uint16_t colour) {
  int W = tft.width();
  tft.fillRect(0, 0, W, 36, colour);
  tft.setTextColor(TFT_WHITE, colour);
  tft.setFreeFont(&FreeSansBold12pt7b);
  tft.setTextDatum(ML_DATUM);
  String s = label;
  while (s.length() > 1 && tft.textWidth(s) > W - 60) s.remove(s.length() - 1);
  tft.drawString(s, 8, 18);
  // page indicator
  if (!st.idle && st.cardCount > 1) {
    tft.setTextFont(2);
    tft.setTextDatum(MR_DATUM);
    tft.drawString(String(st.current + 1) + "/" + String(st.cardCount), W - 8, 18);
  }
  tft.setTextDatum(TL_DATUM);
}

void drawIdle() {
  int W = tft.width(), H = tft.height();
  tft.fillScreen(COL_BG);
  drawHeader("PINBALL", TFT_PURPLE);
  tft.setTextDatum(MC_DATUM);
  tft.setTextColor(COL_ACCENT, COL_BG);
  tft.setFreeFont(&FreeSansBold24pt7b);
  tft.drawString("INSERT", W / 2, H / 2 - 22);
  tft.drawString("COIN", W / 2, H / 2 + 26);
  tft.setTextColor(COL_DIM, COL_BG);
  tft.setFreeFont(&FreeSans9pt7b);
  tft.drawString("Select a table to begin", W / 2, H - 22);
  tft.setTextDatum(TL_DATUM);
}

void drawIdleBlink() {
  // simple attract animation: blinking border
  int W = tft.width(), H = tft.height();
  uint16_t c = (idleAnimPhase & 1) ? COL_ACCENT : COL_BG;
  tft.drawRect(4, 40, W - 8, H - 44, c);
  tft.drawRect(5, 41, W - 10, H - 46, c);
}

void drawCard() {
  if (st.idle || st.cardCount == 0) {
    drawIdle();
    return;
  }
  int W = tft.width(), H = tft.height();
  const Card &c = st.cards[st.current];
  tft.fillScreen(COL_BG);
  String hdr = c.title.length() ? c.title : st.tableTitle;
  drawHeader(hdr, headerColourFor(c.type));

  const int pad = 8, top = 44;
  if (c.type == "title") {
    // Big title, wrapped with the largest font that fits
    String t = c.text.length() ? c.text : st.tableTitle;
    drawFittedText(t, pad, top + 6, W - 2 * pad, H - top - 12, COL_ACCENT);
  } else if (c.type == "cost") {
    // Cost card: each line of text as a big row, e.g. "1 CREDIT = 25c\n3 BALLS"
    tft.setTextColor(TFT_YELLOW, COL_BG);
    tft.setTextDatum(MC_DATUM);
    int lines = 1;
    for (char ch : c.text) if (ch == '\n') lines++;
    tft.setFreeFont(lines <= 3 ? &FreeSansBold18pt7b : &FreeSansBold12pt7b);
    int lh = lines <= 3 ? 44 : 30;
    int y = top + (H - top - lines * lh) / 2 + lh / 2;
    int s = 0;
    for (int i = 0; i < lines; i++) {
      int nl = c.text.indexOf('\n', s);
      String line = c.text.substring(s, nl < 0 ? c.text.length() : nl);
      // shrink font for this line if it is too wide
      if (tft.textWidth(line) > W - 2 * pad) tft.setFreeFont(&FreeSansBold12pt7b);
      if (tft.textWidth(line) > W - 2 * pad) tft.setFreeFont(&FreeSans9pt7b);
      tft.drawString(line, W / 2, y);
      tft.setFreeFont(lines <= 3 ? &FreeSansBold18pt7b : &FreeSansBold12pt7b);
      y += lh;
      s = nl + 1;
    }
    tft.setTextDatum(TL_DATUM);
  } else {
    // instructions / rules / custom: word-wrapped body
    tft.setTextColor(COL_TEXT, COL_BG);
    tft.setTextDatum(TL_DATUM);
    int len = c.text.length();
    if (len < 160) {
      tft.setFreeFont(&FreeSansBold12pt7b);
      drawWrapped(c.text, pad, top + 4, W - 2 * pad, H - 4, 27);
    } else {
      tft.setFreeFont(&FreeSans12pt7b);
      int need = drawWrapped(c.text, pad, top + 4, W - 2 * pad, H - 4, 25, true);
      if (need > H - 4) tft.setFreeFont(&FreeSans9pt7b);  // long rules: smaller font
      drawWrapped(c.text, pad, top + 4, W - 2 * pad, H - 4, need > H - 4 ? 20 : 25);
    }
  }
}

void nextCard() {
  if (st.idle || st.cardCount == 0) return;
  st.current = (st.current + 1) % st.cardCount;
  drawCard();
}

// ---------- Persistence ----------
void saveTable(const String &json) {
  prefs.begin("cards", false);
  // stored as a blob: NVS strings are limited to ~4000 bytes, blobs are not
  prefs.putBytes("last", json.c_str(), json.length());
  prefs.putBool("idle", false);
  prefs.end();
}

void saveIdle(bool idle) {
  prefs.begin("cards", false);
  prefs.putBool("idle", idle);
  prefs.end();
}

void saveSetting(const char *key, uint8_t v) {
  prefs.begin("cards", false);
  prefs.putUChar(key, v);
  prefs.end();
}

bool loadTableFromDoc(JsonDocument &doc) {
  const char *title = doc["title"] | "";
  JsonArray arr = doc["cards"].as<JsonArray>();
  st.tableTitle = title;
  st.cardCount = 0;
  if (!arr.isNull()) {
    for (JsonObject o : arr) {
      if (st.cardCount >= MAX_CARDS) break;
      Card &c = st.cards[st.cardCount++];
      c.type = (const char *)(o["type"] | "instructions");
      c.title = (const char *)(o["title"] | "");
      c.text = (const char *)(o["text"] | "");
    }
  }
  if (st.cardCount == 0) {
    // no cards given: synthesise a title card
    Card &c = st.cards[st.cardCount++];
    c.type = "title";
    c.title = "NOW PLAYING";
    c.text = st.tableTitle;
  }
  st.current = 0;
  st.idle = false;
  return true;
}

// ---------- Serial ----------
void reply(const char *cmd, bool ok, const char *err = nullptr, const char *extraKey = nullptr, int extraVal = 0) {
  JsonDocument r;
  r["ack"] = cmd;
  r["ok"] = ok;
  if (err) r["err"] = err;
  if (extraKey) r[extraKey] = extraVal;
  serializeJson(r, Serial);
  Serial.println();
}

void handleLine(const String &line) {
  JsonDocument doc;
  DeserializationError e = deserializeJson(doc, line);
  if (e) {
    reply("?", false, e.c_str());
    return;
  }
  const char *cmd = doc["cmd"] | "";
  if (!strcmp(cmd, "table")) {
    loadTableFromDoc(doc);
    saveTable(line);
    drawCard();
    lastRotate = millis();
    reply("table", true, nullptr, "cards", st.cardCount);
  } else if (!strcmp(cmd, "idle")) {
    st.idle = true;
    saveIdle(true);
    drawCard();
    reply("idle", true);
  } else if (!strcmp(cmd, "brightness")) {
    int v = doc["value"] | -1;
    if (v < 0 || v > 255) { reply("brightness", false, "value must be 0-255"); return; }
    setBrightness((uint8_t)v);
    saveSetting("bright", (uint8_t)v);
    reply("brightness", true, nullptr, "value", v);
  } else if (!strcmp(cmd, "rotation")) {
    int v = doc["value"] | -1;
    if (v < 0 || v > 3) { reply("rotation", false, "value must be 0-3"); return; }
    st.rotation = v;
    tft.setRotation(v);
    ts.setRotation(v);
    saveSetting("rot", (uint8_t)v);
    drawCard();
    reply("rotation", true, nullptr, "value", v);
  } else if (!strcmp(cmd, "next")) {
    nextCard();
    reply("next", true, nullptr, "card", st.current);
  } else if (!strcmp(cmd, "ping")) {
    JsonDocument r;
    r["ack"] = "ping";
    r["ok"] = true;
    r["fw"] = FW_VERSION;
    r["device"] = "cyd-pinball-cards";
    serializeJson(r, Serial);
    Serial.println();
  } else {
    reply(cmd[0] ? cmd : "?", false, "unknown cmd");
  }
}

void pollSerial() {
  while (Serial.available()) {
    char ch = (char)Serial.read();
    if (ch == '\r') continue;
    if (ch == '\n') {
      if (rxOverflow) {
        reply("?", false, "line too long");
      } else if (rxLine.length()) {
        handleLine(rxLine);
      }
      rxLine = "";
      rxOverflow = false;
    } else if (!rxOverflow) {
      if (rxLine.length() >= MAX_LINE) rxOverflow = true;
      else rxLine += ch;
    }
  }
}

// ---------- Touch ----------
void pollTouch() {
  if (ts.tirqTouched() && ts.touched()) {
    unsigned long now = millis();
    if (now - lastTouch > 350) {  // debounce
      lastTouch = now;
      nextCard();
      lastRotate = now;  // restart auto-rotate timer after manual tap
    }
  }
}

// ---------- Setup / loop ----------
void setup() {
  Serial.begin(115200);
  rxLine.reserve(1024);

  prefs.begin("cards", true);
  st.brightness = prefs.getUChar("bright", DEFAULT_BRIGHTNESS);
  st.rotation = prefs.getUChar("rot", CYD_ROTATION);
  bool wasIdle = prefs.getBool("idle", true);
  String last;
  size_t lastLen = prefs.getBytesLength("last");
  if (lastLen > 0 && lastLen <= MAX_LINE) {
    char *buf = (char *)malloc(lastLen + 1);
    if (buf) {
      prefs.getBytes("last", buf, lastLen);
      buf[lastLen] = 0;
      last = buf;
      free(buf);
    }
  }
  prefs.end();

  // Backlight PWM (Arduino-ESP32 core 2.x API)
  ledcSetup(BL_CHANNEL, 5000, 8);
  ledcAttachPin(TFT_BL, BL_CHANNEL);
  setBrightness(st.brightness);

  tft.init();
  tft.setRotation(st.rotation);
  tft.fillScreen(COL_BG);

  touchSpi.begin(XPT2046_CLK, XPT2046_MISO, XPT2046_MOSI, XPT2046_CS);
  ts.begin(touchSpi);
  ts.setRotation(st.rotation);

  if (!wasIdle && last.length()) {
    JsonDocument doc;
    if (!deserializeJson(doc, last)) loadTableFromDoc(doc);
  }
  drawCard();
  lastRotate = millis();
  Serial.println("{\"ready\":true,\"device\":\"cyd-pinball-cards\",\"fw\":\"" FW_VERSION "\"}");
}

void loop() {
  pollSerial();
  pollTouch();
  unsigned long now = millis();
  if (!st.idle && st.cardCount > 1 && CARD_ROTATE_MS > 0 && now - lastRotate >= CARD_ROTATE_MS) {
    lastRotate = now;
    nextCard();
  }
  if (st.idle && now - idleAnimT >= 700) {
    idleAnimT = now;
    idleAnimPhase++;
    drawIdleBlink();
  }
  delay(5);
}
