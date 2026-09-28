// CYD Pinball Cards - companion card display for a virtual pinball cabinet
// Board: ESP32-2432S028R (Cheap Yellow Display)
//
// Serial protocol (115200 baud, newline-delimited JSON, one object per line):
//   {"cmd":"table","title":"Medieval Madness","ts":<local epoch s, optional>,"cards":[{"type":"instructions","title":"Rules","text":"..."}, ...]}
//   {"cmd":"idle"}        bare idle: attract playlist using the saved (or built-in default) idle config
//   {"cmd":"idle","ts":<local epoch s>,"cabinet":"Crews Pinball","screens":[...],"selected":"Table"}
//                         full idle: config comes from cards/_idle.json via cyd_push.py --idle (see README)
//   {"cmd":"brightness","value":0-255}
//   {"cmd":"rotation","value":0-3}
//   {"cmd":"next"}
//   {"cmd":"ping"}
// Replies: {"ack":"<cmd>","ok":true[, ...]} or {"ack":"<cmd>","ok":false,"err":"..."}
//
// "ts" is the host's LOCAL wall-clock time as seconds since 1970-01-01 00:00 (i.e. local time
// encoded as if it were UTC). The ESP32 has no RTC, so the clock only runs after a host sent it,
// and it is advanced locally with millis().

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
#ifndef DEFAULT_CABINET
#define DEFAULT_CABINET "Crews Pinball"
#endif

#define FW_VERSION "1.1.0"
#define BL_CHANNEL 0
#define MAX_CARDS 8
#define MAX_IDLE_SCREENS 12
#define MAX_LINE 6144         // longest accepted JSON line (bytes)
#define RX_BUFFER (MAX_LINE + 512)  // UART RX ring buffer: a whole line fits even while drawing

TFT_eSPI tft;
SPIClass touchSpi(VSPI);
XPT2046_Touchscreen ts(XPT2046_CS, XPT2046_IRQ);
Preferences prefs;

// ---------- Colours (high contrast) ----------
static const uint16_t COL_BG = TFT_BLACK;
static const uint16_t COL_TEXT = TFT_WHITE;
static const uint16_t COL_ACCENT = 0xFD20;  // orange
static const uint16_t COL_DIM = 0x8410;     // grey
static const uint16_t COL_DARK = 0x2104;    // very dark grey
static const uint16_t COL_GOLD = 0xFEA0;
static const uint16_t COL_CYAN = 0x07FF;
static const uint16_t COL_RED = 0xF800;
static const uint16_t COL_GREEN = 0x07E0;
static const uint16_t COL_YELLOW = 0xFFE0;
static const uint16_t COL_MAGENTA = 0xF81F;

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

// ---------- Idle / attract playlist ----------
enum IdleType : uint8_t { IS_MARQUEE, IS_CHOOSE, IS_CLOCK, IS_RULES, IS_PRICING, IS_ANIM, IS_LAST, IS_UPNEXT, IS_TEXT };

struct IdleScreen {
  IdleType type = IS_MARQUEE;
  String title;
  String text;
  uint16_t durS = 0;   // 0 = use default
  uint8_t style = 0;   // IS_ANIM: 0 = pinball, 1 = stars
};

struct IdleConfig {
  String cabinet = DEFAULT_CABINET;
  String subtitle = "VIRTUAL PINBALL";
  bool h24 = false;
  uint16_t defDurS = 10;
  uint16_t autoIdleMin = 0;  // 0 = off; else table -> idle after N minutes
  IdleScreen screens[MAX_IDLE_SCREENS];
  int count = 0;
} icfg;

struct IdleRuntime {
  int idx = 0;
  unsigned long start = 0;     // millis when current screen was drawn
  unsigned long tickT = 0;     // last animation tick
  uint32_t phase = 0;
  uint32_t cycle = 0;          // increments each full screen change (burn-in shift)
  int ox = 0, oy = 0;          // current content offset
  int lastMinute = -1;
  bool colonOn = true;
} irt;

// wall clock (no RTC): base local-epoch seconds + millis() at which it was set
bool clockValid = false;
uint32_t clockBase = 0;
unsigned long clockMillis = 0;

String lastTitle;      // last table played (persisted)
uint32_t lastTs = 0;   // local epoch of last table start, 0 = unknown (persisted)
String selectedTable;  // "up next" hint from host (not persisted)
unsigned long tableStartMs = 0;

String rxLine;
bool rxOverflow = false;
unsigned long lastRotate = 0;
unsigned long lastTouch = 0;

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

