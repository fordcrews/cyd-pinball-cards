// CYD Pinball Cards - companion card display for a virtual pinball cabinet
// Boards: ESP32-2432S028R (Cheap Yellow Display, env cyd) and Waveshare ESP32-S3-Touch-LCD-7
// (7" 800x480, env waveshare_s3_lcd7). Board-specific code (display driver, touch, backlight,
// serial ports) is in board.h / board_cyd.cpp / board_ws_s3_lcd7.cpp; this file is shared.
//
// Serial protocol (115200 baud, newline-delimited JSON, one object per line):
//   {"cmd":"table","title":"Medieval Madness","ts":<local epoch s, optional>,"cards":[{"type":"instructions","title":"Rules","text":"..."}, ...]}
//   {"cmd":"idle"}        bare idle: attract playlist using the saved (or built-in default) idle config
//   {"cmd":"idle","ts":<local epoch s>,"cabinet":"Crews Pinball","screens":[...],"selected":"Table"}
//                         full idle: config comes from cards/_idle.json via cyd_push.py --idle (see README)
//   {"cmd":"brightness","value":0-255}
//   {"cmd":"rotation","value":0-3}
//   {"cmd":"next"}        next card / next idle screen / next keypad page
//   {"cmd":"ping"}  |  {"cmd":"hello"}   reply includes fw, mode, board ("cyd" | "ws-s3-7") and the identity (id, name, role)
//   {"cmd":"identify"[,"secs":5]}   big on-screen label (role / name / id) for a few seconds
//   {"cmd":"config"[,"name":"Right palm"][,"role":"right"][,"rotation":0-3][,"keypad":true|false]}
//   {"cmd":"set_id"[,"id":"cyd-right"|"reset":true][,"name":..][,"role":..]}   same as config, plus the id
//                         (identity is saved in NVS; the default id comes from the ESP32 MAC: cyd-a1b2c3)
//   {"cmd":"keypad"[,"layout":{"pages":[...]}][,"page":N]}   touch mini-keyboard (see README)
//   {"cmd":"keypad","exit":true}   leave the keypad and restore the previous screen
//   {"cmd":"cal"}  |  {"cmd":"cal","x_min":200,"x_max":3700,"y_min":240,"y_max":3800}  |
//   {"cmd":"cal","reset":true}  |  {"cmd":"cal","debug":true|false}      touch calibration values
//   {"cmd":"calibrate"}   on-device 4-point touch calibration (tap the crosshairs)
//                         (capacitive GT911 board: cal/calibrate are no-ops that report "touch":"capacitive")
// Events (device -> host, unsolicited):
//   {"evt":"key","key":"alt+f4"[,"mods":["ctrl","shift","alt","win"]]}   keypad button released
//   {"evt":"keypad","state":"on"|"off","source":"touch"}   keypad opened (long-press) / EXIT tapped
//   {"evt":"cal","ok":true,"x_min":..,...}   calibration finished (or "ok":false,"err":...)
//   {"evt":"touch","raw_x":..,"raw_y":..,"x":..,"y":..}   only while cal debug is on
// Replies: {"ack":"<cmd>","ok":true[, ...]} or {"ack":"<cmd>","ok":false,"err":"..."}
// Boot line: {"ready":true,"device":"cyd-pinball-cards","fw":"1.4.0","board":"cyd","id":"cyd-a1b2c3","name":"","role":""}
//
// "ts" is the host's LOCAL wall-clock time as seconds since 1970-01-01 00:00 (i.e. local time
// encoded as if it were UTC). The ESP32 has no RTC, so the clock only runs after a host sent it,
// and it is advanced locally with millis().

#include <Arduino.h>
#include "board.h"
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

// Touch calibration defaults: raw XPT2046 readings (native / rotation-1 orientation) at the
// left/right and top/bottom edges of the 320x240 landscape screen. Override in build_flags or at
// runtime with {"cmd":"cal",...} / {"cmd":"calibrate"} (saved to flash).
#ifndef TOUCH_X_MIN
#define TOUCH_X_MIN 200
#endif
#ifndef TOUCH_X_MAX
#define TOUCH_X_MAX 3700
#endif
#ifndef TOUCH_Y_MIN
#define TOUCH_Y_MIN 240
#endif
#ifndef TOUCH_Y_MAX
#define TOUCH_Y_MAX 3800
#endif
// Hold a finger on the idle/card screen this long to open the keypad (0 = disabled)
#ifndef LONGPRESS_KEYPAD_MS
#define LONGPRESS_KEYPAD_MS 2000
#endif
// How long {"cmd":"identify"} shows the board label when no "secs" is given
#ifndef IDENTIFY_MS
#define IDENTIFY_MS 5000
#endif

#define FW_VERSION "1.4.0"
#define MAX_CARDS 8
#define MAX_IDLE_SCREENS 12
#define MAX_LINE 6144         // longest accepted JSON line (bytes)
#define RX_BUFFER (MAX_LINE + 512)  // UART RX ring buffer: a whole line fits even while drawing

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

// UI mode on top of the idle/table state: the keypad and calibration screens temporarily take over
enum UiMode : uint8_t { UI_NORMAL, UI_KEYPAD, UI_CAL, UI_IDENT };
UiMode ui = UI_NORMAL;

// ---------- Board identity (multi-display) ----------
// Saved in NVS ("cards" namespace): bid (custom id, empty = MAC-derived), bname, brole, kpen.
struct Identity {
  String macId;       // cyd-a1b2c3 from the last 3 bytes of the factory MAC
  String customId;    // set with {"cmd":"set_id","id":...}
  String name;        // free text, e.g. "Right palm"
  String role;        // right | left | top | bottom | center | any free text (lower case)
  bool keypad = true; // false: long-press does not open the keypad on this board
} ident;

String boardId() { return ident.customId.length() ? ident.customId : ident.macId; }


// ---------- Fonts ----------
// Logical font sizes; the CYD uses them as-is, the 800x480 board uses ~2x larger faces
// (18/24 pt FreeFonts natively, the big bold titles as 2x-scaled 18/24 pt).
enum UiFont : uint8_t { F_S9, F_S12, F_SB9, F_SB12, F_SB18, F_SB24, F_SMALL, F_CLOCK7, F_CLOCK8 };

