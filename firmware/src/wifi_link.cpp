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
static int active = SRC_USB;
static bool sessionChanged = false;
static void (*onWifiUp)() = nullptr;

class LinkPrinter : public Print {
 public:
  size_t write(uint8_t c) override { return write(&c, 1); }
  size_t write(const uint8_t *buf, size_t n) override;
};
static LinkPrinter printer;

static bool wifiEnabled() { return WIFI_CFG_SSID[0] != 0; }

static bool usbSession() {
  if (boardUsbHostOpen()) return true;
  if (!usbSeen) return false;
  return (millis() - usbLast) < WIFI_USB_QUIET_MS;
}

static void dropWifi(bool changed) {
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

int linkAvailable() {
  if (HOST.available()) {
    noteUsbByte();
    return HOST.available();
  }
  if (usbSession()) {
    dropWifi(true);
    return 0;
  }
  if (client.connected() && client.available()) return client.available();
  return 0;
}

int linkRead() {
  if (HOST.available()) {
    noteUsbByte();
    return HOST.read();
  }
  if (usbSession()) return -1;
  if (client.connected() && client.available()) {
    if (active != SRC_WIFI) {
      active = SRC_WIFI;
      sessionChanged = true;
    }
    return client.read();
  }
  return -1;
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
  linkWrite(buf, n);
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

static bool dial() {
  if (useStatic) return client.connect(staticHost, staticPort, 200) != 0;
  if (!haveBeacon) return false;
  return client.connect(beaconIp, beaconPort, 200) != 0;
}

void linkPoll() {
  if (!wifiEnabled()) return;
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
  nextTry = millis() + 3000;
  if (!useStatic && !haveBeacon) return;
  if (!dial()) return;
  active = SRC_WIFI;
  sessionChanged = true;
  if (onWifiUp) onWifiUp();
}