// Split text into lines that fit width w with the current font. Returns line count.
int wrapLines(const String &text, int w, String *out, int maxLines) {
  int count = 0, start = 0;
  const int n = text.length();
  while (start <= n && count < maxLines) {
    int nl = text.indexOf('\n', start);
    int paraEnd = (nl < 0) ? n : nl;
    String para = text.substring(start, paraEnd);
    para.trim();
    if (para.length() == 0) {
      if (nl >= 0) out[count++] = "";
    }
    int p = 0;
    while (p < (int)para.length() && count < maxLines) {
      int lastFit = -1, i = p;
      while (true) {
        int sp = para.indexOf(' ', i);
        int end = (sp < 0) ? para.length() : sp;
        if (tft.textWidth(para.substring(p, end)) <= w) {
          lastFit = end;
          if (sp < 0) break;
          i = sp + 1;
        } else break;
      }
      if (lastFit < 0) {
        int end = p + 1;
        while (end < (int)para.length() && tft.textWidth(para.substring(p, end + 1)) <= w) end++;
        lastFit = end;
      }
      out[count++] = para.substring(p, lastFit);
      p = lastFit;
      while (p < (int)para.length() && para[p] == ' ') p++;
    }
    if (nl < 0) break;
    start = paraEnd + 1;
  }
  return count;
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

// Centred, largest-font-that-fits text block (each line centred on cx) inside a w x h box whose
// vertical centre is cy. Returns the font height step used.
int drawCenteredFit(const String &text, int cx, int cy, int w, int h, uint16_t colour, int maxFont = 0,
                    uint16_t shadow = 0) {
  const GFXfont *fonts[] = {&FreeSansBold24pt7b, &FreeSansBold18pt7b, &FreeSansBold12pt7b, &FreeSans9pt7b};
  const int lineHs[] = {46, 36, 28, 20};
  String lines[6];
  int f = maxFont, n = 0;
  for (; f < 4; f++) {
    tft.setFreeFont(fonts[f]);
    n = wrapLines(text, w, lines, 6);
    bool wordSplit = false;  // avoid breaking words mid-way when a smaller font would avoid it
    int ws = 0;
    for (int i = 0; i <= (int)text.length(); i++) {
      if (i == (int)text.length() || text[i] == ' ' || text[i] == '\n') {
        if (i > ws && tft.textWidth(text.substring(ws, i)) > w) wordSplit = true;
        ws = i + 1;
      }
    }
    if (n * lineHs[f] <= h && (!wordSplit || f == 3)) break;
    if (f == 3) break;
  }
  if (f > 3) f = 3;
  tft.setFreeFont(fonts[f]);
  tft.setTextDatum(MC_DATUM);
  int y = cy - (n * lineHs[f]) / 2 + lineHs[f] / 2;
  for (int i = 0; i < n; i++) {
    if (shadow) {
      tft.setTextColor(shadow);
      tft.drawString(lines[i], cx + 2, y + 2);
    }
    tft.setTextColor(colour);
    tft.drawString(lines[i], cx, y);
    y += lineHs[f];
  }
  tft.setTextDatum(TL_DATUM);
  return lineHs[f];
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

// ---------- Clock ----------
uint32_t clockNow() { return clockBase + (millis() - clockMillis) / 1000UL; }

void setClock(uint32_t t) {
  if (t < 946684800UL) return;  // ignore anything before 2000-01-01
  clockBase = t;
  clockMillis = millis();
  clockValid = true;
}

struct CivilTime { int y, mo, d, h, mi, s, wd; };  // wd: 0 = Sunday

CivilTime civil(uint32_t t) {
  CivilTime c;
  int32_t days = t / 86400UL;
  uint32_t sod = t % 86400UL;
  c.h = sod / 3600; c.mi = (sod / 60) % 60; c.s = sod % 60;
  c.wd = (days + 4) % 7;  // 1970-01-01 was a Thursday
  // Howard Hinnant's civil_from_days
  int32_t z = days + 719468;
  int32_t era = (z >= 0 ? z : z - 146096) / 146097;
  uint32_t doe = (uint32_t)(z - era * 146097);
  uint32_t yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
  int32_t y = (int32_t)yoe + era * 400;
  uint32_t doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
  uint32_t mp = (5 * doy + 2) / 153;
  c.d = doy - (153 * mp + 2) / 5 + 1;
  c.mo = mp < 10 ? mp + 3 : mp - 9;
  c.y = y + (c.mo <= 2);
  return c;
}

static const char *WDAYS[] = {"Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"};
static const char *MONTHS[] = {"January", "February", "March", "April", "May", "June", "July",
                               "August", "September", "October", "November", "December"};

String fmtTime(const CivilTime &c, bool withAmPm = true) {
  char buf[16];
  if (icfg.h24) {
    snprintf(buf, sizeof buf, "%02d:%02d", c.h, c.mi);
  } else {
    int h12 = c.h % 12; if (h12 == 0) h12 = 12;
    if (withAmPm) snprintf(buf, sizeof buf, "%d:%02d %s", h12, c.mi, c.h < 12 ? "AM" : "PM");
    else snprintf(buf, sizeof buf, "%d:%02d", h12, c.mi);
  }
  return String(buf);
}

// ---------- Idle config ----------
IdleType parseIdleType(const String &t, uint8_t &style) {
  style = 0;
  if (t == "marquee" || t == "logo" || t == "title" || t == "cabinet") return IS_MARQUEE;
  if (t == "choose" || t == "pick" || t == "pick_table" || t == "prompt") return IS_CHOOSE;
  if (t == "clock" || t == "time") return IS_CLOCK;
  if (t == "rules" || t == "house_rules" || t == "instructions") return IS_RULES;
  if (t == "pricing" || t == "cost" || t == "price") return IS_PRICING;
  if (t == "stars" || t == "starfield") { style = 1; return IS_ANIM; }
  if (t == "anim" || t == "animation" || t == "pinball" || t == "ball") return IS_ANIM;
  if (t == "last_played" || t == "last" || t == "lastplayed") return IS_LAST;
  if (t == "up_next" || t == "upnext" || t == "selected") return IS_UPNEXT;
  return IS_TEXT;
}

void addScreen(IdleType t, const char *title, const char *text, uint16_t dur, uint8_t style = 0) {
  if (icfg.count >= MAX_IDLE_SCREENS) return;
  IdleScreen &s = icfg.screens[icfg.count++];
  s.type = t; s.title = title; s.text = text; s.durS = dur; s.style = style;
}

void defaultIdleScreens() {
  icfg.count = 0;
  addScreen(IS_MARQUEE, "", "", 12);
  addScreen(IS_CHOOSE, "PICK A TABLE", "Now choosing...", 10);
  addScreen(IS_CLOCK, "", "", 10);
  addScreen(IS_RULES, "HOUSE RULES", "Have fun!\nNo drinks on the glass.\nGive the next player a turn.", 12);
  addScreen(IS_PRICING, "PRICING", "FREE PLAY\nPRESS START", 8);
  addScreen(IS_ANIM, "", "", 20, 0);
  addScreen(IS_LAST, "LAST PLAYED", "", 8);
}

void applyIdleConfig(JsonDocument &doc) {
  icfg.cabinet = (const char *)(doc["cabinet"] | DEFAULT_CABINET);
  icfg.subtitle = (const char *)(doc["subtitle"] | "VIRTUAL PINBALL");
  icfg.h24 = doc["clock_24h"] | false;
  int d = doc["duration"] | 10;
  icfg.defDurS = constrain(d, 2, 3600);
  int a = doc["auto_idle_min"] | 0;
  icfg.autoIdleMin = constrain(a, 0, 1440);
  JsonArray arr = doc["screens"].as<JsonArray>();
  icfg.count = 0;
  if (!arr.isNull()) {
    for (JsonVariant v : arr) {
      if (icfg.count >= MAX_IDLE_SCREENS) break;
      IdleScreen &s = icfg.screens[icfg.count];
      String type;
      if (v.is<const char *>()) {  // allow "clock" shorthand
        type = v.as<const char *>();
        s.title = ""; s.text = ""; s.durS = 0;
      } else if (v.is<JsonObject>()) {
        JsonObject o = v.as<JsonObject>();
        if (o["enabled"].is<bool>() && !o["enabled"].as<bool>()) continue;
        type = (const char *)(o["type"] | "text");
        s.title = (const char *)(o["title"] | "");
        s.text = (const char *)(o["text"] | "");
        int du = o["duration"] | 0;
        s.durS = constrain(du, 0, 3600);
        String style = (const char *)(o["style"] | "");
        if (style.length()) type = (style == "stars" || style == "starfield") ? "stars" : type;
      } else continue;
      s.type = parseIdleType(type, s.style);
      icfg.count++;
    }
  }
  if (icfg.count == 0) defaultIdleScreens();
  // make sure an "up next" slot exists; it is only shown while the host sent a "selected" table
  bool hasUpNext = false;
  for (int i = 0; i < icfg.count; i++) hasUpNext |= icfg.screens[i].type == IS_UPNEXT;
  if (!hasUpNext) addScreen(IS_UPNEXT, "UP NEXT", "", 0);
}

void loadDefaultIdleConfig() {
  JsonDocument doc;
  applyIdleConfig(doc);
}

// ---------- Idle screens ----------
static const int8_t SHIFTS[][2] = {{0, 0}, {5, 3}, {-5, 2}, {3, -3}, {-3, -2}, {6, 0}, {-6, 1}, {0, 4}, {2, -4}};

uint16_t screenDurMs(const IdleScreen &s) {
  uint32_t d = s.durS ? s.durS : icfg.defDurS;
  if (s.type == IS_ANIM && !s.durS) d = max<uint32_t>(d, 20);
  return (uint16_t)min<uint32_t>(d * 1000UL, 65000UL);
}

bool screenUsable(const IdleScreen &s) {
  switch (s.type) {
    case IS_CLOCK: return clockValid;
    case IS_LAST: return lastTitle.length() > 0;
    case IS_UPNEXT: return selectedTable.length() > 0;
    default: return true;
  }
}

// Title line used by the text-style idle screens: coloured text + underline, shifted with content.
int drawIdleTitle(const String &label, uint16_t colour) {
  int W = tft.width();
  if (!label.length()) return 10 + irt.oy;
  tft.setFreeFont(&FreeSansBold18pt7b);
  String s = label;
  if (tft.textWidth(s) > W - 24) tft.setFreeFont(&FreeSansBold12pt7b);
  while (s.length() > 1 && tft.textWidth(s) > W - 24) s.remove(s.length() - 1);
  tft.setTextColor(colour);
  tft.setTextDatum(TC_DATUM);
  int y = 10 + irt.oy;
  tft.drawString(s, W / 2 + irt.ox, y);
  int tw = tft.textWidth(s);
  tft.fillRect(W / 2 + irt.ox - tw / 2, y + 36, tw, 3, colour);
  tft.setTextDatum(TL_DATUM);
  return y + 46;
}

// ---- marquee: cabinet name with chasing bulbs ----
int bulbCount() {
  int W = tft.width(), H = tft.height();
  return 2 * ((W - 12) / 20) + 2 * ((H - 12) / 20);
}

void bulbPos(int i, int &x, int &y) {
  int W = tft.width(), H = tft.height();
  int nx = (W - 12) / 20, ny = (H - 12) / 20;
  int stepX = (W - 12) / nx, stepY = (H - 12) / ny;
  if (i < nx) { x = 6 + i * stepX; y = 6; return; }
  i -= nx;
  if (i < ny) { x = W - 7; y = 6 + i * stepY; return; }
  i -= ny;
  if (i < nx) { x = W - 7 - i * stepX; y = H - 7; return; }
  i -= nx;
  x = 6; y = H - 7 - i * stepY;
}

void drawBulbs() {
  int n = bulbCount();
  for (int i = 0; i < n; i++) {
    int x, y;
    bulbPos(i, x, y);
    bool lit = ((i + irt.phase) % 3) == 0;
    tft.fillCircle(x, y, 4, lit ? COL_GOLD : 0x4100);
  }
}

void drawMarquee(const IdleScreen &s) {
  int W = tft.width(), H = tft.height();
  drawBulbs();
  // inner frame
  tft.drawRoundRect(16, 16, W - 32, H - 32, 8, COL_RED);
  tft.drawRoundRect(18, 18, W - 36, H - 36, 7, 0x7800);
  String name = s.title.length() ? s.title : icfg.cabinet;
  String sub = s.text.length() ? s.text : icfg.subtitle;
  int cx = W / 2 + irt.ox, cy = H / 2 - 14 + irt.oy;
  drawCenteredFit(name, cx, cy, W - 56, H - 110, COL_GOLD, 0, COL_RED);
  if (sub.length()) {
    tft.setFreeFont(&FreeSansBold12pt7b);
    if (tft.textWidth(sub) > W - 56) tft.setFreeFont(&FreeSans9pt7b);
    tft.setTextColor(COL_CYAN);
    tft.setTextDatum(MC_DATUM);
    tft.drawString(sub, cx, H - 52 + irt.oy);
    tft.setTextDatum(TL_DATUM);
  }
}

void tickMarquee() {
  irt.phase++;
  drawBulbs();
}

// ---- choose: "Pick a table" prompt with sweeping lane arrows ----
void drawChevrons() {
  int W = tft.width(), H = tft.height();
  const int n = 7, cw = 26;
  int x0 = W / 2 - (n * cw) / 2 + irt.ox, y = H - 44 + irt.oy;
  int lit = irt.phase % (n + 3);
  for (int i = 0; i < n; i++) {
    uint16_t c = (i == lit) ? COL_ACCENT : (i == lit - 1 ? 0x7A00 : COL_DARK);
    int x = x0 + i * cw;
    tft.fillTriangle(x, y - 12, x + 16, y, x, y + 12, c);
    tft.fillTriangle(x, y - 6, x + 8, y, x, y + 6, COL_BG);
  }
}

void drawChoose(const IdleScreen &s) {
  int W = tft.width(), H = tft.height();
  String title = s.title.length() ? s.title : "PICK A TABLE";
  String text = s.text.length() ? s.text : "Now choosing...";
  drawCenteredFit(title, W / 2 + irt.ox, 70 + irt.oy, W - 30, 100, COL_ACCENT, 0);
  tft.setTextColor(COL_TEXT);
  drawCenteredFit(text, W / 2 + irt.ox, 142 + irt.oy, W - 30, 50, COL_TEXT, 2);
  drawChevrons();
}

void tickChoose() {
  irt.phase++;
  drawChevrons();
}

// ---- clock ----
void drawClockTime(bool full) {
  int W = tft.width();
  CivilTime c = civil(clockNow());
  int hr = icfg.h24 ? c.h : ((c.h % 12) ? c.h % 12 : 12);
  char hh[4], mm[4];
  snprintf(hh, sizeof hh, icfg.h24 ? "%02d" : "%d", hr);
  snprintf(mm, sizeof mm, "%02d", c.mi);
  uint8_t font = 8;
  tft.setTextFont(font);
  int wDigits = tft.textWidth("00"), wColon = tft.textWidth(":");
  int ampmW = icfg.h24 ? 0 : 44;
  if (2 * wDigits + wColon + ampmW > W - 20) {
    font = 7;
    tft.setTextFont(font);
    wDigits = tft.textWidth("00");
    wColon = tft.textWidth(":");
  }
  int fh = tft.fontHeight(font);
  int total = 2 * wDigits + wColon + ampmW;
  int x0 = W / 2 - total / 2 + irt.ox;
  int y = 30 + irt.oy;
  int colonX = x0 + wDigits;
  if (full || c.mi != irt.lastMinute) {
    tft.setTextColor(COL_TEXT, COL_BG);
    tft.setTextPadding(wDigits);
    tft.setTextDatum(TR_DATUM);
    tft.drawString(hh, colonX, y);
    tft.setTextDatum(TL_DATUM);
    tft.drawString(mm, colonX + wColon, y);
    tft.setTextPadding(0);
    if (!icfg.h24) {
      tft.setFreeFont(&FreeSansBold12pt7b);
      tft.setTextColor(COL_ACCENT, COL_BG);
      tft.setTextPadding(ampmW);
      tft.drawString(c.h < 12 ? "AM" : "PM", colonX + wColon + wDigits + 6, y + fh - 26);
      tft.setTextPadding(0);
    }
    if (full || c.mi != irt.lastMinute) {
      // date lines
      int H = tft.height();
      tft.fillRect(0, y + fh + 6, W, H - (y + fh + 6), COL_BG);
      tft.setTextDatum(TC_DATUM);
      tft.setFreeFont(&FreeSansBold18pt7b);
      tft.setTextColor(COL_ACCENT);
      tft.drawString(WDAYS[c.wd], W / 2 + irt.ox, y + fh + 14);
      char date[32];
      snprintf(date, sizeof date, "%s %d, %d", MONTHS[c.mo - 1], c.d, c.y);
      tft.setFreeFont(&FreeSansBold12pt7b);
      tft.setTextColor(COL_TEXT);
      tft.drawString(date, W / 2 + irt.ox, y + fh + 56);
      tft.setFreeFont(&FreeSans9pt7b);
      tft.setTextColor(COL_DIM);
      tft.drawString(icfg.cabinet, W / 2 + irt.ox, y + fh + 90);
      tft.setTextDatum(TL_DATUM);
    }
    irt.lastMinute = c.mi;
  }
  tft.setTextFont(font);
  tft.setTextColor(irt.colonOn ? COL_TEXT : COL_BG, COL_BG);
  tft.drawString(":", colonX, y);
}

void drawClock(const IdleScreen &) {
  irt.lastMinute = -1;
  irt.colonOn = true;
  drawClockTime(true);
}

void tickClock() {
  irt.colonOn = !irt.colonOn;
  drawClockTime(false);
}

// ---- rules / pricing / text ----
void drawRules(const IdleScreen &s, const char *defTitle, uint16_t titleCol) {
  int W = tft.width(), H = tft.height();
  int top = drawIdleTitle(s.title.length() ? s.title : defTitle, titleCol);
  const int pad = 14;
  tft.setTextColor(COL_TEXT, COL_BG);
  tft.setTextDatum(TL_DATUM);
  int x = pad + irt.ox, w = W - 2 * pad, maxY = H - 8;
  tft.setFreeFont(&FreeSansBold12pt7b);
  int need = drawWrapped(s.text, x, top + 4, w, maxY, 28, true);
  if (need > maxY) {
    tft.setFreeFont(&FreeSans12pt7b);
    need = drawWrapped(s.text, x, top + 4, w, maxY, 25, true);
    if (need > maxY) {
      tft.setFreeFont(&FreeSans9pt7b);
      drawWrapped(s.text, x, top + 2, w, maxY, 20);
      return;
    }
    drawWrapped(s.text, x, top + 4, w, maxY, 25);
    return;
  }
  drawWrapped(s.text, x, top + 4, w, maxY, 28);
}

void drawPricing(const IdleScreen &s) {
  int W = tft.width(), H = tft.height();
  int top = drawIdleTitle(s.title.length() ? s.title : "PRICING", COL_GREEN);
  String text = s.text.length() ? s.text : "FREE PLAY";
  int lines = 1;
  for (char ch : text) if (ch == '\n') lines++;
  const GFXfont *big = lines <= 3 ? &FreeSansBold24pt7b : &FreeSansBold12pt7b;
  int lh = lines <= 3 ? 50 : 30;
  if (lines == 3) { big = &FreeSansBold18pt7b; lh = 42; }
  int y = top + (H - top - lines * lh) / 2 + lh / 2;
  tft.setTextColor(COL_YELLOW);
  tft.setTextDatum(MC_DATUM);
  int s0 = 0;
  for (int i = 0; i < lines; i++) {
    int nl = text.indexOf('\n', s0);
    String line = text.substring(s0, nl < 0 ? text.length() : nl);
    tft.setFreeFont(big);
    if (tft.textWidth(line) > W - 24) tft.setFreeFont(&FreeSansBold18pt7b);
    if (tft.textWidth(line) > W - 24) tft.setFreeFont(&FreeSansBold12pt7b);
    if (tft.textWidth(line) > W - 24) tft.setFreeFont(&FreeSans9pt7b);
    tft.drawString(line, W / 2 + irt.ox, y);
    y += lh;
    s0 = nl + 1;
  }
  tft.setTextDatum(TL_DATUM);
}

// ---- animation: bouncing pinball with trail, or starfield ----
#define TRAIL 12
#define TRAIL_EVERY 3
#define BALL_R 9
float ballX, ballY, ballVX, ballVY;
int16_t trailX[TRAIL], trailY[TRAIL];
int trailN = 0;
uint16_t ballHue = 0;

#define STARS 70
struct Star { float x, y, z; int16_t px, py; };
Star stars[STARS];

uint16_t hueColour(uint16_t h) {  // h 0..359 -> saturated RGB565
  uint8_t r, g, b, x = (uint8_t)((h % 60) * 255 / 60);
  switch ((h / 60) % 6) {
    case 0: r = 255; g = x; b = 0; break;
    case 1: r = 255 - x; g = 255; b = 0; break;
    case 2: r = 0; g = 255; b = x; break;
    case 3: r = 0; g = 255 - x; b = 255; break;
    case 4: r = x; g = 0; b = 255; break;
    default: r = 255; g = 0; b = 255 - x; break;
  }
  return tft.color565(r, g, b);
}

uint16_t dimColour(uint16_t c, int num, int den) {
  uint8_t r = ((c >> 11) & 0x1F) * num / den, g = ((c >> 5) & 0x3F) * num / den, b = (c & 0x1F) * num / den;
  return (r << 11) | (g << 5) | b;
}

void resetStar(Star &s, bool anyDepth) {
  s.x = random(-1000, 1000) / 1000.0f;
  s.y = random(-1000, 1000) / 1000.0f;
  s.z = anyDepth ? random(100, 1000) / 1000.0f : 1.0f;
  s.px = -1;
}

void drawAnim(const IdleScreen &s) {
  int W = tft.width(), H = tft.height();
  if (s.style == 1) {
    for (int i = 0; i < STARS; i++) resetStar(stars[i], true);
  } else {
    ballX = random(BALL_R + 2, W - BALL_R - 2);
    ballY = random(BALL_R + 2, H - BALL_R - 2);
    float a = random(20, 70) * 0.01745f + (random(4) * 1.5708f);
    ballVX = 3.2f * cosf(a);
    ballVY = 3.2f * sinf(a);
    trailN = 0;
  }
  if (s.text.length()) {  // optional small caption, dim, bottom
    tft.setFreeFont(&FreeSans9pt7b);
    tft.setTextColor(COL_DIM);
    tft.setTextDatum(BC_DATUM);
    tft.drawString(s.text, W / 2 + irt.ox, H - 4);
    tft.setTextDatum(TL_DATUM);
  }
}

void tickAnim(const IdleScreen &s) {
  int W = tft.width(), H = tft.height();
  if (s.style == 1) {
    int cx = W / 2, cy = H / 2;
    for (int i = 0; i < STARS; i++) {
      Star &p = stars[i];
      if (p.px >= 0) tft.drawRect(p.px, p.py, 2, 2, COL_BG);
      p.z -= 0.012f;
      if (p.z <= 0.05f) { resetStar(p, false); continue; }
      int sx = cx + (int)(p.x / p.z * cx), sy = cy + (int)(p.y / p.z * cy);
      if (sx < 0 || sx >= W - 1 || sy < 0 || sy >= H - 1) { resetStar(p, false); continue; }
      uint8_t v = (uint8_t)constrain((int)((1.0f - p.z) * 255), 40, 255);
      tft.fillRect(sx, sy, 2, 2, tft.color565(v, v, v));
      p.px = sx; p.py = sy;
    }
    return;
  }
  // pinball
  ballX += ballVX; ballY += ballVY;
  if (ballX < BALL_R) { ballX = BALL_R; ballVX = fabsf(ballVX); }
  if (ballX > W - 1 - BALL_R) { ballX = W - 1 - BALL_R; ballVX = -fabsf(ballVX); }
  if (ballY < BALL_R) { ballY = BALL_R; ballVY = fabsf(ballVY); }
  if (ballY > H - 1 - BALL_R - 20) {  // keep the bottom caption row clear
    ballY = H - 1 - BALL_R - 20; ballVY = -fabsf(ballVY);
    ballVX += random(-30, 31) / 100.0f;  // small nudge so the path keeps changing
    float sp = sqrtf(ballVX * ballVX + ballVY * ballVY);
    ballVX *= 3.2f / sp; ballVY *= 3.2f / sp;
  }
  // Trail: a new trail point every TRAIL_EVERY frames (longer comet tail); between samples only
  // the head moves. Erase what moves, then redraw the trail oldest -> newest.
  static uint8_t frame = 0;
  bool push = (++frame % TRAIL_EVERY) == 0 || trailN == 0;
  if (trailN) tft.fillCircle(trailX[0], trailY[0], BALL_R, COL_BG);  // old head
  if (push) {
    if (trailN == TRAIL) tft.fillCircle(trailX[TRAIL - 1], trailY[TRAIL - 1], BALL_R, COL_BG);
    for (int i = min(trailN, TRAIL - 1); i > 0; i--) { trailX[i] = trailX[i - 1]; trailY[i] = trailY[i - 1]; }
    if (trailN < TRAIL) trailN++;
    ballHue = (ballHue + 3) % 360;
  }
  trailX[0] = (int16_t)ballX; trailY[0] = (int16_t)ballY;
  uint16_t tc = hueColour(ballHue);
  for (int i = trailN - 1; i >= 1; i--) {
    int r = 2 + (BALL_R - 3) * (TRAIL - i) / TRAIL;
    if (push) tft.fillCircle(trailX[i], trailY[i], r + 1, COL_BG);  // it just shrank
    tft.fillCircle(trailX[i], trailY[i], r, dimColour(tc, TRAIL - i, TRAIL + 2));
  }
  // steel ball with highlight
  tft.fillCircle(trailX[0], trailY[0], BALL_R, 0xC618);
  tft.drawCircle(trailX[0], trailY[0], BALL_R, 0x7BEF);
  tft.fillCircle(trailX[0] - 3, trailY[0] - 3, 3, TFT_WHITE);
}

// ---- last played / up next ----
String whenText(uint32_t t) {
  if (!t) return "";
  CivilTime c = civil(t);
  char buf[40];
  if (clockValid) {
    uint32_t now = clockNow();
    int32_t dayNow = now / 86400UL, dayThen = t / 86400UL;
    if (dayNow == dayThen) return "Today at " + fmtTime(c);
    if (dayNow - dayThen == 1) return "Yesterday at " + fmtTime(c);
    if (dayNow - dayThen > 1 && dayNow - dayThen < 7) return String(WDAYS[c.wd]) + " at " + fmtTime(c);
  }
  snprintf(buf, sizeof buf, "%.3s %d, %d", MONTHS[c.mo - 1], c.d, c.y);
  return String(buf);
}

String agoText(uint32_t t) {
  if (!t || !clockValid) return "";
  uint32_t now = clockNow();
  if (now < t) return "";
  uint32_t d = now - t;
  if (d < 120) return "just now";
  if (d < 3600) return String(d / 60) + " minutes ago";
  if (d < 7200) return "1 hour ago";
  if (d < 86400) return String(d / 3600) + " hours ago";
  if (d < 172800) return "1 day ago";
  return String(d / 86400) + " days ago";
}

void drawTitledBig(const String &label, uint16_t labelCol, const String &big, const String &l1, const String &l2) {
  int W = tft.width(), H = tft.height();
  int top = drawIdleTitle(label, labelCol);
  int bottomH = (l1.length() ? 28 : 0) + (l2.length() ? 24 : 0);
  int boxH = H - top - bottomH - 10;
  drawCenteredFit(big, W / 2 + irt.ox, top + boxH / 2 + 2, W - 28, boxH, COL_ACCENT, 1);
  int y = H - bottomH - 6 + irt.oy / 2;
  tft.setTextDatum(TC_DATUM);
  if (l1.length()) {
    tft.setFreeFont(&FreeSansBold12pt7b);
    tft.setTextColor(COL_TEXT);
    tft.drawString(l1, W / 2 + irt.ox, y);
    y += 28;
  }
  if (l2.length()) {
    tft.setFreeFont(&FreeSans9pt7b);
    tft.setTextColor(COL_DIM);
    tft.drawString(l2, W / 2 + irt.ox, y);
  }
  tft.setTextDatum(TL_DATUM);
}

void drawLast(const IdleScreen &s) {
  drawTitledBig(s.title.length() ? s.title : "LAST PLAYED", COL_MAGENTA, lastTitle, whenText(lastTs), agoText(lastTs));
}

void drawUpNext(const IdleScreen &s) {
  drawTitledBig(s.title.length() ? s.title : "UP NEXT", COL_GREEN, selectedTable,
                s.text.length() ? s.text : "Press START to play", "");
}

// ---- playlist ----
const IdleScreen *curScreen() {
  static IdleScreen fallback;  // marquee
  if (icfg.count == 0 || irt.idx < 0 || irt.idx >= icfg.count) return &fallback;
  return &icfg.screens[irt.idx];
}

void drawIdleScreen() {
  const IdleScreen &s = *curScreen();
  irt.ox = SHIFTS[irt.cycle % (sizeof SHIFTS / sizeof SHIFTS[0])][0];
  irt.oy = SHIFTS[irt.cycle % (sizeof SHIFTS / sizeof SHIFTS[0])][1];
  irt.phase = 0;
  tft.fillScreen(COL_BG);
  switch (s.type) {
    case IS_MARQUEE: drawMarquee(s); break;
    case IS_CHOOSE: drawChoose(s); break;
    case IS_CLOCK: drawClock(s); break;
    case IS_RULES: drawRules(s, "HOUSE RULES", COL_CYAN); break;
    case IS_PRICING: drawPricing(s); break;
    case IS_ANIM: drawAnim(s); break;
    case IS_LAST: drawLast(s); break;
    case IS_UPNEXT: drawUpNext(s); break;
    default: drawRules(s, "", COL_CYAN); break;
  }
  irt.start = irt.tickT = millis();
}

// Move to the next usable screen (from index `from`, inclusive) and draw it.
void idleGoto(int from) {
  int n = icfg.count;
  irt.idx = -1;
  for (int k = 0; k < n; k++) {
    int i = ((from + k) % n + n) % n;
    if (screenUsable(icfg.screens[i])) { irt.idx = i; break; }
  }
  irt.cycle++;
  drawIdleScreen();
}

void idleStart() {
  int startIdx = 0;
  if (selectedTable.length()) {
    for (int i = 0; i < icfg.count; i++)
      if (icfg.screens[i].type == IS_UPNEXT) { startIdx = i; break; }
  }
  idleGoto(startIdx);
}

void idleTick(unsigned long now) {
  const IdleScreen &s = *curScreen();
  if (now - irt.start >= screenDurMs(s)) {
    idleGoto(irt.idx + 1);
    return;
  }
  uint16_t every;
  switch (s.type) {
    case IS_MARQUEE: every = 220; break;
    case IS_CHOOSE: every = 140; break;
    case IS_CLOCK: every = 500; break;
    case IS_ANIM: every = 28; break;
    default: return;
  }
  if (now - irt.tickT < every) return;
  irt.tickT = now;
  switch (s.type) {
    case IS_MARQUEE: tickMarquee(); break;
    case IS_CHOOSE: tickChoose(); break;
    case IS_CLOCK: tickClock(); break;
    case IS_ANIM: tickAnim(s); break;
    default: break;
  }
}

// ---------- Cards ----------
void drawCard() {
  if (st.idle || st.cardCount == 0) {
    idleStart();
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
  if (st.idle || st.cardCount == 0) {
    idleGoto(irt.idx + 1);  // tap during idle = next attract screen
    return;
  }
  st.current = (st.current + 1) % st.cardCount;
  drawCard();
}

// ---------- Persistence ----------
void saveTable(const String &json) {
  prefs.begin("cards", false);
  // stored as a blob: NVS strings are limited to ~4000 bytes, blobs are not
  prefs.putBytes("last", json.c_str(), json.length());
  prefs.putBool("idle", false);
  prefs.putString("lastT", lastTitle);
  prefs.putULong("lastTs", lastTs);
  prefs.end();
}

void saveIdle(bool idle) {
  prefs.begin("cards", false);
  if (prefs.getBool("idle", !idle) != idle) prefs.putBool("idle", idle);
  prefs.end();
}

// Persist the idle config (without clock/selection), only when it changed (limits flash wear).
void saveIdleConfig(const String &cfg) {
  prefs.begin("cards", false);
  size_t len = prefs.getBytesLength("idlecfg");
  bool same = false;
  if (len == cfg.length() && len > 0) {
    char *buf = (char *)malloc(len);
    if (buf) {
      prefs.getBytes("idlecfg", buf, len);
      same = memcmp(buf, cfg.c_str(), len) == 0;
      free(buf);
    }
  }
  if (!same) prefs.putBytes("idlecfg", cfg.c_str(), cfg.length());
  prefs.end();
}

String loadBlob(const char *key) {
  String out;
  size_t len = prefs.getBytesLength(key);
  if (len > 0 && len <= MAX_LINE) {
    char *buf = (char *)malloc(len + 1);
    if (buf) {
      prefs.getBytes(key, buf, len);
      buf[len] = 0;
      out = buf;
      free(buf);
    }
  }
  return out;
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
    if (doc["ts"].is<uint32_t>()) setClock(doc["ts"].as<uint32_t>());
    loadTableFromDoc(doc);
    lastTitle = st.tableTitle;
    lastTs = clockValid ? clockNow() : 0;
    selectedTable = "";
    tableStartMs = millis();
    saveTable(line);
    drawCard();
    lastRotate = millis();
    reply("table", true, nullptr, "cards", st.cardCount);
  } else if (!strcmp(cmd, "idle")) {
    if (doc["ts"].is<uint32_t>()) setClock(doc["ts"].as<uint32_t>());
    selectedTable = (const char *)(doc["selected"] | "");
    if (doc["screens"].is<JsonArray>() || doc["cabinet"].is<const char *>()) {
      // full idle config: apply, then persist it without the volatile keys
      applyIdleConfig(doc);
      doc.remove("cmd");
      doc.remove("ts");
      doc.remove("selected");
      String cfg;
      serializeJson(doc, cfg);
      saveIdleConfig(cfg);
      prefs.begin("cards", false);
      prefs.putUShort("autoidle", icfg.autoIdleMin);
      prefs.end();
    }
    st.idle = true;
    saveIdle(true);
    drawCard();
    JsonDocument r;
    r["ack"] = "idle";
    r["ok"] = true;
    r["screens"] = icfg.count;
    r["clock"] = clockValid;
    serializeJson(r, Serial);
    Serial.println();
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
    reply("next", true, nullptr, "card", st.idle ? irt.idx : st.current);
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
  // Big RX ring buffer (must be set before begin): a full 6 KB line can arrive while a screen
  // is being drawn without overflowing the default 256-byte UART buffer.
  Serial.setRxBufferSize(RX_BUFFER);
  Serial.begin(115200);
  rxLine.reserve(1024);
  randomSeed(esp_random());

  prefs.begin("cards", true);
  st.brightness = prefs.getUChar("bright", DEFAULT_BRIGHTNESS);
  st.rotation = prefs.getUChar("rot", CYD_ROTATION);
  bool wasIdle = prefs.getBool("idle", true);
  String last = loadBlob("last");
  String idleCfg = loadBlob("idlecfg");
  lastTitle = prefs.getString("lastT", "");
  lastTs = prefs.getULong("lastTs", 0);
  prefs.end();

  loadDefaultIdleConfig();
  if (idleCfg.length()) {
    JsonDocument doc;
    if (!deserializeJson(doc, idleCfg)) applyIdleConfig(doc);
  }

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
    if (!deserializeJson(doc, last)) {
      loadTableFromDoc(doc);
      if (!lastTitle.length()) lastTitle = st.tableTitle;
    }
  }
  tableStartMs = millis();
  drawCard();
  lastRotate = millis();
  Serial.println("{\"ready\":true,\"device\":\"cyd-pinball-cards\",\"fw\":\"" FW_VERSION "\"}");
}

void loop() {
  pollSerial();
  pollTouch();
  unsigned long now = millis();
  if (!st.idle) {
    if (st.cardCount > 1 && CARD_ROTATE_MS > 0 && now - lastRotate >= CARD_ROTATE_MS) {
      lastRotate = now;
      nextCard();
    }
    // optional safety net: fall back to the attract playlist N minutes after the last table push
    if (icfg.autoIdleMin && now - tableStartMs >= (unsigned long)icfg.autoIdleMin * 60000UL) {
      st.idle = true;
      saveIdle(true);
      drawCard();
    }
  } else {
    idleTick(now);
  }
  delay(st.idle ? 2 : 5);
}
