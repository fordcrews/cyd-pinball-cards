// Wi-Fi station + TCP client for house displays. Same newline JSON as USB.
// Credentials: firmware/wifi.json (gitignored; copy firmware/wifi.example.json). Not stored in git.
#include "wifi_link.h"
#include "board.h"
#include "wifi_secrets.h"
#include <WiFi.h>
#include <WiFiUdp.h>
#include <ArduinoJson.h>

#ifndef WIFI_USB_QUIET_MS
#define WIFI_USB_QUIET_MS 15000UL
#endif
#ifndef WIFI_BEACON_PORT
#define WIFI_BEACON_PORT 47311
#endif
// Dialing the cabinet: 3 s at first, doubling after each failed dial or refused session (closed
// within WIFI_SHORT_SESSION_MS, e.g. the cabinet already has this board on USB) up to
// WIFI_RETRY_MAX_MS. Once a USB host has sent a whole command line since boot (a UART bridge
// cannot tell whether the host still holds the port; a stray byte on the UART does not count),
// the board dials at most every WIFI_USB_RETRY_MS.
#ifndef WIFI_RETRY_MIN_MS
#define WIFI_RETRY_MIN_MS 3000UL
#endif
#ifndef WIFI_RETRY_MAX_MS
#define WIFI_RETRY_MAX_MS 60000UL
#endif
#ifndef WIFI_USB_RETRY_MS
#define WIFI_USB_RETRY_MS 120000UL
#endif
#ifndef WIFI_SHORT_SESSION_MS
#define WIFI_SHORT_SESSION_MS 15000UL
#endif

static const int SRC_USB = 0;
static const int SRC_WIFI = 1;

static WiFiClient client;
static WiFiUDP beaconUdp;
static bool staStarted = false;
static bool udpOn = false;
static bool useStatic = false;
static char staticHost[80];
static uint16_t staticPort = 47311;
static IPAddress beaconIp;
static uint16_t beaconPort = 47311;
static bool haveBeacon = false;
static unsigned long nextTry = 0;
static unsigned long usbLast = 0;
static bool usbSeen = false;
static bool usbHostLine = false;    // a newline-terminated line came over USB since boot
static int active = SRC_USB;
// Wi-Fi bytes are read from the socket in blocks: one connected()/read() per byte cost a socket
// call each, which held a 4 KB picture chunk to ~9 KB/s.
static uint8_t rxb[1024];
static size_t rxHead = 0, rxLen = 0;
static uint8_t dialFails = 0;       // failed dials / refused sessions in a row
static bool wasUp = false;          // a TCP session was open at the last poll
static unsigned long upSince = 0;
static bool sessionChanged = false;
static void (*onWifiUp)() = nullptr;

// Replies are collected per line and sent in one write: ArduinoJson prints byte by byte, and one
// TCP segment per byte (Nagle + the host's delayed ACK) cost ~0.3 s per ack over Wi-Fi.
class LinkPrinter : public Print {
 public:
  size_t write(uint8_t c) override { return write(&c, 1); }
  size_t write(const uint8_t *buf, size_t n) override;

 private:
  uint8_t line[512];
  size_t len = 0;
};
static LinkPrinter printer;

static bool wifiEnabled() { return WIFI_CFG_SSID[0] != 0; }

static bool usbSession() {
  if (boardUsbHostOpen()) return true;
  if (!usbSeen) return false;
  return (millis() - usbLast) < WIFI_USB_QUIET_MS;
}

static void dropWifi(bool changed) {
  rxHead = rxLen = 0;
  if (client.connected()) client.stop();
  if (active != SRC_USB) {
    active = SRC_USB;
    if (changed) sessionChanged = true;
  }
}

static void noteUsbByte() {
  usbSeen = true;
  usbLast = millis();
  dropWifi(true);
}

void linkOnWifiSession(void (*fn)()) { onWifiUp = fn; }

void linkBegin() {
  staticHost[0] = 0;
  if (WIFI_CFG_HOST[0]) {
    useStatic = true;
    strncpy(staticHost, WIFI_CFG_HOST, sizeof staticHost - 1);
    staticHost[sizeof staticHost - 1] = 0;
    staticPort = WIFI_CFG_PORT ? (uint16_t)WIFI_CFG_PORT : 47311;
  }
}

bool linkSessionChanged() {
  bool c = sessionChanged;
  sessionChanged = false;
  return c;
}

static size_t wifiBuffered() {
  if (rxHead < rxLen) return rxLen - rxHead;
  rxHead = rxLen = 0;
  if (!client.connected()) return 0;  // once per block, not per byte
  int n = client.available();
  if (n <= 0) return 0;
  int got = client.read(rxb, n > (int)sizeof rxb ? (int)sizeof rxb : n);
  if (got <= 0) return 0;
  rxLen = (size_t)got;
  return rxLen;
}