void useFont(UiFont f) {
#if UI_SCALE == 1
  tft.setTextSize(1);
  switch (f) {
    case F_S9: tft.setFreeFont(&FreeSans9pt7b); break;
    case F_S12: tft.setFreeFont(&FreeSans12pt7b); break;
    case F_SB9: tft.setFreeFont(&FreeSansBold9pt7b); break;
    case F_SB12: tft.setFreeFont(&FreeSansBold12pt7b); break;
    case F_SB18: tft.setFreeFont(&FreeSansBold18pt7b); break;
    case F_SB24: tft.setFreeFont(&FreeSansBold24pt7b); break;
    case F_SMALL: tft.setTextFont(2); break;
    case F_CLOCK7: tft.setTextFont(7); break;
    default: tft.setTextFont(8); break;
  }
#else
  uint8_t size = 1;
  switch (f) {
    case F_S9: tft.setFont(&FreeSans18pt7b); break;
    case F_S12: tft.setFont(&FreeSans24pt7b); break;
    case F_SB9: tft.setFont(&FreeSansBold18pt7b); break;
    case F_SB12: tft.setFont(&FreeSansBold24pt7b); break;
    case F_SB18: tft.setFont(&FreeSansBold18pt7b); size = 2; break;
    case F_SB24: tft.setFont(&FreeSansBold24pt7b); size = 2; break;
    case F_SMALL: tft.setTextFont(4); break;
    case F_CLOCK7: tft.setTextFont(7); size = 2; break;
    default: tft.setTextFont(8); size = 2; break;
  }
  tft.setTextSize(size);
#endif
}

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
  boardSetBacklight(v);
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
  UiFont fonts[] = {F_SB18, F_SB12, F_S12, F_S9};
  const int lineHs[] = {S(34), S(26), S(26), S(20)};
  tft.setTextColor(colour, COL_BG);
  tft.setTextDatum(TL_DATUM);
  for (int f = 0; f < 4; f++) {
    useFont(fonts[f]);
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
  UiFont fonts[] = {F_SB24, F_SB18, F_SB12, F_S9};
  const int lineHs[] = {S(46), S(36), S(28), S(20)};
  String lines[6];
  int f = maxFont, n = 0;
  for (; f < 4; f++) {
    useFont(fonts[f]);
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
  useFont(fonts[f]);
  tft.setTextDatum(MC_DATUM);
  int y = cy - (n * lineHs[f]) / 2 + lineHs[f] / 2;
  for (int i = 0; i < n; i++) {
    if (shadow) {
      tft.setTextColor(shadow);
      tft.drawString(lines[i], cx + S(2), y + S(2));
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
  tft.fillRect(0, 0, W, S(36), colour);
  tft.setTextColor(TFT_WHITE, colour);
  useFont(F_SB12);
  tft.setTextDatum(ML_DATUM);
  String s = label;
  while (s.length() > 1 && tft.textWidth(s) > W - S(60)) s.remove(s.length() - 1);
  tft.drawString(s, S(8), S(18));
  // page indicator
  if (!st.idle && st.cardCount > 1) {
    useFont(F_SMALL);
    tft.setTextDatum(MR_DATUM);
    tft.drawString(String(st.current + 1) + "/" + String(st.cardCount), W - S(8), S(18));
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
  if (!label.length()) return S(10) + irt.oy;
  useFont(F_SB18);
  String s = label;
  if (tft.textWidth(s) > W - S(24)) useFont(F_SB12);
  while (s.length() > 1 && tft.textWidth(s) > W - S(24)) s.remove(s.length() - 1);
  tft.setTextColor(colour);
  tft.setTextDatum(TC_DATUM);
  int y = S(10) + irt.oy;
  tft.drawString(s, W / 2 + irt.ox, y);
  int tw = tft.textWidth(s);
  tft.fillRect(W / 2 + irt.ox - tw / 2, y + S(36), tw, S(3), colour);
  tft.setTextDatum(TL_DATUM);
  return y + S(46);
}

// ---- marquee: cabinet name with chasing bulbs ----
int bulbCount() {
  int W = tft.width(), H = tft.height();
  return 2 * ((W - S(12)) / S(20)) + 2 * ((H - S(12)) / S(20));
}

void bulbPos(int i, int &x, int &y) {
  int W = tft.width(), H = tft.height();
  int nx = (W - S(12)) / S(20), ny = (H - S(12)) / S(20);
  int stepX = (W - S(12)) / nx, stepY = (H - S(12)) / ny;
  const int e0 = S(6), e1 = S(7) - (UI_SCALE - 1);  // bulb centre distance from the edges
  if (i < nx) { x = e0 + i * stepX; y = e0; return; }
  i -= nx;
  if (i < ny) { x = W - e1; y = e0 + i * stepY; return; }
  i -= ny;
  if (i < nx) { x = W - e1 - i * stepX; y = H - e1; return; }
  i -= nx;
  x = e0; y = H - e1 - i * stepY;
}

void drawBulbs() {
  int n = bulbCount();
  for (int i = 0; i < n; i++) {
    int x, y;
    bulbPos(i, x, y);
    bool lit = ((i + irt.phase) % 3) == 0;
    tft.fillCircle(x, y, S(4), lit ? COL_GOLD : 0x4100);
  }
}

void drawMarquee(const IdleScreen &s) {
  int W = tft.width(), H = tft.height();
  drawBulbs();
  // inner frame
  for (int k = 0; k < UI_SCALE; k++) {
    tft.drawRoundRect(S(16) + k, S(16) + k, W - S(32) - 2 * k, H - S(32) - 2 * k, S(8), COL_RED);
    tft.drawRoundRect(S(18) + k, S(18) + k, W - S(36) - 2 * k, H - S(36) - 2 * k, S(7), 0x7800);
  }
  String name = s.title.length() ? s.title : icfg.cabinet;
  String sub = s.text.length() ? s.text : icfg.subtitle;
  int cx = W / 2 + irt.ox, cy = H / 2 - S(14) + irt.oy;
  drawCenteredFit(name, cx, cy, W - S(56), H - S(110), COL_GOLD, 0, COL_RED);
  if (sub.length()) {
    useFont(F_SB12);
    if (tft.textWidth(sub) > W - S(56)) useFont(F_S9);
    tft.setTextColor(COL_CYAN);
    tft.setTextDatum(MC_DATUM);
    tft.drawString(sub, cx, H - S(52) + irt.oy);
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
  const int n = 7, cw = S(26);
  int x0 = W / 2 - (n * cw) / 2 + irt.ox, y = H - S(44) + irt.oy;
  int lit = irt.phase % (n + 3);
  for (int i = 0; i < n; i++) {
    uint16_t c = (i == lit) ? COL_ACCENT : (i == lit - 1 ? 0x7A00 : COL_DARK);
    int x = x0 + i * cw;
    tft.fillTriangle(x, y - S(12), x + S(16), y, x, y + S(12), c);
    tft.fillTriangle(x, y - S(6), x + S(8), y, x, y + S(6), COL_BG);
  }
}

void drawChoose(const IdleScreen &s) {
  int W = tft.width(), H = tft.height();
  String title = s.title.length() ? s.title : "PICK A TABLE";
  String text = s.text.length() ? s.text : "Now choosing...";
  drawCenteredFit(title, W / 2 + irt.ox, S(70) + irt.oy, W - S(30), S(100), COL_ACCENT, 0);
  tft.setTextColor(COL_TEXT);
  drawCenteredFit(text, W / 2 + irt.ox, S(142) + irt.oy, W - S(30), S(50), COL_TEXT, 2);
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
  UiFont font = F_CLOCK8;
  useFont(font);
  int wDigits = tft.textWidth("00"), wColon = tft.textWidth(":");
  int ampmW = icfg.h24 ? 0 : S(44);
  if (2 * wDigits + wColon + ampmW > W - S(20)) {
    font = F_CLOCK7;
    useFont(font);
    wDigits = tft.textWidth("00");
    wColon = tft.textWidth(":");
  }
  int fh = tft.fontHeight();
  int total = 2 * wDigits + wColon + ampmW;
  int x0 = W / 2 - total / 2 + irt.ox;
  int y = S(30) + irt.oy;
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
      useFont(F_SB12);
      tft.setTextColor(COL_ACCENT, COL_BG);
      tft.setTextPadding(ampmW);
      tft.drawString(c.h < 12 ? "AM" : "PM", colonX + wColon + wDigits + S(6), y + fh - S(26));
      tft.setTextPadding(0);
    }
    if (full || c.mi != irt.lastMinute) {
      // date lines
      int H = tft.height();
      tft.fillRect(0, y + fh + S(6), W, H - (y + fh + S(6)), COL_BG);
      tft.setTextDatum(TC_DATUM);
      useFont(F_SB18);
      tft.setTextColor(COL_ACCENT);
      tft.drawString(WDAYS[c.wd], W / 2 + irt.ox, y + fh + S(14));
      char date[32];
      snprintf(date, sizeof date, "%s %d, %d", MONTHS[c.mo - 1], c.d, c.y);
      useFont(F_SB12);
      tft.setTextColor(COL_TEXT);
      tft.drawString(date, W / 2 + irt.ox, y + fh + S(56));
      useFont(F_S9);
      tft.setTextColor(COL_DIM);
      tft.drawString(icfg.cabinet, W / 2 + irt.ox, y + fh + S(90));
      tft.setTextDatum(TL_DATUM);
    }
    irt.lastMinute = c.mi;
  }
  useFont(font);
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
  const int pad = S(14);
  tft.setTextColor(COL_TEXT, COL_BG);
  tft.setTextDatum(TL_DATUM);
  int x = pad + irt.ox, w = W - 2 * pad, maxY = H - S(8);
  useFont(F_SB12);
  int need = drawWrapped(s.text, x, top + S(4), w, maxY, S(28), true);
  if (need > maxY) {
    useFont(F_S12);
    need = drawWrapped(s.text, x, top + S(4), w, maxY, S(25), true);
    if (need > maxY) {
      useFont(F_S9);
      drawWrapped(s.text, x, top + S(2), w, maxY, S(20));
      return;
    }
    drawWrapped(s.text, x, top + S(4), w, maxY, S(25));
    return;
  }
  drawWrapped(s.text, x, top + S(4), w, maxY, S(28));
}

void drawPricing(const IdleScreen &s) {
  int W = tft.width(), H = tft.height();
  int top = drawIdleTitle(s.title.length() ? s.title : "PRICING", COL_GREEN);
  String text = s.text.length() ? s.text : "FREE PLAY";
  int lines = 1;
  for (char ch : text) if (ch == '\n') lines++;
  UiFont big = lines <= 3 ? F_SB24 : F_SB12;
  int lh = lines <= 3 ? S(50) : S(30);
  if (lines == 3) { big = F_SB18; lh = S(42); }
  int y = top + (H - top - lines * lh) / 2 + lh / 2;
  tft.setTextColor(COL_YELLOW);
  tft.setTextDatum(MC_DATUM);
  int s0 = 0;
  for (int i = 0; i < lines; i++) {
    int nl = text.indexOf('\n', s0);
    String line = text.substring(s0, nl < 0 ? text.length() : nl);
    useFont(big);
    if (tft.textWidth(line) > W - S(24)) useFont(F_SB18);
    if (tft.textWidth(line) > W - S(24)) useFont(F_SB12);
    if (tft.textWidth(line) > W - S(24)) useFont(F_S9);
    tft.drawString(line, W / 2 + irt.ox, y);
    y += lh;
    s0 = nl + 1;
  }
  tft.setTextDatum(TL_DATUM);
}

// ---- animation: bouncing pinball with trail, or starfield ----
#define TRAIL 12
#define TRAIL_EVERY 3
#define BALL_R S(9)
#define BALL_SPEED (3.2f * UI_SCALE)
float ballX, ballY, ballVX, ballVY;
int16_t trailX[TRAIL], trailY[TRAIL];
int trailN = 0;
uint16_t ballHue = 0;

#define STARS (70 * UI_SCALE)
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
    ballVX = BALL_SPEED * cosf(a);
    ballVY = BALL_SPEED * sinf(a);
    trailN = 0;
  }
  if (s.text.length()) {  // optional small caption, dim, bottom
    useFont(F_S9);
    tft.setTextColor(COL_DIM);
    tft.setTextDatum(BC_DATUM);
    tft.drawString(s.text, W / 2 + irt.ox, H - S(4));
    tft.setTextDatum(TL_DATUM);
  }
}

void tickAnim(const IdleScreen &s) {
  int W = tft.width(), H = tft.height();
  if (s.style == 1) {
    int cx = W / 2, cy = H / 2;
    for (int i = 0; i < STARS; i++) {
      Star &p = stars[i];
      if (p.px >= 0) tft.fillRect(p.px, p.py, S(2), S(2), COL_BG);
      p.z -= 0.012f;
      if (p.z <= 0.05f) { resetStar(p, false); continue; }
      int sx = cx + (int)(p.x / p.z * cx), sy = cy + (int)(p.y / p.z * cy);
      if (sx < 0 || sx >= W - S(1) || sy < 0 || sy >= H - S(1)) { resetStar(p, false); continue; }
      uint8_t v = (uint8_t)constrain((int)((1.0f - p.z) * 255), 40, 255);
      tft.fillRect(sx, sy, S(2), S(2), tft.color565(v, v, v));
      p.px = sx; p.py = sy;
    }
    return;
  }
  // pinball
  ballX += ballVX; ballY += ballVY;
  if (ballX < BALL_R) { ballX = BALL_R; ballVX = fabsf(ballVX); }
  if (ballX > W - 1 - BALL_R) { ballX = W - 1 - BALL_R; ballVX = -fabsf(ballVX); }
  if (ballY < BALL_R) { ballY = BALL_R; ballVY = fabsf(ballVY); }
  if (ballY > H - 1 - BALL_R - S(20)) {  // keep the bottom caption row clear
    ballY = H - 1 - BALL_R - S(20); ballVY = -fabsf(ballVY);
    ballVX += random(-30, 31) / 100.0f;  // small nudge so the path keeps changing
    float sp = sqrtf(ballVX * ballVX + ballVY * ballVY);
    ballVX *= BALL_SPEED / sp; ballVY *= BALL_SPEED / sp;
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
    int r = S(2) + (BALL_R - S(3)) * (TRAIL - i) / TRAIL;
    if (push) tft.fillCircle(trailX[i], trailY[i], r + S(1), COL_BG);  // it just shrank
    tft.fillCircle(trailX[i], trailY[i], r, dimColour(tc, TRAIL - i, TRAIL + 2));
  }
  // steel ball with highlight
  tft.fillCircle(trailX[0], trailY[0], BALL_R, 0xC618);
  tft.drawCircle(trailX[0], trailY[0], BALL_R, 0x7BEF);
  tft.fillCircle(trailX[0] - S(3), trailY[0] - S(3), S(3), TFT_WHITE);
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
  int bottomH = (l1.length() ? S(28) : 0) + (l2.length() ? S(24) : 0);
  int boxH = H - top - bottomH - S(10);
  drawCenteredFit(big, W / 2 + irt.ox, top + boxH / 2 + S(2), W - S(28), boxH, COL_ACCENT, 1);
  int y = H - bottomH - S(6) + irt.oy / 2;
  tft.setTextDatum(TC_DATUM);
  if (l1.length()) {
    useFont(F_SB12);
    tft.setTextColor(COL_TEXT);
    tft.drawString(l1, W / 2 + irt.ox, y);
    y += S(28);
  }
  if (l2.length()) {
    useFont(F_S9);
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
  irt.ox = S(SHIFTS[irt.cycle % (sizeof SHIFTS / sizeof SHIFTS[0])][0]);
  irt.oy = S(SHIFTS[irt.cycle % (sizeof SHIFTS / sizeof SHIFTS[0])][1]);
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

  const int pad = S(8), top = S(44);
  if (c.type == "title") {
    // Big title, wrapped with the largest font that fits
    String t = c.text.length() ? c.text : st.tableTitle;
    drawFittedText(t, pad, top + S(6), W - 2 * pad, H - top - S(12), COL_ACCENT);
  } else if (c.type == "cost") {
    // Cost card: each line of text as a big row, e.g. "1 CREDIT = 25c\n3 BALLS"
    tft.setTextColor(TFT_YELLOW, COL_BG);
    tft.setTextDatum(MC_DATUM);
    int lines = 1;
    for (char ch : c.text) if (ch == '\n') lines++;
    useFont(lines <= 3 ? F_SB18 : F_SB12);
    int lh = lines <= 3 ? S(44) : S(30);
    int y = top + (H - top - lines * lh) / 2 + lh / 2;
    int s = 0;
    for (int i = 0; i < lines; i++) {
      int nl = c.text.indexOf('\n', s);
      String line = c.text.substring(s, nl < 0 ? c.text.length() : nl);
      // shrink font for this line if it is too wide
      if (tft.textWidth(line) > W - 2 * pad) useFont(F_SB12);
      if (tft.textWidth(line) > W - 2 * pad) useFont(F_S9);
      tft.drawString(line, W / 2, y);
      useFont(lines <= 3 ? F_SB18 : F_SB12);
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
      useFont(F_SB12);
      drawWrapped(c.text, pad, top + S(4), W - 2 * pad, H - S(4), S(27));
    } else {
      useFont(F_S12);
      int need = drawWrapped(c.text, pad, top + S(4), W - 2 * pad, H - S(4), S(25), true);
      if (need > H - S(4)) useFont(F_S9);  // long rules: smaller font
      drawWrapped(c.text, pad, top + S(4), W - 2 * pad, H - S(4), need > H - S(4) ? S(20) : S(25));
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

// Persist a blob only when it changed (limits flash wear).
void saveBlobIfChanged(const char *key, const String &val) {
  prefs.begin("cards", false);
  size_t len = prefs.getBytesLength(key);
  bool same = false;
  if (len == val.length() && len > 0) {
    char *buf = (char *)malloc(len);
    if (buf) {
      prefs.getBytes(key, buf, len);
      same = memcmp(buf, val.c_str(), len) == 0;
      free(buf);
    }
  }
  if (!same) prefs.putBytes(key, val.c_str(), val.length());
  prefs.end();
}

// Persist the idle config (without clock/selection).
void saveIdleConfig(const String &cfg) { saveBlobIfChanged("idlecfg", cfg); }

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

// ---------- Touch: calibration + coordinate mapping ----------
struct TouchCal {
  int16_t xMin = TOUCH_X_MIN, xMax = TOUCH_X_MAX, yMin = TOUCH_Y_MIN, yMax = TOUCH_Y_MAX;
} tcal;
bool touchDebug = false;

bool calValid(const TouchCal &c) {
  int dx = abs(c.xMax - c.xMin), dy = abs(c.yMax - c.yMin);
  return dx >= 1000 && dx <= 8000 && dy >= 1000 && dy <= 8000;
}

void saveCal() {
  prefs.begin("cards", false);
  prefs.putBytes("tcal", &tcal, sizeof tcal);
  prefs.end();
}

// Touch is always read in the controller's native orientation (XPT2046 lib rotation 1 = landscape,
// which matches the CYD panel). Raw -> 320x240 landscape via the calibration, then rotated to the
// current display rotation using the same transforms the XPT2046 library applies.
void touchToScreen(int rx, int ry, int &sx, int &sy) {
  int lx = (int32_t)(rx - tcal.xMin) * 320 / (tcal.xMax - tcal.xMin);
  int ly = (int32_t)(ry - tcal.yMin) * 240 / (tcal.yMax - tcal.yMin);
  lx = constrain(lx, 0, 319);
  ly = constrain(ly, 0, 239);
  switch (st.rotation & 3) {
    case 1: sx = lx; sy = ly; break;
    case 3: sx = 319 - lx; sy = 239 - ly; break;
    case 0: sx = 239 - ly; sy = lx; break;
    default: sx = ly; sy = 319 - lx; break;
  }
}

void emitLine(JsonDocument &d) {
  serializeJson(d, HOST);
  HOST.println();
}

// ---------- Keypad (touch mini-keyboard) ----------
#define MAX_KP_PAGES 6
#define MAX_KP_KEYS 24
#define KP_HDR S(22)        // header bar height
#define KP_SWIPE_PX S(70)   // horizontal travel that counts as a page swipe

enum KpKind : uint8_t { KK_KEY, KK_ACTION, KK_MOD };
enum KpAction : uint8_t { KA_NEXT, KA_PREV, KA_EXIT, KA_PAGE };
enum : uint8_t { MOD_CTRL = 1, MOD_SHIFT = 2, MOD_ALT = 4, MOD_WIN = 8 };
static const char *MOD_NAMES[] = {"ctrl", "shift", "alt", "win"};

struct KpKey {
  String label;   // shown text; "@up" "@down" "@left" "@right" "@next" "@prev" draw arrows; "\n" = 2 lines
  String key;     // host key name / combo, e.g. "esc", "f5", "alt+f4"
  KpKind kind = KK_KEY;
  uint8_t action = 0, arg = 0, modBit = 0;
  uint8_t col = 0, row = 0, w = 1, h = 1;
  int32_t colour = -1;  // RGB565, -1 = automatic
};

struct KpPage {
  String title;
  uint8_t cols = 4, rows = 4, count = 0;
  KpKey keys[MAX_KP_KEYS];
};

KpPage kpPages[MAX_KP_PAGES];
int kpPageCount = 0;
int kpPage = 0;
uint8_t kpModOnce = 0, kpModLock = 0;  // one-shot / locked modifiers
int kpPressed = -1;                    // index of the highlighted key on the current page
String kpFlash;                        // "sent" feedback text in the header
unsigned long kpFlashMs = 0;
UiMode calPrevUi = UI_NORMAL;

// Built-in default; cards/_keypad.json (sent by the host) overrides it and is saved to flash.
#if UI_SCALE == 1
static const char KP_DEFAULT_LAYOUT[] PROGMEM = R"JSON({"pages":[
{"title":"BASIC","cols":4,"rows":4,"keys":[
 {"label":"ESC","key":"esc"},{"label":"TAB","key":"tab"},{"label":"@up","key":"up"},{"label":"BKSP","key":"backspace"},
 {"label":"ENTER","key":"enter"},{"label":"@left","key":"left"},{"label":"@down","key":"down"},{"label":"@right","key":"right"},
 {"label":"SPACE","key":"space","w":2},{"label":"F1","key":"f1"},{"label":"F2","key":"f2"},
 {"label":"ALT+F4","key":"alt+f4"},{"label":"ALT+TAB","key":"alt+tab"},{"label":"EXIT","action":"exit"},{"label":"@next","action":"next"}]},
{"title":"F-KEYS","cols":4,"rows":4,"keys":[
 "f1","f2","f3","f4","f5","f6","f7","f8","f9","f10","f11","f12",
 {"label":"@prev","action":"prev"},{"label":"ESC","key":"esc"},{"label":"ENTER","key":"enter"},{"label":"@next","action":"next"}]},
{"title":"NAV / MODS","cols":4,"rows":4,"keys":[
 {"label":"INS","key":"insert"},{"label":"HOME","key":"home"},{"label":"PGUP","key":"pageup"},{"label":"WIN","key":"win"},
 {"label":"DEL","key":"delete"},{"label":"END","key":"end"},{"label":"PGDN","key":"pagedown"},{"label":"ENTER","key":"enter"},
 {"label":"CTRL","mod":"ctrl"},{"label":"SHIFT","mod":"shift"},{"label":"ALT","mod":"alt"},{"label":"WIN+","mod":"win"},
 {"label":"@prev","action":"prev"},{"label":"ESC","key":"esc"},{"label":"EXIT","action":"exit"},{"label":"@next","action":"next"}]}
]})JSON";
#else
// 800x480: two 6x4 pages with bigger, more numerous buttons (~130x100 px each in landscape)
static const char KP_DEFAULT_LAYOUT[] PROGMEM = R"JSON({"pages":[
{"title":"MAIN","cols":6,"rows":4,"keys":[
 {"label":"ESC","key":"esc"},{"label":"TAB","key":"tab"},{"label":"F1","key":"f1"},{"label":"@up","key":"up"},{"label":"BKSP","key":"backspace"},{"label":"DEL","key":"delete"},
 {"label":"ENTER","key":"enter"},{"label":"ALT+TAB","key":"alt+tab"},{"label":"@left","key":"left"},{"label":"@down","key":"down"},{"label":"@right","key":"right"},{"label":"ALT+F4","key":"alt+f4"},
 {"label":"SPACE","key":"space","w":2},{"label":"F2","key":"f2"},{"label":"F3","key":"f3"},{"label":"F4","key":"f4"},{"label":"F5","key":"f5"},
 {"label":"CTRL","mod":"ctrl"},{"label":"SHIFT","mod":"shift"},{"label":"ALT","mod":"alt"},{"label":"WIN","key":"win"},{"label":"EXIT","action":"exit"},{"label":"@next","action":"next"}]},
{"title":"F-KEYS / NAV","cols":6,"rows":4,"keys":[
 "f1","f2","f3","f4","f5","f6","f7","f8","f9","f10","f11","f12",
 {"label":"INS","key":"insert"},{"label":"HOME","key":"home"},{"label":"PGUP","key":"pageup"},{"label":"END","key":"end"},{"label":"PGDN","key":"pagedown"},{"label":"WIN+","mod":"win"},
 {"label":"@prev","action":"prev"},{"label":"ESC","key":"esc"},{"label":"ENTER","key":"enter"},{"label":"CTRL","mod":"ctrl"},{"label":"EXIT","action":"exit"},{"label":"@next","action":"next"}]}
]})JSON";
#endif