int linkAvailable() {
  if (HOST.available()) {
    noteUsbByte();
    return HOST.available();
  }
  if (usbSession()) {
    dropWifi(true);
    return 0;
  }
  return (int)wifiBuffered();
}

int linkRead() {
  if (HOST.available()) {
    noteUsbByte();
    int c = HOST.read();
    if (c == '\n') usbHostLine = true;
    return c;
  }
  if (usbSession()) return -1;
  if (!wifiBuffered()) return -1;
  if (active != SRC_WIFI) {
    active = SRC_WIFI;
    sessionChanged = true;
  }
  return rxb[rxHead++];
}

static void linkWrite(const uint8_t *buf, size_t n) {
  if (!usbSession() && active == SRC_WIFI && client.connected()) {
    size_t sent = client.write(buf, n);
    if (sent != n) dropWifi(true);
    return;
  }
  HOST.write(buf, n);
}

size_t LinkPrinter::write(const uint8_t *buf, size_t n) {
  for (size_t i = 0; i < n; i++) {
    line[len++] = buf[i];
    if (buf[i] == '\n' || len == sizeof line) {
      linkWrite(line, len);
      len = 0;
    }
  }
  return n;
}

Print &linkOut() { return printer; }

static void considerBeacon() {
  if (useStatic || !udpOn) return;
  int n = beaconUdp.parsePacket();
  if (n <= 0) return;
  char buf[256];
  int got = beaconUdp.read(buf, sizeof buf - 1);
  if (got < 0) got = 0;
  buf[got] = 0;
  JsonDocument doc;
  if (deserializeJson(doc, buf)) return;
  const char *svc = doc["svc"] | "";
  if (strcmp(svc, "cyd-pinball-cards") != 0) return;
  int tcp = doc["tcp"] | 47311;
  if (tcp < 1 || tcp > 65535) return;
  beaconIp = beaconUdp.remoteIP();
  beaconPort = (uint16_t)tcp;
  haveBeacon = beaconIp != IPAddress(0, 0, 0, 0);
}

static unsigned long retryDelay() {
  unsigned long d = WIFI_RETRY_MIN_MS << (dialFails > 5 ? 5 : dialFails);
  if (d > WIFI_RETRY_MAX_MS) d = WIFI_RETRY_MAX_MS;
  if (usbHostLine && d < WIFI_USB_RETRY_MS) d = WIFI_USB_RETRY_MS;
  return d;
}

// Notice a session that ended (cabinet closed it, Wi-Fi blip, or USB took over) and pace the next dial.
static void trackSession() {
  bool up = client.connected();
  if (wasUp && !up) {
    if (millis() - upSince < WIFI_SHORT_SESSION_MS) {
      if (dialFails < 255) dialFails++;
    } else {
      dialFails = 0;
    }
    nextTry = millis() + retryDelay();
  }
  wasUp = up;
}

static bool dial() {
  if (useStatic) return client.connect(staticHost, staticPort, 200) != 0;
  if (!haveBeacon) return false;
  return client.connect(beaconIp, beaconPort, 200) != 0;
}

void linkPoll() {
  if (!wifiEnabled()) return;
  trackSession();
  if (usbSession()) {
    dropWifi(true);
    return;
  }
  if (WiFi.status() != WL_CONNECTED) {
    if (!staStarted) {
      WiFi.persistent(false);  // do not copy the password into NVS; wifi.json is the source
      WiFi.mode(WIFI_STA);
      WiFi.setAutoReconnect(true);
      if (WIFI_CFG_PASS[0]) WiFi.begin(WIFI_CFG_SSID, WIFI_CFG_PASS);
      else WiFi.begin(WIFI_CFG_SSID);
      // No modem sleep: with it the radio only wakes for DTIM beacons, so every line from the
      // cabinet waited 100-300 ms and picture chunks timed out. A display is powered anyway.
      WiFi.setSleep(false);
      staStarted = true;
    }
    return;
  }
  if (!udpOn && !useStatic) {
    udpOn = beaconUdp.begin(WIFI_BEACON_PORT);
  }
  considerBeacon();
  if (client.connected()) return;
  if ((long)(millis() - nextTry) < 0) return;
  if (!useStatic && !haveBeacon) {
    nextTry = millis() + 1000;  // no beacon heard yet
    return;
  }
  if (!dial()) {
    if (dialFails < 255) dialFails++;
    nextTry = millis() + retryDelay();
    return;
  }
  client.setNoDelay(true);  // request/ack lines: send each one now
  wasUp = true;
  upSince = millis();
  nextTry = millis() + retryDelay();
  active = SRC_WIFI;
  sessionChanged = true;
  if (onWifiUp) onWifiUp();
}