uint8_t modBitFor(String m) {
  m.toLowerCase();
  if (m == "ctrl" || m == "control") return MOD_CTRL;
  if (m == "shift") return MOD_SHIFT;
  if (m == "alt") return MOD_ALT;
  if (m == "win" || m == "gui" || m == "super" || m == "meta") return MOD_WIN;
  return 0;
}

int32_t parseHexColour(const char *s) {
  if (!s || s[0] != '#' || strlen(s) != 7) return -1;
  uint32_t v = strtoul(s + 1, nullptr, 16);
  return tft.color565((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF);
}

// Parse {"pages":[{"title","cols","rows","keys":[...]}]} into kpPages. Keys flow left-to-right,
// top-to-bottom; "w"/"h" span cells; null or {} leaves a gap. Returns false (state unchanged) on error.
bool parseKeypadLayout(JsonVariantConst lay, String &err) {
  JsonArrayConst pages = lay["pages"].as<JsonArrayConst>();
  if (pages.isNull() || pages.size() == 0) { err = "layout needs a non-empty \"pages\" array"; return false; }
  int pc = 0;
  for (JsonVariantConst pv : pages) {
    if (pc >= MAX_KP_PAGES) { err = "too many pages (max 6)"; break; }
    KpPage &pg = kpPages[pc];
    pg.title = (const char *)(pv["title"] | "");
    pg.cols = constrain((int)(pv["cols"] | 4), 1, 6);
    pg.rows = constrain((int)(pv["rows"] | 4), 1, 6);
    pg.count = 0;
    bool occ[36] = {false};
    for (JsonVariantConst kv : pv["keys"].as<JsonArrayConst>()) {
      KpKey k;
      bool gap = false;
      int w = 1, h = 1;
      if (kv.is<const char *>()) {
        k.key = kv.as<const char *>();
        k.label = k.key;
        k.label.toUpperCase();
      } else if (kv.is<JsonObjectConst>()) {
        w = kv["w"] | 1;
        h = kv["h"] | 1;
        k.label = (const char *)(kv["label"] | "");
        k.colour = parseHexColour(kv["color"] | (const char *)nullptr);
        String act = (const char *)(kv["action"] | "");
        String mod = (const char *)(kv["mod"] | "");
        k.key = (const char *)(kv["key"] | "");
        if (act.length()) {
          k.kind = KK_ACTION;
          if (act == "next") k.action = KA_NEXT;
          else if (act == "prev") k.action = KA_PREV;
          else if (act == "exit") k.action = KA_EXIT;
          else if (act == "page") { k.action = KA_PAGE; k.arg = kv["page"] | 0; }
          else gap = true;
          if (!k.label.length()) { k.label = act; k.label.toUpperCase(); }
        } else if (mod.length()) {
          k.kind = KK_MOD;
          k.modBit = modBitFor(mod);
          if (!k.modBit) gap = true;
          if (!k.label.length()) { k.label = mod; k.label.toUpperCase(); }
        } else if (k.key.length()) {
          if (!k.label.length()) { k.label = k.key; k.label.toUpperCase(); }
        } else gap = true;
      } else gap = true;
      w = constrain(w, 1, (int)pg.cols);
      h = constrain(h, 1, (int)pg.rows);
      // first free cell (row-major) where a w x h block fits
      int fr = -1, fc = -1;
      for (int r = 0; r + h <= pg.rows && fr < 0; r++)
        for (int c = 0; c + w <= pg.cols && fr < 0; c++) {
          bool ok = true;
          for (int rr = r; rr < r + h && ok; rr++)
            for (int cc = c; cc < c + w && ok; cc++) ok = !occ[rr * 6 + cc];
          if (ok) { fr = r; fc = c; }
        }
      if (fr < 0) break;  // page full
      for (int rr = fr; rr < fr + h; rr++)
        for (int cc = fc; cc < fc + w; cc++) occ[rr * 6 + cc] = true;
      if (gap || pg.count >= MAX_KP_KEYS) continue;
      k.col = fc; k.row = fr; k.w = w; k.h = h;
      pg.keys[pg.count++] = k;
    }
    pc++;
  }
  kpPageCount = pc;
  if (kpPage >= kpPageCount) kpPage = 0;
  return true;
}

void loadDefaultKeypadLayout() {
  JsonDocument doc;
  String err;
  if (!deserializeJson(doc, KP_DEFAULT_LAYOUT)) parseKeypadLayout(doc.as<JsonVariantConst>(), err);
}

uint16_t kpAutoColour(const KpKey &k) {
  if (k.kind == KK_ACTION) return k.action == KA_EXIT ? 0x5000 : 0x2945;
  if (k.kind == KK_MOD) return 0x480F;
  String s = k.key;
  s.toLowerCase();
  if (s == "esc" || s == "escape") return 0xA800;
  if (s == "enter" || s == "return") return 0x0460;
  if (s.indexOf('+') > 0) return 0x780A;
  if (s == "up" || s == "down" || s == "left" || s == "right") return 0x0319;
  if (s.length() >= 2 && s[0] == 'f' && isdigit(s[1])) return 0x02AE;
  return 0x3186;
}

void kpCell(const KpPage &pg, const KpKey &k, int &x, int &y, int &w, int &h) {
  int W = tft.width(), gh = tft.height() - KP_HDR;
  x = k.col * W / pg.cols;
  w = (k.col + k.w) * W / pg.cols - x;
  y = KP_HDR + k.row * gh / pg.rows;
  h = KP_HDR + (k.row + k.h) * gh / pg.rows - y;
}

void drawArrow(int cx, int cy, char dir, int s, uint16_t col) {
  switch (dir) {
    case 'u': tft.fillTriangle(cx, cy - s, cx - s, cy + s * 2 / 3, cx + s, cy + s * 2 / 3, col); break;
    case 'd': tft.fillTriangle(cx, cy + s, cx - s, cy - s * 2 / 3, cx + s, cy - s * 2 / 3, col); break;
    case 'l': tft.fillTriangle(cx - s, cy, cx + s * 2 / 3, cy - s, cx + s * 2 / 3, cy + s, col); break;
    default: tft.fillTriangle(cx + s, cy, cx - s * 2 / 3, cy - s, cx - s * 2 / 3, cy + s, col); break;
  }
}

void kpDrawKey(int idx, bool pressed) {
  const KpPage &pg = kpPages[kpPage];
  const KpKey &k = pg.keys[idx];
  int x, y, w, h;
  kpCell(pg, k, x, y, w, h);
  uint16_t fill = k.colour >= 0 ? (uint16_t)k.colour : kpAutoColour(k);
  uint16_t fg = TFT_WHITE;
  if (k.kind == KK_MOD) {
    if (kpModLock & k.modBit) fill = COL_RED;
    else if (kpModOnce & k.modBit) { fill = COL_ACCENT; fg = TFT_BLACK; }
  }
  if (pressed) { fill = TFT_WHITE; fg = TFT_BLACK; }
  tft.fillRect(x, y, w, h, COL_BG);
  tft.fillRoundRect(x + S(2), y + S(2), w - S(4), h - S(4), S(7), fill);
  tft.drawRoundRect(x + S(2), y + S(2), w - S(4), h - S(4), S(7), pressed ? COL_ACCENT : dimColour(TFT_WHITE, 1, 3));
  int cx = x + w / 2, cy = y + h / 2;
  const String &L = k.label;
  if (L.startsWith("@")) {
    int s = min(w, h) / 4;
    if (L == "@up") { drawArrow(cx, cy, 'u', s, fg); return; }
    if (L == "@down") { drawArrow(cx, cy, 'd', s, fg); return; }
    if (L == "@left") { drawArrow(cx, cy, 'l', s, fg); return; }
    if (L == "@right") { drawArrow(cx, cy, 'r', s, fg); return; }
    if (L == "@next" || L == "@prev") {
      char d = L == "@next" ? 'r' : 'l';
      int s2 = s * 2 / 3, off = s2 / 2 + S(2);
      drawArrow(cx - off, cy, d, s2, fg);
      drawArrow(cx + off, cy, d, s2, fg);
      return;
    }
  }
  tft.setTextColor(fg);
  tft.setTextDatum(MC_DATUM);
  int nl = L.indexOf('\n');
  if (nl >= 0) {
    useFont(F_SB9);
    tft.drawString(L.substring(0, nl), cx, cy - S(10));
    tft.drawString(L.substring(nl + 1), cx, cy + S(10));
  } else {
    useFont(F_SB12);
    if (tft.textWidth(L) > w - S(10) || h < S(36)) useFont(F_SB9);
    if (tft.textWidth(L) > w - S(8)) useFont(F_SMALL);
    tft.drawString(L, cx, cy);
  }
  tft.setTextDatum(TL_DATUM);
}

String kpModText(uint8_t mods) {
  String s;
  for (int i = 0; i < 4; i++)
    if (mods & (1 << i)) { if (s.length()) s += "+"; s += MOD_NAMES[i]; }
  s.toUpperCase();
  return s;
}

void kpDrawHeader() {
  int W = tft.width();
  tft.fillRect(0, 0, W, KP_HDR, COL_DARK);
  useFont(F_SMALL);
  tft.setTextColor(COL_CYAN);
  tft.setTextDatum(ML_DATUM);
  tft.drawString(String("KEYPAD ") + kpPages[kpPage].title, S(4), KP_HDR / 2);
  tft.setTextDatum(MR_DATUM);
  tft.setTextColor(COL_TEXT);
  tft.drawString(String(kpPage + 1) + "/" + String(kpPageCount), W - S(4), KP_HDR / 2);
  tft.setTextDatum(MC_DATUM);
  if (kpFlash.length()) {
    tft.setTextColor(COL_GREEN);
    tft.drawString(kpFlash, W * 5 / 8, KP_HDR / 2);
  } else if (kpModOnce | kpModLock) {
    tft.setTextColor(COL_ACCENT);
    tft.drawString(kpModText(kpModOnce | kpModLock) + " +", W * 5 / 8, KP_HDR / 2);
  }
  tft.setTextDatum(TL_DATUM);
}

void drawKeypad() {
  tft.fillScreen(COL_BG);
  kpDrawHeader();
  const KpPage &pg = kpPages[kpPage];
  for (int i = 0; i < pg.count; i++) kpDrawKey(i, i == kpPressed);
}

int kpHit(int x, int y) {
  const KpPage &pg = kpPages[kpPage];
  for (int i = 0; i < pg.count; i++) {
    int cx, cy, cw, ch;
    kpCell(pg, pg.keys[i], cx, cy, cw, ch);
    if (x >= cx && x < cx + cw && y >= cy && y < cy + ch) return i;
  }
  return -1;
}

void kpGotoPage(int p) {
  kpPage = ((p % kpPageCount) + kpPageCount) % kpPageCount;
  kpPressed = -1;
  drawKeypad();
}

void enterKeypad(int page, bool fromTouch) {
  if (kpPageCount == 0) loadDefaultKeypadLayout();
  ui = UI_KEYPAD;
  kpModOnce = kpModLock = 0;
  kpFlash = "";
  kpPressed = -1;
  kpPage = constrain(page, 0, kpPageCount - 1);
  drawKeypad();
  if (fromTouch) {
    JsonDocument e;
    e["evt"] = "keypad"; e["state"] = "on"; e["source"] = "touch";
    emitLine(e);
  }
}

void exitKeypad(bool fromTouch) {
  ui = UI_NORMAL;
  kpPressed = -1;
  drawCard();  // back to the idle playlist or the table cards
  lastRotate = millis();
  if (fromTouch) {
    JsonDocument e;
    e["evt"] = "keypad"; e["state"] = "off"; e["source"] = "touch";
    emitLine(e);
  }
}

void kpActivate(int idx) {
  KpKey &k = kpPages[kpPage].keys[idx];
  if (k.kind == KK_ACTION) {
    switch (k.action) {
      case KA_NEXT: kpGotoPage(kpPage + 1); break;
      case KA_PREV: kpGotoPage(kpPage - 1); break;
      case KA_PAGE: kpGotoPage(k.arg); break;
      default: exitKeypad(true); break;
    }
    return;
  }
  if (k.kind == KK_MOD) {  // off -> one-shot -> locked -> off
    if (kpModLock & k.modBit) kpModLock &= ~k.modBit;
    else if (kpModOnce & k.modBit) { kpModOnce &= ~k.modBit; kpModLock |= k.modBit; }
    else kpModOnce |= k.modBit;
    kpDrawKey(idx, false);
    kpDrawHeader();
    return;
  }
  uint8_t mods = kpModOnce | kpModLock;
  JsonDocument e;
  e["evt"] = "key";
  e["key"] = k.key;
  if (mods) {
    JsonArray a = e["mods"].to<JsonArray>();
    for (int i = 0; i < 4; i++) if (mods & (1 << i)) a.add(MOD_NAMES[i]);
  }
  emitLine(e);
  String shown = k.label.startsWith("@") ? k.key : k.label;
  shown.replace("\n", " ");
  shown.toUpperCase();
  kpFlash = (mods ? kpModText(mods) + "+" : String("")) + shown;
  kpFlashMs = millis();
  kpDrawKey(idx, false);
  if (kpModOnce) {  // one-shot modifiers are used up
    kpModOnce = 0;
    const KpPage &pg = kpPages[kpPage];
    for (int i = 0; i < pg.count; i++) if (pg.keys[i].kind == KK_MOD) kpDrawKey(i, false);
  }
  kpDrawHeader();
}

void kpTick(unsigned long now) {
  if (kpFlash.length() && now - kpFlashMs > 900) {
    kpFlash = "";
    kpDrawHeader();
  }
}

// ---------- On-device touch calibration ----------
static const int16_t CAL_PTS[4][2] = {{20, 20}, {299, 20}, {299, 219}, {20, 219}};
struct CalRun {
  uint8_t step = 0;
  int32_t rx[4], ry[4];
  unsigned long stepStart = 0;
} cal;

void calDrawTarget() {
  tft.fillScreen(COL_BG);
  tft.setTextColor(COL_TEXT);
  tft.setTextDatum(MC_DATUM);
  useFont(F_SB12);
  tft.drawString("TOUCH CALIBRATION", 160, 90);
  useFont(F_S9);
  tft.setTextColor(COL_ACCENT);
  tft.drawString("Tap the centre of the cross (" + String(cal.step + 1) + "/4)", 160, 125);
  tft.setTextColor(COL_DIM);
  tft.drawString("use a stylus or fingernail", 160, 150);
  tft.setTextDatum(TL_DATUM);
  int x = CAL_PTS[cal.step][0], y = CAL_PTS[cal.step][1];
  tft.drawFastHLine(x - 14, y, 29, COL_RED);
  tft.drawFastVLine(x, y - 14, 29, COL_RED);
  tft.drawCircle(x, y, 7, COL_YELLOW);
}

void calStart() {
  calPrevUi = ui == UI_CAL ? calPrevUi : ui;
  ui = UI_CAL;
  cal.step = 0;
  cal.stepStart = millis();
  boardSetRotation(1);  // targets are defined in native landscape coordinates
  calDrawTarget();
}

void calEnd(bool ok, const char *err) {
  JsonDocument e;
  e["evt"] = "cal";
  e["ok"] = ok;
  if (err) e["err"] = err;
  e["x_min"] = tcal.xMin; e["x_max"] = tcal.xMax; e["y_min"] = tcal.yMin; e["y_max"] = tcal.yMax;
  emitLine(e);
  tft.fillScreen(COL_BG);
  tft.setTextDatum(MC_DATUM);
  useFont(F_SB12);
  tft.setTextColor(ok ? COL_GREEN : COL_RED);
  tft.drawString(ok ? "CALIBRATED" : "CALIBRATION FAILED", 160, 110);
  tft.setTextDatum(TL_DATUM);
  delay(1200);
  boardSetRotation(st.rotation);
  ui = calPrevUi;
  if (ui == UI_KEYPAD) drawKeypad();
  else drawCard();
  lastRotate = millis();
}

void calSample(int32_t rx, int32_t ry) {
  cal.rx[cal.step] = rx;
  cal.ry[cal.step] = ry;
  cal.step++;
  cal.stepStart = millis();
  if (cal.step < 4) { calDrawTarget(); return; }
  // left/right x from points 0,3 / 1,2 ; top/bottom y from 0,1 / 2,3; extrapolate to screen edges
  float xl = (cal.rx[0] + cal.rx[3]) / 2.0f, xr = (cal.rx[1] + cal.rx[2]) / 2.0f;
  float yt = (cal.ry[0] + cal.ry[1]) / 2.0f, yb = (cal.ry[2] + cal.ry[3]) / 2.0f;
  float kx = (xr - xl) / (CAL_PTS[1][0] - CAL_PTS[0][0]), ky = (yb - yt) / (CAL_PTS[2][1] - CAL_PTS[1][1]);
  TouchCal c;
  c.xMin = (int16_t)lroundf(xl - CAL_PTS[0][0] * kx);
  c.xMax = (int16_t)lroundf(c.xMin + 320 * kx);
  c.yMin = (int16_t)lroundf(yt - CAL_PTS[0][1] * ky);
  c.yMax = (int16_t)lroundf(c.yMin + 240 * ky);
  if (!calValid(c)) { calEnd(false, "implausible readings (axes swapped or missed a target?)"); return; }
  tcal = c;
  saveCal();
  calEnd(true, nullptr);
}

void calTick(unsigned long now) {
  if (now - cal.stepStart > 30000UL) calEnd(false, "timeout");
}

// ---------- Identify overlay + identity persistence ----------
UiMode identPrevUi = UI_NORMAL;
unsigned long identUntil = 0;

// A stable colour per role, so boards are easy to tell apart at a glance
uint16_t roleColour(const String &role) {
  if (role == "right") return 0x041F;   // blue
  if (role == "left") return COL_GREEN;
  if (role == "top") return COL_MAGENTA;
  if (role == "bottom") return COL_ACCENT;
  if (role == "center" || role == "centre") return COL_CYAN;
  if (!role.length()) return COL_DIM;
  static const uint16_t pal[] = {COL_GOLD, COL_RED, 0x07F0, 0xFB56, 0x867F, COL_YELLOW};
  uint32_t h = 0;
  for (char ch : role) h = h * 31 + (uint8_t)ch;
  return pal[h % 6];
}

void drawIdentify() {
  int W = tft.width(), H = tft.height();
  uint16_t col = roleColour(ident.role);
  tft.fillScreen(COL_BG);
  for (int i = 0; i < S(8); i++) tft.drawRect(i, i, W - 2 * i, H - 2 * i, col);
  tft.setTextDatum(MC_DATUM);
  useFont(F_S9);
  tft.setTextColor(COL_DIM);
  tft.drawString("THIS DISPLAY IS", W / 2, S(24));
  String r = ident.role.length() ? ident.role : String("no role");
  r.toUpperCase();
  drawCenteredFit(r, W / 2, H * 40 / 100, W - S(36), H * 36 / 100, col, 0, COL_DARK);
  String n = ident.name.length() ? ident.name : String("(no name set)");
  drawCenteredFit(n, W / 2, H * 69 / 100, W - S(36), S(36), COL_TEXT, 2);
  useFont(F_S9);
  tft.setTextColor(COL_ACCENT);
  tft.setTextDatum(MC_DATUM);
  tft.drawString(boardId() + "   fw " FW_VERSION, W / 2, H - S(28));
  tft.setTextDatum(TL_DATUM);
}

void startIdentify(unsigned long ms) {
  if (ui != UI_IDENT) identPrevUi = ui;
  ui = UI_IDENT;
  identUntil = millis() + ms;
  drawIdentify();
}

void endIdentify() {
  if (ui != UI_IDENT) return;
  ui = identPrevUi;
  if (ui == UI_KEYPAD) drawKeypad();
  else drawCard();
  lastRotate = millis();
}

// Printable ASCII only, trimmed, at most maxLen characters (the fonts are ASCII-only)
String cleanText(const char *s, size_t maxLen, bool lower) {
  String out;
  for (const char *p = s; *p && out.length() < maxLen; p++) {
    char ch = *p;
    if (ch < 32 || ch > 126) continue;
    out += lower ? (char)tolower(ch) : ch;
  }
  out.trim();
  return out;
}

bool validId(const String &id) {
  if (id.length() < 1 || id.length() > 24) return false;
  for (char ch : id)
    if (!(isalnum((unsigned char)ch) || ch == '-' || ch == '_' || ch == '.')) return false;
  return true;
}

void putStringIfChanged(const char *key, const String &v) {
  if (prefs.getString(key, "") != v) prefs.putString(key, v);
}

// config / set_id: validate every given field first, then apply and save only what changed.
bool applyIdentity(JsonDocument &doc, bool allowId, String &err) {
  bool hasName = doc["name"].is<const char *>(), hasRole = doc["role"].is<const char *>();
  bool hasKp = doc["keypad"].is<bool>(), hasRot = !doc["rotation"].isNull();
  bool hasId = allowId && doc["id"].is<const char *>(), resetId = allowId && (doc["reset"] | false);
  int rot = doc["rotation"] | -1;
  if (hasRot && (rot < 0 || rot > 3)) { err = "rotation must be 0-3"; return false; }
  String newId = hasId ? cleanText(doc["id"].as<const char *>(), 32, true) : String();
  if (hasId && !validId(newId)) { err = "id: 1-24 chars a-z 0-9 - _ ."; return false; }
  prefs.begin("cards", false);
  if (hasName) { ident.name = cleanText(doc["name"].as<const char *>(), 32, false); putStringIfChanged("bname", ident.name); }
  if (hasRole) { ident.role = cleanText(doc["role"].as<const char *>(), 16, true); putStringIfChanged("brole", ident.role); }
  if (hasKp) { ident.keypad = doc["keypad"].as<bool>(); if (prefs.getBool("kpen", true) != ident.keypad) prefs.putBool("kpen", ident.keypad); }
  if (resetId) { ident.customId = ""; putStringIfChanged("bid", ""); }
  else if (hasId) { ident.customId = (newId == ident.macId) ? String() : newId; putStringIfChanged("bid", ident.customId); }
  if (hasRot && rot != st.rotation) prefs.putUChar("rot", (uint8_t)rot);
  prefs.end();
  if (hasRot && rot != st.rotation) {
    st.rotation = rot;
    if (ui != UI_CAL) boardSetRotation(rot);
    if (ui == UI_IDENT) drawIdentify();
    else if (ui == UI_KEYPAD) drawKeypad();
    else if (ui == UI_NORMAL) drawCard();
  } else if (ui == UI_IDENT) {
    drawIdentify();  // show the new name/role right away
  }
  return true;
}

void addIdentity(JsonDocument &r) {
  r["board"] = BOARD_KIND;  // fw 1.4.0: "cyd" | "ws-s3-7"
  r["id"] = boardId();
  r["name"] = ident.name;
  r["role"] = ident.role;
  r["rotation"] = st.rotation;
  r["keypad"] = ident.keypad;
}

// ---------- Serial ----------
void reply(const char *cmd, bool ok, const char *err = nullptr, const char *extraKey = nullptr, int extraVal = 0) {
  JsonDocument r;
  r["ack"] = cmd;
  r["ok"] = ok;
  if (err) r["err"] = err;
  if (extraKey) r[extraKey] = extraVal;
  serializeJson(r, HOST);
  HOST.println();
}

// Leave keypad / calibration screens (a table or idle push always wins).
void leaveOverlay() {
  if (ui == UI_CAL) boardSetRotation(st.rotation);
  ui = UI_NORMAL;
  kpPressed = -1;
}

void redrawUi() {
  if (ui == UI_KEYPAD) drawKeypad();
  else if (ui == UI_CAL) calDrawTarget();
  else if (ui == UI_IDENT) drawIdentify();
  else drawCard();
}

const char *modeName() {
  if (ui == UI_KEYPAD) return "keypad";
  if (ui == UI_CAL) return "calibrate";
  if (ui == UI_IDENT) return "identify";
  return st.idle ? "idle" : "table";
}

void replyCal(const char *ack) {
  JsonDocument r;
  r["ack"] = ack;
  r["ok"] = true;
  r["x_min"] = tcal.xMin; r["x_max"] = tcal.xMax; r["y_min"] = tcal.yMin; r["y_max"] = tcal.yMax;
  r["debug"] = touchDebug;
  emitLine(r);
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
    leaveOverlay();
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
    leaveOverlay();
    st.idle = true;
    saveIdle(true);
    drawCard();
    JsonDocument r;
    r["ack"] = "idle";
    r["ok"] = true;
    r["screens"] = icfg.count;
    r["clock"] = clockValid;
    serializeJson(r, HOST);
    HOST.println();
  } else if (!strcmp(cmd, "brightness")) {
    int v = doc["value"] | -1;
    if (v < 0 || v > 255) { reply("brightness", false, "value must be 0-255"); return; }
    setBrightness((uint8_t)v);
    saveSetting("bright", (uint8_t)v);
#if BACKLIGHT_DIMMABLE
    reply("brightness", true, nullptr, "value", v);
#else
    JsonDocument r;  // on/off backlight: any value > 0 = on
    r["ack"] = "brightness";
    r["ok"] = true;
    r["value"] = v;
    r["dimmable"] = false;
    r["backlight"] = v ? "on" : "off";
    emitLine(r);
#endif
  } else if (!strcmp(cmd, "rotation")) {
    int v = doc["value"] | -1;
    if (v < 0 || v > 3) { reply("rotation", false, "value must be 0-3"); return; }
    st.rotation = v;
    if (ui != UI_CAL) boardSetRotation(v);  // calibration restores it when done
    saveSetting("rot", (uint8_t)v);
    redrawUi();
    reply("rotation", true, nullptr, "value", v);
  } else if (!strcmp(cmd, "next")) {
    endIdentify();
    if (ui == UI_KEYPAD) {
      kpGotoPage(kpPage + 1);
      reply("next", true, nullptr, "page", kpPage);
      return;
    }
    nextCard();
    reply("next", true, nullptr, "card", st.idle ? irt.idx : st.current);
  } else if (!strcmp(cmd, "ping") || !strcmp(cmd, "hello")) {
    JsonDocument r;
    r["ack"] = cmd;
    r["ok"] = true;
    r["fw"] = FW_VERSION;
    r["device"] = "cyd-pinball-cards";
    r["mode"] = modeName();
    addIdentity(r);
    emitLine(r);
  } else if (!strcmp(cmd, "identify")) {
    if (ui == UI_CAL) { reply("identify", false, "calibrating"); return; }
    int secs = doc["secs"] | (IDENTIFY_MS / 1000);
    secs = constrain(secs, 1, 60);
    startIdentify((unsigned long)secs * 1000UL);
    JsonDocument r;
    r["ack"] = "identify";
    r["ok"] = true;
    r["secs"] = secs;
    addIdentity(r);
    emitLine(r);
  } else if (!strcmp(cmd, "config") || !strcmp(cmd, "set_id")) {
    String err;
    bool ok = applyIdentity(doc, !strcmp(cmd, "set_id"), err);
    JsonDocument r;
    r["ack"] = cmd;
    r["ok"] = ok;
    if (!ok) r["err"] = err;
    r["fw"] = FW_VERSION;
    addIdentity(r);
    emitLine(r);
  } else if (!strcmp(cmd, "keypad")) {
    if (doc["exit"] | false) {
      if (ui == UI_IDENT && identPrevUi == UI_KEYPAD) identPrevUi = UI_NORMAL;
      if (ui == UI_KEYPAD) exitKeypad(false);
      reply("keypad", true);
      return;
    }
    String err;
    bool ok = true;
    JsonVariantConst lay = doc["layout"].is<JsonObject>() ? doc["layout"].as<JsonVariantConst>() : JsonVariantConst();
    if (lay.isNull() && doc["pages"].is<JsonArray>()) lay = doc.as<JsonVariantConst>();
    if (!lay.isNull()) {
      ok = parseKeypadLayout(lay, err);
      if (ok) {
        String saved = "{\"pages\":";  // store only the pages (not cmd/page/other keys)
        String pagesJson;
        serializeJson(lay["pages"], pagesJson);
        saved += pagesJson + "}";
        saveBlobIfChanged("kplayout", saved);
      }
    }
    if (ui == UI_CAL) leaveOverlay();
    enterKeypad(doc["page"] | 0, false);
    JsonDocument r;
    r["ack"] = "keypad";
    r["ok"] = ok;
    if (!ok) r["err"] = err;
    r["pages"] = kpPageCount;
    r["page"] = kpPage;
    emitLine(r);
  } else if (!strcmp(cmd, "cal")) {
    if (doc["debug"].is<bool>()) touchDebug = doc["debug"].as<bool>();
#if TOUCH_CAPACITIVE
    JsonDocument r;  // GT911: factory-calibrated, nothing to set or save
    r["ack"] = "cal";
    r["ok"] = true;
    r["touch"] = "capacitive";
    r["note"] = "no calibration needed";
    r["debug"] = touchDebug;
    emitLine(r);
    return;
#endif
    if (doc["reset"] | false) {
      tcal = TouchCal();
      saveCal();
    } else if (doc["x_min"].is<int>() || doc["x_max"].is<int>() || doc["y_min"].is<int>() || doc["y_max"].is<int>()) {
      TouchCal c = tcal;
      c.xMin = doc["x_min"] | (int)c.xMin; c.xMax = doc["x_max"] | (int)c.xMax;
      c.yMin = doc["y_min"] | (int)c.yMin; c.yMax = doc["y_max"] | (int)c.yMax;
      if (!calValid(c)) { reply("cal", false, "range too small/large (need 1000..8000 raw units per axis)"); return; }
      tcal = c;
      saveCal();
    }
    replyCal("cal");
  } else if (!strcmp(cmd, "calibrate")) {
#if TOUCH_CAPACITIVE
    JsonDocument r;
    r["ack"] = "calibrate";
    r["ok"] = true;
    r["touch"] = "capacitive";
    r["note"] = "no calibration needed";
    emitLine(r);
    JsonDocument e;  // also close the host's "calibrating" state right away
    e["evt"] = "cal"; e["ok"] = true; e["touch"] = "capacitive";
    emitLine(e);
    return;
#endif
    if (ui == UI_IDENT) ui = identPrevUi;
    calStart();
    reply("calibrate", true);
  } else {
    reply(cmd[0] ? cmd : "?", false, "unknown cmd");
  }
}

void pollSerial() {
  while (HOST.available()) {
    char ch = (char)HOST.read();
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
// A small gesture layer: press / hold / release with screen coordinates.
//   idle + cards: tap (on release) = next card/screen; hold LONGPRESS_KEYPAD_MS = open keypad
//   keypad: press highlights a key, release on it sends it; horizontal swipe = page change
//   calibrate: the averaged raw position of each press is one calibration sample
struct TouchState {
  bool down = false, consumed = false;
  unsigned long t0 = 0, lastSeen = 0;
  int x0 = 0, y0 = 0, x = 0, y = 0;  // screen coordinates (current rotation)
  int32_t sumX = 0, sumY = 0, n = 0; // raw accumulators (calibration)
} tch;
unsigned long lastTapMs = 0;

void onTouchDown() {
  if (ui == UI_IDENT) {  // a tap closes the identify label; the rest of this touch is ignored
    tch.consumed = true;
    endIdentify();
    return;
  }
  if (ui == UI_KEYPAD) {
    kpPressed = kpHit(tch.x0, tch.y0);
    if (kpPressed >= 0) kpDrawKey(kpPressed, true);
  }
}

void onTouchHeld(unsigned long now) {
  if (ui == UI_KEYPAD) {
    // finger slid far sideways: treat as a swipe, drop the highlight
    if (kpPressed >= 0 && abs(tch.x - tch.x0) > KP_SWIPE_PX / 2) {
      kpDrawKey(kpPressed, false);
      kpPressed = -1;
    }
  } else if (ui == UI_NORMAL && ident.keypad && LONGPRESS_KEYPAD_MS > 0 && !tch.consumed && now - tch.t0 >= LONGPRESS_KEYPAD_MS) {
    tch.consumed = true;
    enterKeypad(0, true);
  }
}

void onTouchUp(unsigned long now) {
  if (ui == UI_CAL) {
    if (tch.n >= 3) calSample(tch.sumX / tch.n, tch.sumY / tch.n);
    return;
  }
  if (ui == UI_KEYPAD) {
    int dx = tch.x - tch.x0, dy = tch.y - tch.y0;
    if (tch.consumed) {  // the long-press that opened the keypad: ignore its release
      if (kpPressed >= 0) kpDrawKey(kpPressed, false);
      kpPressed = -1;
      return;
    }
    if (abs(dx) >= KP_SWIPE_PX && abs(dx) > abs(dy)) {
      kpGotoPage(kpPage + (dx < 0 ? 1 : -1));
      return;
    }
    int idx = kpPressed;
    kpPressed = -1;
    if (idx >= 0) {
      if (kpHit(tch.x, tch.y) == idx) kpActivate(idx);  // released on the same key
      else kpDrawKey(idx, false);                        // slid off: cancel
    }
    return;
  }
  if (!tch.consumed && now - lastTapMs > 150) {
    lastTapMs = now;
    nextCard();
    lastRotate = now;  // restart auto-rotate timer after manual tap
  }
}

void pollTouch() {
  unsigned long now = millis();
  TouchSample p;
  if (boardReadTouch(p)) {
    int sx = p.x, sy = p.y;
#if !TOUCH_CAPACITIVE
    touchToScreen(p.rawX, p.rawY, sx, sy);  // resistive: raw -> screen via the calibration
#endif
    if (!tch.down) {
      tch.down = true;
      tch.consumed = false;
      tch.t0 = now;
      tch.x0 = tch.x = sx;
      tch.y0 = tch.y = sy;
      tch.sumX = tch.sumY = tch.n = 0;
      if (touchDebug) {
        JsonDocument e;
        e["evt"] = "touch"; e["raw_x"] = p.rawX; e["raw_y"] = p.rawY; e["z"] = p.z; e["x"] = sx; e["y"] = sy;
        emitLine(e);
      }
      onTouchDown();
    } else if (p.z >= 500) {  // ignore the weak, noisy samples while lifting off
      tch.x = sx;
      tch.y = sy;
    }
    if (p.z >= 600 && now - tch.t0 >= 40) { tch.sumX += p.rawX; tch.sumY += p.rawY; tch.n++; }
    tch.lastSeen = now;
    onTouchHeld(now);
  } else if (tch.down && now - tch.lastSeen > 40) {  // released (debounced)
    tch.down = false;
    onTouchUp(now);
  }
}

// ---------- Setup / loop ----------
void setup() {
  // Big RX ring buffer (set before begin): a full 6 KB line can arrive while a screen is drawn
  boardBeginSerial(RX_BUFFER);
  rxLine.reserve(1024);
  randomSeed(esp_random());

  {  // default board id from the factory MAC (last 3 bytes), e.g. cyd-a1b2c3
    uint64_t mac = ESP.getEfuseMac();
    char buf[16];
    snprintf(buf, sizeof buf, "cyd-%02x%02x%02x", (unsigned)((mac >> 24) & 0xFF), (unsigned)((mac >> 32) & 0xFF),
             (unsigned)((mac >> 40) & 0xFF));
    ident.macId = buf;
  }
  prefs.begin("cards", true);
  st.brightness = prefs.getUChar("bright", DEFAULT_BRIGHTNESS);
  st.rotation = prefs.getUChar("rot", CYD_ROTATION);
  bool wasIdle = prefs.getBool("idle", true);
  String last = loadBlob("last");
  String idleCfg = loadBlob("idlecfg");
  lastTitle = prefs.getString("lastT", "");
  lastTs = prefs.getULong("lastTs", 0);
  String kpLayout = loadBlob("kplayout");
  ident.customId = prefs.getString("bid", "");
  ident.name = prefs.getString("bname", "");
  ident.role = prefs.getString("brole", "");
  ident.keypad = prefs.getBool("kpen", true);
  if (prefs.getBytesLength("tcal") == sizeof(TouchCal)) {
    TouchCal c;
    prefs.getBytes("tcal", &c, sizeof c);
    if (calValid(c)) tcal = c;
  }
  prefs.end();

  loadDefaultKeypadLayout();
  if (kpLayout.length()) {
    JsonDocument doc;
    String err;
    if (!deserializeJson(doc, kpLayout)) parseKeypadLayout(doc.as<JsonVariantConst>(), err);
  }

  loadDefaultIdleConfig();
  if (idleCfg.length()) {
    JsonDocument doc;
    if (!deserializeJson(doc, idleCfg)) applyIdleConfig(doc);
  }

  // Display, backlight and touch controller (board.h): CYD = PWM backlight, TFT_eSPI, XPT2046;
  // Waveshare 7" = CH422G resets + backlight switch, LovyanGFX RGB panel, GT911
  boardInitDisplay(st.rotation, st.brightness);

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
  JsonDocument r;
  r["ready"] = true;
  r["device"] = "cyd-pinball-cards";
  r["fw"] = FW_VERSION;
  addIdentity(r);
  emitLine(r);
}

void loop() {
  pollSerial();
  pollTouch();
  unsigned long now = millis();
  if (ui == UI_KEYPAD) {
    kpTick(now);
  } else if (ui == UI_CAL) {
    calTick(now);
  } else if (ui == UI_IDENT) {
    if ((long)(now - identUntil) >= 0) endIdentify();
  } else if (!st.idle) {
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
  delay(ui == UI_NORMAL && !st.idle ? 5 : 2);
}
